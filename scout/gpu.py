"""RTX-5090-Wächter: sucht auf Kleinanzeigen-/Gebrauchtplattformen und Auktionen nach einer
bestimmten GPU und meldet NEUE Angebote sofort per Telegram.

Quellen: willhaben.at, kleinanzeigen.de, Troostwijk/Surplex (API), Aurena, Zoll-Auktion.
Konfiguration: Abschnitt gpu_hunt in config.yaml. Zustand: data/gpu_seen.json.
Env: TELEGRAM_BOT_TOKEN (Secret), TELEGRAM_CHAT_ID (Secret, sonst digest.chat_id), DRY_RUN=1
"""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from urllib.parse import quote_plus

import yaml
from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).parent))
from scrape import UA, accept_cookies, EXTRACT_JS, TB_API  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CFG = yaml.safe_load(open(ROOT / "config.yaml", encoding="utf-8"))
H = CFG["gpu_hunt"]
NOW = dt.datetime.now(dt.timezone.utc)
DRY = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")
FULL = os.environ.get("FULL", "").strip().lower() in ("1", "true", "yes")   # komplette Übersicht schicken
MUST = re.compile(H.get("must", r"5090"), re.I)
EXCLUDE = re.compile(H.get("exclude", r"$^"), re.I)


def price_of(text: str) -> float | None:
    """Erster Betrag direkt neben € / EUR (sonst None – Modellnummern wie 5090 nicht als Preis lesen)."""
    m = re.search(r"€\s*(\d{1,3}(?:\.\d{3})+|\d{2,5})|(\d{1,3}(?:\.\d{3})+|\d{2,5})(?:,\d{2})?\s*(?:€|EUR)", text or "")
    if not m:
        return None
    return float(next(g for g in m.groups() if g).replace(".", ""))


# ---------------------------------------------------------------- Quellen
async def willhaben(ctx, q):
    page = await ctx.new_page()
    url = f"https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz?keyword={quote_plus(q)}&rows=90&sort=1"
    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    ads = await page.evaluate("""() => {
      const n = document.querySelector('#__NEXT_DATA__'); if (!n) return [];
      const d = JSON.parse(n.textContent);
      const l = (((d.props||{}).pageProps||{}).searchResult||{}).advertSummaryList || {};
      return (l.advertSummary || []).map(a => {
        const at = {}; for (const x of (a.attributes||{}).attribute || []) at[x.name] = (x.values||[])[0];
        return {id: a.id, title: at.HEADING || a.description, price: at.PRICE, loc: at.LOCATION,
                body: at.BODY_DYN || '', url: 'https://www.willhaben.at/iad/' + (at.SEO_URL || ''),
                published: at.PUBLISHED_String || ''};
      });
    }""")
    await page.close()
    out = []
    for a in ads:
        out.append({"source": "willhaben", "id": f"wh:{a['id']}", "title": a["title"] or "",
                    "price": float(a["price"]) if a.get("price") else None, "location": a.get("loc") or "",
                    "text": f"{a['title']} {a.get('body', '')}", "url": a["url"]})
    return out


async def kleinanzeigen(ctx, q):
    page = await ctx.new_page()
    slug = re.sub(r"[^a-z0-9]+", "-", q.lower()).strip("-")
    await page.goto(f"https://www.kleinanzeigen.de/s-anzeige:angebote/sortierung:neuste/{slug}/k0",
                    wait_until="domcontentloaded", timeout=45000)
    await accept_cookies(page)
    try:
        btn = page.locator("#gdpr-banner-accept, button:has-text('Alle akzeptieren')").first
        if await btn.is_visible(timeout=3000):
            await btn.click()
            await page.wait_for_timeout(2000)
    except Exception:
        pass
    await page.wait_for_timeout(1500)
    cards = await page.evaluate(EXTRACT_JS, 80)
    await page.close()
    out = []
    for c in cards:
        m = re.search(r"/s-anzeige/[^/]+/(\d+)", c["url"])
        if not m:
            continue
        lines = [x.strip() for x in c["text"].split("\n") if x.strip()]
        cand = [x for x in lines if not re.match(r"^(\d{5}\s|heute|gestern|\d{2}\.\d{2}\.\d{4}|.*€|vb$|top$|versand|direkt kaufen)", x, re.I)]
        title = next((x for x in cand if MUST.search(x) and len(x) <= 140), cand[0] if cand else "")
        loc = next((x for x in lines if re.match(r"^\d{5}\s", x)), "")
        out.append({"source": "kleinanzeigen", "id": f"ka:{m.group(1)}", "title": title[:120],
                    "price": price_of(c["text"]), "location": loc, "text": c["text"], "url": c["url"]})
    return out


async def troostwijk(ctx, q):
    out = []
    for platform in ("TWK", "SPX"):
        r = await ctx.request.get(TB_API, params={"pageNumber": 1, "pageSize": 100, "query": q, "platform": platform},
                                  headers={"accept": "application/json", "Search-Language": "de"})
        if r.status != 200:
            continue
        for it in (await r.json()).get("results", []):
            loc = it.get("location") or {}
            out.append({"source": "troostwijk/surplex (Auktion)", "id": f"tb:{it['id']}", "title": it.get("title", ""),
                        "price": (it.get("currentBidAmountInCents") or 0) / 100,
                        "location": f"{loc.get('city', '')} ({(loc.get('countryCode') or '').upper()})",
                        "text": it.get("title", ""), "url": f"https://www.troostwijkauctions.com/de/l/{it.get('slug')}",
                        "auction": True})
    return out


async def aurena(ctx, q):
    page = await ctx.new_page()
    url = f"https://www.aurena.at/s?keywords={quote_plus(q)}&pagesize=96"
    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    await accept_cookies(page)
    try:
        await page.wait_for_selector(".lot-gallery-container", timeout=12000)
    except Exception:
        pass
    head = await page.evaluate("document.body.innerText.slice(0, 1500)")
    m = re.search(r"konnten wir ([\d.]+) Posten", head)
    total = int(m.group(1).replace(".", "")) if m else 0
    texts = await page.evaluate("""[...document.querySelectorAll('.lot-gallery-container')]
        .map(e => (e.innerText||'').split('\\n').map(s=>s.trim()).filter(Boolean).join(' | '))""")
    await page.close()
    out = []
    for k, t in enumerate([t for t in texts if len(t) > 15][:total]):
        out.append({"source": "aurena (Auktion)", "id": f"au:{t[:60]}", "title": t.split(" | ")[1] if " | " in t else t,
                    "price": price_of(t), "location": "", "text": t, "url": f"{url}#los-{k}", "auction": True})
    return out


async def zoll(ctx, q):
    page = await ctx.new_page()
    await page.goto(f"https://www.zoll-auktion.de/auktion/auktionsuebersicht.php?n2={quote_plus(q)}&n1%5B%5D=0&n0=search",
                    wait_until="domcontentloaded", timeout=45000)
    await accept_cookies(page)
    await page.wait_for_timeout(1500)
    cards = await page.evaluate(EXTRACT_JS, 60)
    await page.close()
    return [{"source": "zoll-auktion (Auktion)", "id": f"zo:{c['url']}", "title": c["text"].split("\n")[0][:120],
             "price": price_of(c["text"]), "location": "", "text": c["text"], "url": c["url"], "auction": True}
            for c in cards if "/auktion/produkt/" in c["url"]]


async def alternate(ctx, q, tld):
    out, seen = [], set()
    page = await ctx.new_page()
    for n in (1, 2, 3):
        await page.goto(f"https://www.alternate.{tld}/listing.xhtml?q={quote_plus(q)}&page={n}",
                        wait_until="domcontentloaded", timeout=45000)
        if n == 1:
            await accept_cookies(page)
        await page.wait_for_timeout(1500)
        cards = await page.evaluate(EXTRACT_JS, 60)
        got = 0
        for c in cards:
            m = re.search(r"/html/product/(\d+)", c["url"])
            if not m or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            got += 1
            lines = [x.strip() for x in c["text"].split("\n") if x.strip()]
            title = next((x for x in lines if MUST.search(x)), lines[0] if lines else "")
            avail = next((x for x in lines if re.search(r"verfügbar|lieferbar|lager|tage", x, re.I)), "")
            out.append({"source": f"alternate.{tld} (neu)", "id": f"alt:{tld}:{m.group(1)}", "title": title[:120],
                        "price": price_of(c["text"]), "location": avail[:40], "text": c["text"],
                        "url": c["url"], "retail": True})
        if not got:
            break
    await page.close()
    return out


async def alternate_at(ctx, q):
    return await alternate(ctx, q, "at")


async def alternate_de(ctx, q):
    return await alternate(ctx, q, "de")


async def caseking(ctx, q):
    page = await ctx.new_page()
    await page.goto(f"https://www.caseking.de/search?search={quote_plus(q)}", wait_until="domcontentloaded", timeout=45000)
    try:
        btn = page.get_by_role("button", name=re.compile(r"ich mag cookies|alle akzeptieren", re.I)).first
        if await btn.is_visible(timeout=4000):
            await btn.click()
            await page.wait_for_timeout(2500)
    except Exception:
        pass
    await page.wait_for_timeout(2000)
    cards = await page.evaluate(EXTRACT_JS, 80)
    await page.close()
    out = []
    for c in cards:
        if not MUST.search(c["text"]) or not price_of(c["text"]):
            continue
        lines = [x.strip() for x in c["text"].split("\n") if x.strip()]
        title = next((x for x in lines if MUST.search(x)), lines[0])
        out.append({"source": "caseking.de (neu)", "id": f"ck:{c['url']}", "title": title[:120],
                    "price": price_of(c["text"]), "location": "", "text": c["text"], "url": c["url"], "retail": True})
    return out


SOURCES = {"alternate_at": alternate_at, "alternate_de": alternate_de, "caseking": caseking,
           "willhaben": willhaben, "kleinanzeigen": kleinanzeigen, "troostwijk": troostwijk,
           "aurena": aurena, "zoll": zoll}


# ---------------------------------------------------------------- Filter / Telegram
def relevant(o) -> str | None:
    """None = behalten, sonst Grund für das Aussortieren."""
    if not MUST.search(o["title"]) and not MUST.search(o["text"][:300]):
        return "kein Treffer im Titel"
    if EXCLUDE.search(o["title"]):
        return "ausgeschlossen (Titel)"
    if not o.get("auction") and o.get("price") is not None and o["price"] < H.get("min_price", 0):
        return "Preis unplausibel niedrig"
    return None


def send(tok, chat, text):
    req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", method="POST",
                                 data=json.dumps({"chat_id": chat, "text": text, "parse_mode": "HTML",
                                                  "disable_web_page_preview": True}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        if not json.load(r).get("ok"):
            raise RuntimeError("Telegram-Fehler")


PC = re.compile(H.get("pc", r"gaming[- ]?pc|komplett|ryzen|core i\d|ddr5|tower|rechner"), re.I)


def line(o):
    is_pc = bool(PC.search(o["title"]))
    deal = (not o.get("auction") and not is_pc and o.get("price") and o["price"] <= H.get("deal_price", 0))
    p = f"{o['price']:,.0f} €".replace(",", ".") if o.get("price") else "Preis ?"
    if o.get("auction"):
        p = "Gebot " + p
    loc = f" · {html.escape(o['location'])}" if o.get("location") else ""
    tag = " · <i>Komplett-PC</i>" if is_pc else ""
    return (f"{'🔥 ' if deal else '• '}<a href=\"{html.escape(o['url'])}\">{html.escape(o['title'][:90])}</a>\n"
            f"   <b>{p}</b> · {html.escape(o['source'])}{loc}{tag}")


def chunks(lines, head, limit=3800):
    out, cur = [], head
    for l in lines:
        if len(cur) + len(l) + 2 > limit:
            out.append(cur)
            cur = head + " (Forts.)"
        cur += "\n\n" + l
    out.append(cur)
    return out


async def main():
    q = H["query"]
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(user_agent=UA, locale="de-AT", viewport={"width": 1440, "height": 2200})
        results, errors = [], {}
        for name in H.get("sources", list(SOURCES)):
            try:
                got = await SOURCES[name](ctx, q)
                results += got
                errors[name] = f"{len(got)} Treffer"
            except Exception as e:  # noqa: BLE001
                errors[name] = f"Fehler: {type(e).__name__}: {str(e)[:120]}"
        await browser.close()
    print(json.dumps(errors, ensure_ascii=False, indent=1))

    kept, dropped = [], {}
    for o in results:
        why = relevant(o)
        if why:
            dropped[why] = dropped.get(why, 0) + 1
        else:
            kept.append(o)
    print(f"gesamt={len(results)} relevant={len(kept)} aussortiert={dropped}")

    # Neuware separat: nur interessant, wenn ungefähr auf Gebraucht-Niveau (Median der Gebraucht-Karten)
    retail = [o for o in kept if o.get("retail") and o.get("price")]
    kept = [o for o in kept if not o.get("retail")]
    used = sorted(o["price"] for o in kept
                  if o.get("price") and not o.get("auction") and not PC.search(o["title"]))
    median = used[len(used) // 2] if len(used) % 2 else (sum(used[len(used) // 2 - 1:len(used) // 2 + 1]) / 2 if used else 0)
    limit = median * (1 + H.get("retail_tolerance", 0.05)) if median else 0
    retail_ok = sorted([o for o in retail if not PC.search(o["title"]) and o["price"] <= limit], key=lambda o: o["price"])
    print(f"Gebraucht-Median={median:.0f} € (n={len(used)}), Neuware-Grenze={limit:.0f} €, "
          f"Neuware gesamt={len(retail)}, davon im Rahmen={len(retail_ok)}")

    seen_p = DATA / "gpu_seen.json"
    seen = json.loads(seen_p.read_text(encoding="utf-8")) if seen_p.exists() else {}
    first_run = not seen
    new = [o for o in kept if o["id"] not in seen]
    new.sort(key=lambda o: (o.get("auction", False), o.get("price") or 1e9))
    rseen_p = DATA / "gpu_retail_seen.json"
    rseen = json.loads(rseen_p.read_text(encoding="utf-8")) if rseen_p.exists() else {}
    # melden, wenn neu im Rahmen oder seit letzter Meldung um mind. 3 % billiger
    retail_new = [o for o in retail_ok if o["id"] not in rseen or o["price"] <= rseen[o["id"]]["price"] * 0.97]

    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = (os.environ.get("TELEGRAM_CHAT_ID", "") or str(CFG.get("digest", {}).get("chat_id", ""))).strip()
    msgs = []
    if first_run and kept:
        top = sorted(kept, key=lambda o: (o.get("auction", False), o.get("price") or 1e9))[: H.get("first_run_top", 15)]
        msgs = chunks([line(o) for o in top],
                      f"🎯 <b>{html.escape(q)}-Wächter aktiv</b>\n{len(kept)} aktuelle Angebote, hier die günstigsten. "
                      f"Ab jetzt melde ich nur noch neue (🔥 = bis {H.get('deal_price')} €).")
    elif FULL and kept:
        top = sorted(kept, key=lambda o: (o.get("auction", False), o.get("price") or 1e9))[: H.get("first_run_top", 15)]
        msgs = chunks([line(o) for o in top],
                      f"🎯 <b>{html.escape(q)}: Übersicht</b>\n{len(kept)} Gebraucht-/Auktionsangebote, die günstigsten:")
    elif new:
        msgs = chunks([line(o) for o in new], f"🎯 <b>{len(new)} neue {html.escape(q)}-Angebote</b>")
    rlist = retail_ok if (FULL or first_run) else retail_new
    if median:
        fmt = lambda v: f"{v:,.0f}".replace(",", ".")
        if rlist:
            msgs += chunks([line(o) for o in rlist],
                           f"🛒 <b>Neuware im Handel auf Gebraucht-Niveau</b>\nMedian gebraucht {fmt(median)} € "
                           f"(aus {len(used)} Karten) → Neuware bis {fmt(limit)} €:")
        elif FULL:
            cheapest = min((o for o in retail if not PC.search(o["title"])), key=lambda o: o["price"], default=None)
            msgs.append(f"🛒 <b>Neuware:</b> nichts bis {fmt(limit)} € (Median gebraucht {fmt(median)} €)."
                        + (f"\nGünstigste neue Karte: {line(cheapest)}" if cheapest else ""))
    broken = [f"{k}: {v}" for k, v in errors.items() if v.startswith("Fehler") or v.startswith("0 ")]
    status = {"generated": NOW.isoformat(timespec="seconds"), "sources": errors, "relevant": len(kept),
              "used_median": median, "retail_limit": limit, "retail_total": len(retail), "retail_ok": len(retail_ok),
              "new": len(new), "dropped": dropped}
    (DATA / "gpu_status.json").write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")
    (ROOT / "debug").mkdir(exist_ok=True)
    (ROOT / "debug" / "gpu_preview.txt").write_text(
        "\n\n=====\n\n".join(msgs) + "\n\n----- alle relevanten -----\n" +
        "\n".join(f"{o.get('price')} | {o['source']} | {o['title'][:80]} | {o['url']}" for o in kept + retail) +
        "\n\n----- aussortiert (Beispiele) -----\n" +
        "\n".join(f"{relevant(o)} | {o['source']} | {o['title'][:80]} | {o.get('price')}" for o in results if relevant(o))[:6000],
        encoding="utf-8")
    for m in msgs:
        print(m, "\n-----")
    if DRY:
        print("DRY_RUN – nichts gesendet.")
        return
    if msgs:
        if not (tok and chat):
            raise SystemExit("TELEGRAM_BOT_TOKEN oder Chat-ID fehlt")
        for m in msgs:
            send(tok, chat, m)
    for o in retail_ok:
        rseen[o["id"]] = {"price": o["price"], "date": NOW.date().isoformat()}
    rseen_p.write_text(json.dumps(rseen, indent=0), encoding="utf-8")
    for o in kept:
        seen.setdefault(o["id"], NOW.date().isoformat())
    old = (NOW.date() - dt.timedelta(days=90)).isoformat()
    seen_p.write_text(json.dumps({k: v for k, v in seen.items() if v >= old}, indent=0), encoding="utf-8")
    if broken:
        print("::warning::" + "; ".join(broken))


if __name__ == "__main__":
    asyncio.run(main())
