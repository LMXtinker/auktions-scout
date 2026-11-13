"""MacBook-Wächter: sucht ein MacBook Air 15" mit (mind.) 24 GB RAM und 1 TB SSD auf Gebraucht-,
Refurbished-, Auktions- und Neuware-Plattformen und meldet NEUE Angebote per Telegram.

Quellen: willhaben.at, kleinanzeigen.de, ebay.de, Troostwijk/Surplex (API), Apple Refurbished AT/DE, geizhals.at.
Ablauf: Suche -> Vorfilter (Air + 15") -> bei Bedarf Detailseite öffnen -> Spezifikation per Regex prüfen
        -> neue Treffer kurz von Gemini gegenprüfen/kommentieren (optional) -> Telegram.
Konfiguration: Abschnitt mac_hunt in config.yaml. Zustand: data/mac_seen.json, data/mac_status.json.
Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (Secrets), GEMINI_API_KEY (optional), DRY_RUN=1, FULL=1
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
DATA, DEBUG = ROOT / "data", ROOT / "debug"
CFG = yaml.safe_load(open(ROOT / "config.yaml", encoding="utf-8"))
H = CFG["mac_hunt"]
NOW = dt.datetime.now(dt.timezone.utc)
flag = lambda k: os.environ.get(k, "").strip().lower() in ("1", "true", "yes")
DRY, FULL = flag("DRY_RUN"), flag("FULL")

AIR = re.compile(r"mac\s*book\s*air", re.I)
SIZE15 = re.compile(r"\b15(?:[.,]3)?\s*(?:\"|”|″|''|'|-?zoll|-?inch|in\b|z\b)|air\s*\(?15\b|\b15\s*m[2-9]\b|\bm[2-9]\s*15\b|15-in", re.I)
SIZE13 = re.compile(r"\b13(?:[.,]\d)?\s*(?:\"|”|″|''|-?zoll|-?inch)|air\s*13\b", re.I)
RAM = re.compile(H.get("ram", r"\b(24|32)\s*gb\b(?!\s*(ssd|speicher|festplatte))|\b(24|32)\s*gb\s*(ram|arbeitsspeicher|unified|memory)"), re.I)
RAM_LOW = re.compile(r"\b(8|16)\s*gb\s*(ram|arbeitsspeicher|unified|memory|gemeinsamer)", re.I)
SSD = re.compile(H.get("ssd", r"\b[12]\s*tb\b|\b(1000|1024|2000|2048)\s*gb\b"), re.I)
EXCLUDE = re.compile(H.get("exclude", r"$^"), re.I)
CHIP = re.compile(r"\bM([2-9])(?:\s*(?:chip|8c|10c|8-core|10-core|\b))", re.I)


def price_of(text: str) -> float | None:
    m = re.search(r"€\s*(\d{1,3}(?:[.\s]\d{3})+|\d{2,5})|(\d{1,3}(?:[.\s]\d{3})+|\d{2,5})(?:,\d{2}|,-)?\s*(?:€|EUR)", text or "")
    if not m:
        return None
    return float(re.sub(r"[.\s]", "", next(g for g in m.groups() if g)))


def item(source, id_, title, price, url, text="", country="", location="", kind="gebraucht"):
    return {"source": source, "id": id_, "title": (title or "").strip()[:140], "price": price, "url": url,
            "text": text or title or "", "country": country, "location": location or "", "kind": kind}


# ---------------------------------------------------------------- Quellen
async def willhaben(ctx, q):
    page = await ctx.new_page()
    await page.goto(f"https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz?keyword={quote_plus(q)}&rows=90&sort=1",
                    wait_until="domcontentloaded", timeout=45000)
    ads = await page.evaluate("""() => {
      const n = document.querySelector('#__NEXT_DATA__'); if (!n) return [];
      const d = JSON.parse(n.textContent);
      const l = (((d.props||{}).pageProps||{}).searchResult||{}).advertSummaryList || {};
      return (l.advertSummary || []).map(a => {
        const at = {}; for (const x of (a.attributes||{}).attribute || []) at[x.name] = (x.values||[])[0];
        return {id: a.id, title: at.HEADING || a.description, price: at.PRICE, loc: at.LOCATION,
                body: at.BODY_DYN || '', url: 'https://www.willhaben.at/iad/' + (at.SEO_URL || ''),
                isPrivate: at.ISPRIVATE};
      });
    }""")
    await page.close()
    return [item("willhaben", f"wh:{a['id']}", a["title"], float(a["price"]) if a.get("price") else None, a["url"],
                 f"{a['title']} {a.get('body', '')}" + (" [gewerblich]" if a.get("isPrivate") == "0" else ""),
                 "AT", a.get("loc")) for a in ads]


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
        title = next((x for x in lines if AIR.search(x) and len(x) <= 140), lines[0] if lines else "")
        loc = next((x for x in lines if re.match(r"^\d{5}\s", x)), "")
        out.append(item("kleinanzeigen", f"ka:{m.group(1)}", title, price_of(c["text"]), c["url"], c["text"], "DE", loc))
    return out


EBAY_COUNTRY = {"deutschland": "DE", "österreich": "AT", "polen": "PL", "niederlande": "NL", "italien": "IT",
                "frankreich": "FR", "spanien": "ES", "belgien": "BE", "tschechien": "CZ", "großbritannien": "UK",
                "vereinigtes königreich": "UK", "usa": "US", "vereinigte staaten": "US", "china": "CN", "japan": "JP",
                "schweiz": "CH", "dänemark": "DK", "irland": "IE", "litauen": "LT", "ungarn": "HU", "slowakei": "SK"}


async def ebay(ctx, q):
    page = await ctx.new_page()
    await page.goto(f"https://www.ebay.de/sch/i.html?_nkw={quote_plus(q)}&_sop=10&_ipg=120&LH_ItemCondition=1000|1500|2000|2010|2020|2030|2500|3000",
                    wait_until="domcontentloaded", timeout=45000)
    await accept_cookies(page)
    await page.wait_for_timeout(2000)
    cards = await page.evaluate(EXTRACT_JS, 150)
    if not any("/itm/" in c["url"] for c in cards):
        DIAG["ebay"] = (await page.title())[:80] + " | " + (await page.evaluate("document.body.innerText.slice(0,300)"))
    await page.close()
    out = []
    for c in cards:
        m = re.search(r"/itm/(\d{9,})", c["url"])
        if not m or m.group(1) == "123456":
            continue
        lines = [x.strip() for x in c["text"].split("\n") if x.strip() and not re.match(r"^neues angebot$", x.strip(), re.I)]
        title = next((x for x in lines if AIR.search(x)), lines[0] if lines else "")
        cm = re.search(r"aus ([A-Za-zÄÖÜäöüß ]+)", c["text"])
        country = EBAY_COUNTRY.get(cm.group(1).strip().lower(), cm.group(1).strip()) if cm else "DE"
        auction = bool(re.search(r"\bGebot|Gebote\b", c["text"])) and not re.search(r"sofort-?kaufen", c["text"], re.I)
        out.append(item("ebay.de" + (" (Auktion)" if auction else ""), f"eb:{m.group(1)}", title, price_of(c["text"]),
                        f"https://www.ebay.de/itm/{m.group(1)}", c["text"], country))
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
            out.append(item("troostwijk/surplex (Auktion)", f"tb:{it['id']}", it.get("title", ""),
                            (it.get("currentBidAmountInCents") or 0) / 100,
                            f"https://www.troostwijkauctions.com/de/l/{it.get('slug')}", it.get("title", ""),
                            (loc.get("countryCode") or "").upper(), loc.get("city", ""), "auktion"))
    return out


async def apple_refurb(ctx, cc):
    page = await ctx.new_page()
    await page.goto(f"https://www.apple.com/{cc.lower()}/shop/refurbished/mac/macbook-air",
                    wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(2500)
    cards = await page.evaluate(EXTRACT_JS, 200)
    await page.close()
    out = []
    for c in cards:
        if "/shop/product/" not in c["url"]:
            continue
        title = c["text"].split("\n")[0]
        out.append(item(f"Apple Refurbished {cc}", f"ap:{cc}:{c['url'].split('/shop/product/')[1].split('/')[0]}",
                        title, price_of(c["text"]), c["url"], c["text"], cc, "", "refurbished"))
    return out


async def apple_at(ctx, q):
    return await apple_refurb(ctx, "AT")


async def apple_de(ctx, q):
    return await apple_refurb(ctx, "DE")


async def geizhals(ctx, q):
    page = await ctx.new_page()
    await page.goto(f"https://geizhals.at/?fs={quote_plus(q)}&hloc=at&hloc=de&sort=p",
                    wait_until="domcontentloaded", timeout=45000)
    await accept_cookies(page)
    await page.wait_for_timeout(2000)
    cards = await page.evaluate(EXTRACT_JS, 80)
    await page.close()
    out = []
    for c in cards:
        if not re.search(r"geizhals\.at/[^?]*-a\d+\.html", c["url"]):
            continue
        lines = [x.strip() for x in c["text"].split("\n") if x.strip()]
        title = next((x for x in lines if AIR.search(x)), lines[0] if lines else "")
        out.append(item("geizhals.at (neu, Händler AT/DE)", f"gh:{c['url'].split('?')[0]}", title,
                        price_of(c["text"]), c["url"].split("?")[0], c["text"], "AT/DE", "", "neu"))
    return out


async def idealo(ctx, q):
    page = await ctx.new_page()
    await page.goto(f"https://www.idealo.de/preisvergleich/MainSearchProductCategory.html?q={quote_plus(q)}",
                    wait_until="domcontentloaded", timeout=45000)
    await accept_cookies(page)
    await page.wait_for_timeout(2500)
    cards = await page.evaluate(EXTRACT_JS, 80)
    if not any("/preisvergleich/OffersOfProduct/" in c["url"] for c in cards):
        DIAG["idealo"] = (await page.title())[:80] + " | " + (await page.evaluate("document.body.innerText.slice(0,200)"))
    await page.close()
    out = []
    for c in cards:
        m = re.search(r"/preisvergleich/OffersOfProduct/(\d+)", c["url"])
        if not m:
            continue
        lines = [x.strip() for x in c["text"].split("\n") if x.strip()]
        title = next((x for x in lines if AIR.search(x)), lines[0] if lines else "")
        out.append(item("idealo.de (neu, Händler DE)", f"id:{m.group(1)}", title, price_of(c["text"]),
                        c["url"].split("?")[0], c["text"], "DE", "", "neu"))
    return out


SOURCES = {"idealo": idealo, "willhaben": willhaben, "kleinanzeigen": kleinanzeigen, "ebay": ebay, "troostwijk": troostwijk,
           "apple_at": apple_at, "apple_de": apple_de, "geizhals": geizhals}
DIAG = {}
PER_QUERY = {"willhaben", "kleinanzeigen", "ebay", "troostwijk", "geizhals", "idealo"}   # Apple: eine Seite reicht


# ---------------------------------------------------------------- Filter
def candidate(o) -> str | None:
    """Grobfilter: MacBook Air 15". None = Kandidat, sonst Grund."""
    t = o["title"] + " " + o["text"][:400]
    if not AIR.search(t):
        return "kein MacBook Air"
    if EXCLUDE.search(o["title"]):
        return "ausgeschlossen (Titel)"
    if SIZE13.search(o["title"]) and not SIZE15.search(o["title"]):
        return "13 Zoll"
    if not SIZE15.search(t) and not re.search(r"\b15\b", o["title"]):
        return "nicht 15 Zoll" if o.get("detail") else "Größe unklar"
    if o.get("price") is not None and o["kind"] != "auktion" and o["price"] < H.get("min_price", 0):
        return "Preis unplausibel niedrig"
    if o["kind"] == "neu" and not o.get("price"):
        return "Neuware ohne Preis (nicht lieferbar)"
    if o.get("price") and o["price"] > H.get("max_price", 1e9):
        return "zu teuer"
    return None


def spec(o) -> str | None:
    """Feinfilter Spezifikation. None = Treffer, 'unklar' = Detailseite nötig, sonst Grund."""
    t = o["title"] + " " + o["text"]
    has_ram, has_ssd = bool(RAM.search(t)), bool(SSD.search(t))
    if has_ram and has_ssd:
        return None
    if not has_ram and re.search(r"\b(8|16)\s*gb\b", o["title"], re.I):
        return "zu wenig RAM (Titel)"
    if not has_ssd and re.search(r"\b(128|256|512)\s*gb\b", o["title"], re.I):
        return "zu wenig SSD (Titel)"
    if not o.get("detail"):
        return "unklar"
    if RAM_LOW.search(t) and not has_ram:
        return "zu wenig RAM"
    return "RAM fehlt" if not has_ram else "SSD fehlt"


DETAIL_SEL = {"kleinanzeigen": "#viewad-title, #viewad-price, #viewad-details, #viewad-description-text",
              "willhaben": "h1, [data-testid*=attribute], [data-testid*=description], [data-testid*=price]"}
CUT = re.compile(r"Das könnte (dir|Ihnen|Sie) auch gefallen|Ähnliche Anzeigen|Weitere Anzeigen|Anzeigen, die|"
                 r"Andere (Nutzer|Kunden)|Empfohlen für|Mehr von diesem|Zuletzt angesehen|Passendes Zubehör", re.I)


async def fetch_details(ctx, items):
    """Detailseite öffnen und nur den Anzeigen-/Produktteil behalten (keine ähnlichen Anzeigen, Navigation, Zubehör)."""
    page = await ctx.new_page()
    for o in items:
        try:
            await page.goto(o["url"], wait_until="domcontentloaded", timeout=30000)
            await accept_cookies(page)
            await page.wait_for_timeout(1200)
            body = ""
            sel = DETAIL_SEL.get(o["source"])
            if sel:
                body = await page.evaluate("s => [...document.querySelectorAll(s)].map(e => e.innerText).join('\\n')", sel)
            if len(body) < 80:
                body = await page.evaluate("document.body.innerText")
                key = o["title"][:25]
                i = body.find(key) if len(key) > 10 else -1
                body = body[i:] if i >= 0 else body
            m = CUT.search(body)
            body = body[: m.start()] if m else body
            o["text"] = (o["text"][:600] if o["source"] != "willhaben" else o["text"]) + "\n" + body[:5000]
            if o["source"] == "willhaben" and re.search(r"Firmenangebot|Händler|gewerblich", body, re.I):
                o["text"] += " [gewerblich]"
        except Exception as e:  # noqa: BLE001
            o["detail_error"] = str(e)[:80]
        o["detail"] = True
    await page.close()


def evidence(o, rx):
    t = re.sub(r"\s+", " ", o["title"] + " " + o["text"])
    m = rx.search(t)
    return t[max(0, m.start() - 50): m.end() + 30] if m else "-"


# ---------------------------------------------------------------- Gemini (optional)
def gemini_check(items):
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key or not items:
        return {}
    from digest import gemini_models, post_json  # noqa: E402
    lines = [json.dumps({"id": i, "source": o["source"], "price": o.get("price"), "country": o["country"],
                         "text": re.sub(r"\s+", " ", o["title"] + " | " + o["text"])[:3000]}, ensure_ascii=False)
             for i, o in enumerate(items)]
    prompt = f"""Prüfe Angebote für Lucas (Freiberufler in Wien, hat UID-Nummer) – gesucht: Apple MacBook Air 15 Zoll
mit mindestens 24 GB RAM und mindestens 1 TB SSD (Chip egal, Tastaturlayout egal).
Je Angebot:
- ok: true nur, wenn das ANGEBOTENE Gerät laut Text wirklich MacBook Air 15" mit >=24 GB RAM und >=1 TB SSD ist
  (false bei Gesuchen, Zubehör, defekt/iCloud-gesperrt, nur Vergleichswerten, Mehrfach-Konfigurationen ohne passende, 13 Zoll).
- chip: z. B. "M2", "M3", "M4" oder "".
- seller: "privat", "gewerblich" oder "unklar".
- note: 1 kurzer deutscher Satz (max. 100 Zeichen): Zustand/Akku/Garantie/Rechnung (MwSt. ausweisbar?) und ob der Preis fair wirkt.
Nichts erfinden. Antworte NUR mit JSON: {{"items":[{{"id":int,"ok":bool,"chip":str,"seller":str,"note":str}}]}}

ANGEBOTE:
""" + "\n".join(lines)
    payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
               "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json", "maxOutputTokens": 8000}}
    for model in gemini_models(key)[:3]:
        try:
            r = post_json(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                          payload, {"x-goog-api-key": key}, timeout=240)
            txt = "".join(p.get("text", "") for p in r["candidates"][0]["content"]["parts"])
            res = json.loads(re.sub(r"^```(?:json)?|```$", "", txt.strip()).strip())["items"]
            print(f"Gemini {model}: {len(res)} geprüft, Tokens {r.get('usageMetadata', {}).get('totalTokenCount')}")
            return {x["id"]: x for x in res if isinstance(x, dict) and "id" in x}
        except Exception as e:  # noqa: BLE001
            print(f"Gemini {model} fehlgeschlagen: {e}")
    return {}


# ---------------------------------------------------------------- Telegram
def send(tok, chat, text):
    req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", method="POST",
                                 data=json.dumps({"chat_id": chat, "text": text, "parse_mode": "HTML",
                                                  "disable_web_page_preview": True}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        if not json.load(r).get("ok"):
            raise RuntimeError("Telegram-Fehler")


FLAG = {"AT": "🇦🇹", "DE": "🇩🇪", "AT/DE": "🇦🇹🇩🇪", "PL": "🇵🇱", "NL": "🇳🇱", "IT": "🇮🇹", "FR": "🇫🇷", "ES": "🇪🇸",
        "BE": "🇧🇪", "CZ": "🇨🇿", "UK": "🇬🇧", "US": "🇺🇸", "CH": "🇨🇭"}
VAT = {"AT": 0.20, "DE": 0.19, "AT/DE": 0.20}


def order(o):
    c = o.get("country", "")
    return (0 if c.startswith("AT") else 1 if c == "DE" else 2, o["kind"] == "auktion", o.get("price") or 1e9)


def line(o):
    p = f"{o['price']:,.0f} €".replace(",", ".") if o.get("price") else "Preis ?"
    if o["kind"] == "auktion":
        p = "Gebot " + p
    extra = []
    if o["kind"] in ("neu", "refurbished") and o.get("price") and o["country"] in VAT:
        extra.append(f"≈ {o['price'] / (1 + VAT[o['country']]):,.0f} € netto".replace(",", "."))
    deal = o.get("price") and o["kind"] != "auktion" and o["price"] <= H.get("deal_price", 0)
    chip = (o.get("gem") or {}).get("chip") or (f"M{CHIP.search(o['title'] + ' ' + o['text']).group(1)}"
                                                 if CHIP.search(o["title"] + " " + o["text"]) else "")
    seller = (o.get("gem") or {}).get("seller") or ("gewerblich" if "[gewerblich]" in o["text"] else "")
    meta = " · ".join(x for x in (chip, o["kind"], seller) if x)
    loc = " ".join(x for x in (FLAG.get(o["country"], o["country"]), o.get("location", "")) if x)
    note = (o.get("gem") or {}).get("note", "")
    return (f"{'🔥 ' if deal else '• '}<a href=\"{html.escape(o['url'])}\">{html.escape(o['title'][:90])}</a>\n"
            f"   <b>{p}</b>{' (' + ', '.join(extra) + ')' if extra else ''} · {html.escape(o['source'])}"
            f"{' · ' + html.escape(loc) if loc else ''}\n   <i>{html.escape(meta)}</i>"
            + (f"\n   💬 {html.escape(note)}" if note else ""))


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
    seen_p = DATA / "mac_seen.json"
    seen = json.loads(seen_p.read_text(encoding="utf-8")) if seen_p.exists() else {}
    first_run = not seen

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(user_agent=UA, locale="de-AT", viewport={"width": 1440, "height": 2200})
        results, errors = {}, {}
        for name in H.get("sources", list(SOURCES)):
            n = 0
            for q in (H["queries"] if name in PER_QUERY else H["queries"][:1]):
                try:
                    got = await SOURCES[name](ctx, q)
                    for o in got:
                        results.setdefault(o["id"], o)
                    n += len(got)
                except Exception as e:  # noqa: BLE001
                    errors[name] = f"Fehler: {type(e).__name__}: {str(e)[:120]}"
            if name not in errors:
                errors[name] = f"{n} Treffer"
        results = list(results.values())

        dropped, cands = {}, []
        for o in results:
            why = candidate(o)
            if why:
                dropped[why] = dropped.get(why, 0) + 1
            else:
                cands.append(o)
        # Detailseiten nur für unklare, noch nie geprüfte Kandidaten (Deckel pro Lauf)
        unclear = [o for o in cands if spec(o) == "unklar" and o["id"] not in seen]
        unclear += [o for o in results if candidate(o) == "Größe unklar" and o["id"] not in seen
                    and o["source"].startswith("Apple")]
        unclear = unclear[: H.get("max_details", 40)]
        if unclear:
            await fetch_details(ctx, unclear)
            for o in unclear:
                if o not in cands and candidate(o) is None:
                    cands.append(o)
        await browser.close()
    print(json.dumps(errors, ensure_ascii=False, indent=1))
    if DIAG:
        print("Diagnose:", json.dumps(DIAG, ensure_ascii=False))

    matches, checked_no = [], {}
    for o in cands:
        why = spec(o)
        if why is None:
            matches.append(o)
        else:
            checked_no[why] = checked_no.get(why, 0) + 1
    print(f"gesamt={len(results)} kandidaten={len(cands)} details={len(unclear)} treffer={len(matches)} "
          f"aussortiert={dropped} spec={checked_no}")

    new = [o for o in matches if not (seen.get(o["id"]) or {}).get("match")]
    report = matches if (first_run or FULL) else new
    gem = gemini_check(report[:40])
    for i, o in enumerate(report[:40]):
        if i in gem:
            o["gem"] = gem[i]
    rejected = [o for o in report if o.get("gem") and o["gem"].get("ok") is False]
    report = sorted([o for o in report if o not in rejected], key=order)

    msgs = []
    target = "MacBook Air 15\" · ≥24 GB · ≥1 TB"
    if report:
        if first_run:
            head = f"💻 <b>MacBook-Wächter aktiv</b> ({html.escape(target)})\n{len(report)} aktuelle Angebote. Ab jetzt melde ich nur Neues."
        elif FULL:
            head = f"💻 <b>MacBook Air 15\": Übersicht</b>\n{len(report)} passende Angebote:"
        else:
            head = f"💻 <b>{len(report)} neue{'s' if len(report) == 1 else ''} MacBook Air 15\" (24 GB / 1 TB)</b>"
        msgs = chunks([line(o) for o in report], head)
    elif FULL:
        msgs = [f"💻 <b>MacBook Air 15\":</b> aktuell nichts Passendes gefunden ({len(cands)} Kandidaten geprüft)."]

    broken = [f"{k}: {v}" for k, v in errors.items() if v.startswith("Fehler") or v.startswith("0 ")]
    status = {"generated": NOW.isoformat(timespec="seconds"), "sources": errors, "candidates": len(cands),
              "details_fetched": len(unclear), "matches": len(matches), "new": len(new),
              "gemini_rejected": len(rejected), "dropped": dropped, "spec_rejected": checked_no, "diag": DIAG}
    (DATA / "mac_status.json").write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")
    DEBUG.mkdir(exist_ok=True)
    (DEBUG / "mac_preview.txt").write_text(
        "\n\n=====\n\n".join(msgs) + "\n\n----- alle Treffer -----\n" +
        "\n".join(f"{o.get('price')} | {o['source']} | {o['country']} | {o['title'][:80]} | {o['url']}\n"
                  f"     RAM: …{evidence(o, RAM)}… SSD: …{evidence(o, SSD)}… GEM: {o.get('gem')}" for o in matches) +
        "\n\n----- von Gemini verworfen -----\n" +
        "\n".join(f"{o['title'][:80]} | {o['gem'].get('note')} | {o['url']}" for o in rejected) +
        "\n\n----- Kandidaten ohne passende Spezifikation -----\n" +
        "\n".join(f"{spec(o)} | {o.get('price')} | {o['source']} | {o['title'][:80]} | {o['url']}"
                  for o in cands if spec(o))[:8000] +
        "\n\n----- Rohtreffer (Beispiele) -----\n" +
        "\n".join(f"{candidate(o)} | {o.get('price')} | {o['source']} | {o['title'][:80]}" for o in results[:150]),
        encoding="utf-8")
    for m in msgs:
        print(m, "\n-----")
    if DRY:
        print("DRY_RUN – nichts gesendet, Zustand nicht gespeichert.")
        return
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = (os.environ.get("TELEGRAM_CHAT_ID", "") or str(CFG.get("digest", {}).get("chat_id", ""))).strip()
    if msgs:
        if not (tok and chat):
            raise SystemExit("TELEGRAM_BOT_TOKEN oder Chat-ID fehlt")
        for m in msgs:
            send(tok, chat, m)
    today = NOW.date().isoformat()
    for o in cands + unclear:
        if not (o in matches or o.get("detail")):
            continue   # noch nicht abschließend geprüft (z. B. Detail-Deckel erreicht) -> nächster Lauf
        prev = seen.get(o["id"]) or {}
        seen[o["id"]] = {"date": prev.get("date", today),
                         "match": prev.get("match", False) or (o in matches and o not in rejected)}
    old = (NOW.date() - dt.timedelta(days=120)).isoformat()
    seen_p.write_text(json.dumps({k: v for k, v in seen.items() if v["date"] >= old}, indent=0), encoding="utf-8")
    if broken:
        print("::warning::" + "; ".join(broken))


if __name__ == "__main__":
    asyncio.run(main())
