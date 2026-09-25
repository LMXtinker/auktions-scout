"""Auktions-Scout Scraper.

Öffnet pro Plattform und Suchbegriff die Suchergebnisse in einem Headless-Chromium,
extrahiert Los-Karten (Link + sichtbarer Text) und schreibt:
  data/latest.json  – alle aktuell gefundenen Lose
  data/new.json     – Lose, die seit dem letzten Lauf neu sind
  data/seen.json    – Gedächtnis (URL -> erstes Auftauchen)
  debug/            – Diagnose (finale Such-URLs, JSON-API-Aufrufe, Screenshots)
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import yaml
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DEBUG = ROOT / "debug"
NOW = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()

COOKIE_RE = re.compile(
    r"^\s*(alle\s+)?(cookies\s+)?(akzeptieren|zustimmen|annehmen|erlauben)|"
    r"^\s*accept( all)?( cookies)?\s*$|einverstanden|^\s*ok\s*$|alle zulassen",
    re.I,
)
SEARCH_SELECTORS = [
    "input[type=search]",
    "input[name=q]",
    "input[name*=search i]",
    "input[name*=query i]",
    "input[name*=term i]",
    "input[placeholder*=such i]",
    "input[placeholder*=search i]",
    "input[aria-label*=such i]",
    "input[aria-label*=search i]",
]
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# JS: jeden Link auf die kleinste sinnvolle "Karte" hochklettern und Text sammeln.
EXTRACT_JS = r"""
(maxCards) => {
  const out = [];
  const seen = new Set();
  const host = location.hostname.replace(/^www\./, '');
  for (const a of document.querySelectorAll('a[href]')) {
    let href;
    try { href = new URL(a.getAttribute('href'), location.href).href.split('#')[0]; } catch { continue; }
    if (!href.startsWith('http')) continue;
    if (!new URL(href).hostname.replace(/^www\./, '').endsWith(host.split('.').slice(-2).join('.'))) continue;
    if (seen.has(href)) continue;
    let card = a, best = a;
    for (let i = 0; i < 7 && card.parentElement; i++) {
      const p = card.parentElement;
      const t = (p.innerText || '').trim();
      const links = new Set([...p.querySelectorAll('a[href]')].map(x => x.href.split('#')[0]));
      const curLen = (best.innerText || '').trim().length;
      if (t.length > 900 || links.size > 3) break;
      if (links.size > 1 && curLen >= 40) break;  // nächste Ebene enthält schon ein anderes Los
      card = p; best = p;
    }
    const text = (best.innerText || a.innerText || '').replace(/\s+\n/g, '\n').replace(/\n{2,}/g, '\n').trim();
    const img = best.querySelector('img');
    const alt = img ? (img.getAttribute('alt') || '') : '';
    if (text.length < 20) continue;
    seen.add(href);
    out.push({ url: href, text: text.slice(0, 900), img_alt: alt.slice(0, 200) });
    if (out.length >= maxCards) break;
  }
  return out;
}
"""


async def accept_cookies(page) -> None:
    for _ in range(2):
        try:
            btn = page.get_by_role("button", name=COOKIE_RE).first
            if await btn.is_visible(timeout=1500):
                await btn.click(timeout=3000)
                await page.wait_for_timeout(800)
                return
        except Exception:
            pass
        await page.wait_for_timeout(1000)


async def settle(page, scrolls: int) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=12000)
    except PWTimeout:
        pass
    for _ in range(scrolls):
        await page.mouse.wheel(0, 2500)
        await page.wait_for_timeout(900)


async def search_via_form(page, home: str, q: str) -> bool:
    await page.goto(home, wait_until="domcontentloaded", timeout=45000)
    await accept_cookies(page)
    await page.wait_for_timeout(1500)
    for sel in SEARCH_SELECTORS:
        loc = page.locator(sel)
        n = await loc.count()
        for i in range(n):
            el = loc.nth(i)
            try:
                if not await el.is_visible():
                    continue
                await el.click(timeout=3000)
                await el.fill(q, timeout=3000)
                await el.press("Enter")
                await page.wait_for_timeout(2500)
                return True
            except Exception:
                continue
    # Manche Seiten zeigen das Suchfeld erst nach Klick auf ein Such-Icon
    try:
        icon = page.get_by_role("button", name=re.compile(r"such|search", re.I)).first
        if await icon.is_visible(timeout=1500):
            await icon.click()
            await page.wait_for_timeout(1000)
            for sel in SEARCH_SELECTORS:
                el = page.locator(sel).first
                if await el.count() and await el.is_visible():
                    await el.fill(q)
                    await el.press("Enter")
                    await page.wait_for_timeout(2500)
                    return True
    except Exception:
        pass
    return False


async def scrape_site(browser, site: dict, cfg: dict) -> tuple[list[dict], dict]:
    name = site["name"]
    ctx = await browser.new_context(user_agent=UA, locale="de-AT", viewport={"width": 1440, "height": 2000})
    page = await ctx.new_page()
    json_calls: list[dict] = []

    async def on_response(resp):
        try:
            ct = resp.headers.get("content-type", "")
            if "json" in ct and len(json_calls) < 40:
                body = await resp.text()
                req = resp.request
                json_calls.append({
                    "url": resp.url,
                    "method": req.method,
                    "post_data": (req.post_data or "")[:1500],
                    "status": resp.status,
                    "body_head": body[:1500],
                })
        except Exception:
            pass

    page.on("response", on_response)
    diag = {"site": name, "keywords": {}, "errors": []}
    cards: list[dict] = []
    for i, q in enumerate(cfg["keywords"]):
        try:
            if site.get("search_url"):
                await page.goto(site["search_url"].replace("{q}", quote_plus(q)),
                                wait_until="domcontentloaded", timeout=45000)
                if i == 0:
                    await accept_cookies(page)
                ok = True
            else:
                ok = await search_via_form(page, site["home"], q)
            await settle(page, cfg.get("scrolls", 4))
            found = await page.evaluate(EXTRACT_JS, cfg.get("max_cards", 60))
            if site.get("lot_pattern"):
                pat = re.compile(site["lot_pattern"])
                found = [c for c in found if pat.search(c["url"])]
            for c in found:
                c.update(site=name, keyword=q)
            cards.extend(found)
            diag["keywords"][q] = {"form_ok": ok, "final_url": page.url, "cards": len(found)}
            if i == 0:
                DEBUG.mkdir(exist_ok=True)
                await page.screenshot(path=str(DEBUG / f"{name}.png"), full_page=False)
                txt = await page.evaluate("document.body ? document.body.innerText : ''")
                (DEBUG / f"{name}.txt").write_text(txt[:8000], encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            diag["errors"].append(f"{q}: {type(e).__name__}: {str(e)[:200]}")
        await page.wait_for_timeout(1200)  # höflich bleiben
    diag["json_calls"] = json_calls
    await ctx.close()
    return cards, diag


TB_API = "https://shared-api.tbauctions.com/storefront-search/v2/search"


async def scrape_tb(browser, site: dict, cfg: dict) -> tuple[list[dict], dict]:
    """Troostwijk + Surplex (TB Auctions) über die öffentliche Such-API."""
    ctx = await browser.new_context(user_agent=UA)
    diag = {"site": site["name"], "keywords": {}, "errors": [], "json_calls": []}
    cards = []
    for q in cfg["keywords"]:
        n = 0
        for platform in site.get("platforms", ["TWK"]):
            try:
                r = await ctx.request.get(TB_API, params={
                    "pageNumber": 1, "pageSize": 100, "query": q, "platform": platform},
                    headers={"accept": "application/json", "Search-Language": "de"})
                if r.status != 200:
                    diag["errors"].append(f"{q}/{platform}: HTTP {r.status}")
                    continue
                for it in (await r.json()).get("results", []):
                    loc = it.get("location") or {}
                    bid = (it.get("currentBidAmountInCents") or 0) / 100
                    end = dt.datetime.fromtimestamp(it["endDate"], dt.timezone.utc).isoformat() if it.get("endDate") else ""
                    cards.append({
                        "site": "troostwijk/surplex", "keyword": q,
                        "url": f"https://www.troostwijkauctions.com/de/l/{it.get('slug')}",
                        "text": (f"{it.get('title')}\nStandort: {loc.get('city','')} ({(loc.get('countryCode') or '').upper()})"
                                 f"\nAktuelles Gebot: {bid:.0f} {it.get('currency','EUR')} · Gebote: {it.get('bidsCount')}"
                                 f"\nEnde: {end} · Status: {it.get('biddingStatus')}"),
                        "img_alt": "", "end": end,
                    })
                    n += 1
            except Exception as e:  # noqa: BLE001
                diag["errors"].append(f"{q}/{platform}: {type(e).__name__}: {str(e)[:150]}")
        diag["keywords"][q] = {"cards": n}
        await asyncio.sleep(0.5)
    await ctx.close()
    return cards, diag


async def scrape_aurena(browser, site: dict, cfg: dict) -> tuple[list[dict], dict]:
    """Aurena: Suchseite rendern, Los-Karten lesen. Los-IDs kommen aus dem
    'versions'-Request, den die Seite für die angezeigten Lose (in Reihenfolge) schickt."""
    ctx = await browser.new_context(user_agent=UA, locale="de-AT", viewport={"width": 1440, "height": 2200})
    page = await ctx.new_page()
    diag = {"site": "aurena", "keywords": {}, "errors": [], "json_calls": []}
    cards = []
    captured: list[list[int]] = []

    def on_request(req):
        try:
            if "aurena.services/api" in req.url and req.method == "POST" and '"versions"' in (req.post_data or ""):
                captured.append([v["id"] for v in json.loads(req.post_data)["versions"]])
        except Exception:
            pass
    page.on("request", on_request)

    for i, q in enumerate(cfg["keywords"]):
        search = f"https://www.aurena.at/s?keywords={quote_plus(q)}&pagesize=96"
        captured.clear()
        try:
            await page.goto(search, wait_until="domcontentloaded", timeout=45000)
            if i == 0:
                await accept_cookies(page)
            try:
                await page.wait_for_selector(".lot-gallery-container", timeout=15000)
            except PWTimeout:
                pass
            await settle(page, 2)
            head = await page.evaluate("document.body.innerText.slice(0, 1500)")
            m = re.search(r"konnten wir ([\d.]+) Posten", head)
            total = int(m.group(1).replace(".", "")) if m else 0
            texts = await page.evaluate("""[...document.querySelectorAll('.lot-gallery-container')]
                .map(e => (e.innerText||'').split('\\n').map(s=>s.trim()).filter(Boolean).join(' | '))""")
            texts = [t for t in texts if len(t) > 15]
            ids = next((c for c in captured if len(c) == len(texts)), None) if total else None
            for k, t in enumerate(texts[: cfg.get("max_cards", 60)]):
                url = f"https://www.aurena.at/posten/{ids[k]}" if ids else f"{search}#los-{k}"
                cards.append({"site": "aurena", "keyword": q, "url": url,
                              "text": f"{t}\nSuche: {search}", "img_alt": ""})
            diag["keywords"][q] = {"cards": len(texts), "total_reported": total, "ids_matched": bool(ids)}
        except Exception as e:  # noqa: BLE001
            diag["errors"].append(f"{q}: {type(e).__name__}: {str(e)[:200]}")
        await page.wait_for_timeout(1000)
    await ctx.close()
    return cards, diag


ADAPTERS = {"tb": scrape_tb, "aurena": scrape_aurena}


def drop_navigation(cards: list[dict], n_keywords: int) -> list[dict]:
    """Links, die bei fast jedem Suchbegriff auftauchen, sind Navigation/Footer."""
    per_site = defaultdict(Counter)
    for c in cards:
        per_site[c["site"]][c["url"]] += 1
    out = []
    for c in cards:
        if per_site[c["site"]][c["url"]] > max(3, n_keywords * 0.5):
            continue
        out.append(c)
    return out


def merge(cards: list[dict]) -> dict[str, dict]:
    lots: dict[str, dict] = {}
    for c in cards:
        lot = lots.setdefault(c["url"], {
            "url": c["url"], "site": c["site"], "text": c["text"],
            "img_alt": c.get("img_alt", ""), "keywords": [],
        })
        if c["keyword"] not in lot["keywords"]:
            lot["keywords"].append(c["keyword"])
        if len(c["text"]) > len(lot["text"]):
            lot["text"] = c["text"]
    return lots


async def main() -> int:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    only = set(sys.argv[1:])
    sites = [s for s in cfg["sites"] if not only or s["name"] in only]
    DATA.mkdir(exist_ok=True)
    DEBUG.mkdir(exist_ok=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        results = await asyncio.gather(*(ADAPTERS.get(s.get("adapter"), scrape_site)(browser, s, cfg) for s in sites))
        await browser.close()

    all_cards, diags = [], []
    for cards, diag in results:
        all_cards.extend(cards)
        diags.append(diag)
    all_cards = drop_navigation(all_cards, len(cfg["keywords"]))
    lots = merge(all_cards)

    seen_path = DATA / "seen.json"
    seen = json.loads(seen_path.read_text(encoding="utf-8")) if seen_path.exists() else {}
    new = [l for u, l in lots.items() if u not in seen]
    for u in lots:
        seen.setdefault(u, NOW)

    for l in lots.values():
        l["first_seen"] = seen[l["url"]]
    (DATA / "latest.json").write_text(json.dumps(
        {"generated": NOW, "count": len(lots), "lots": list(lots.values())}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    (DATA / "new.json").write_text(json.dumps(
        {"generated": NOW, "count": len(new), "lots": new}, ensure_ascii=False, indent=1), encoding="utf-8")
    seen_path.write_text(json.dumps(seen, ensure_ascii=False, indent=0), encoding="utf-8")
    (DEBUG / "discovery.json").write_text(json.dumps(diags, ensure_ascii=False, indent=1), encoding="utf-8")

    summary = {d["site"]: {"cards": sum(k["cards"] for k in d["keywords"].values()),
                           "errors": len(d["errors"])} for d in diags}
    (DATA / "status.json").write_text(json.dumps(
        {"generated": NOW, "lots": len(lots), "new": len(new), "sites": summary}, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))
    print(f"lots={len(lots)} new={len(new)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
