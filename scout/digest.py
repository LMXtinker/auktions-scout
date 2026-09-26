"""Auktions-Scout Digest: bewertet neue Lose mit Gemini und schickt sie per Telegram.

Env (Repository secrets): GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
     optional TELEGRAM_CC_CHAT_IDS (kommagetrennt), TELEGRAM_ADMIN_CHAT_ID (Warnungen; Standard = TELEGRAM_CHAT_ID)
     GEMINI_MODEL (Variable, optional), DRY_RUN=1, RESEND=1
"""
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA, DEBUG = ROOT / "data", ROOT / "debug"
CFG = yaml.safe_load(open(ROOT / "config.yaml", encoding="utf-8"))
DG = CFG.get("digest", {})
TZ = ZoneInfo("Europe/Vienna")
NOW = dt.datetime.now(dt.timezone.utc)
DRY = os.environ.get("DRY_RUN", "").strip() in ("1", "true", "yes")
RESEND = os.environ.get("RESEND", "").strip() in ("1", "true", "yes")
USAGE = {}

# Vorfilter: nur Lose, deren Text nach relevanter Hardware klingt
INCLUDE = re.compile(DG.get("prefilter", r"RTX|4060|4070|4080|4090|50[6-9]0|A[2-6]000|Ada|Quadro|Beamer|Projektor|"
                     r"Projection|Projektion|Laser|lumen|LED|Novastar|Brompton|Blackmagic|ATEM|SDI|grandMA|Moving|"
                     r"Traverse|Truss|Flightcase|Workstation|Threadripper|Xeon|Barco|Christie|Panasonic|Epson|NEC|"
                     r"Sony|Veranstaltung|Bühne|Grafikkarte|GPU|Medienserver|Lichtpult|Kamera|Switch"), re.I)


def load(fn, default):
    p = DATA / fn
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def save(fn, obj):
    (DATA / fn).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def post_json(url, payload, headers=None, timeout=240):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def esc(s):
    return html.escape(s or "", quote=False)


def fmt(n):
    return f"{n:,}".replace(",", ".")


# ---------------------------------------------------------------- Gemini
def gemini_models(key):
    wanted = [os.environ.get("GEMINI_MODEL", "").strip()] if os.environ.get("GEMINI_MODEL", "").strip() else []
    try:
        req = urllib.request.Request("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                                     headers={"x-goog-api-key": key})
        with urllib.request.urlopen(req, timeout=30) as r:
            names = [m["name"].split("/")[-1] for m in json.load(r).get("models", [])
                     if "generateContent" in m.get("supportedGenerationMethods", [])]
    except Exception as e:
        print("Modellliste nicht abrufbar:", e)
        names = []
    flash = sorted([n for n in names if n.startswith("gemini") and "flash" in n and not re.search(
        r"lite|preview|exp|image|tts|audio|live", n)], key=lambda n: [int(x) for x in re.findall(r"\d+", n)] or [0], reverse=True)
    out = []
    for n in wanted + flash + ["gemini-flash-latest", "gemini-2.5-flash"]:
        if n and n not in out:
            out.append(n)
    return out


PROMPT = """Du bewertest Lose aus Insolvenz-, Firmenauflösungs-, Justiz- und Zollauktionen (AT/DE) für Lucas,
freiberuflicher Visual Artist / Event-Techniker aus Wien (Projection Mapping, LED-Wände, Unreal Engine, TouchDesigner).

RELEVANZ (Priorität absteigend):
1. NVIDIA RTX 40-/50-Serie (ab 4060 Ti, v. a. 4080/4090/5080/5090) und Profikarten (RTX A4000–A6000, RTX 4000/5000/6000 Ada,
   Quadro RTX 8000) – auch versteckt in „Workstation“, „Gaming-PC“, „Server“, wenn der Text es nahelegt.
2. Projektoren ab ca. 5.000 lm / Laser / Installationsprojektoren (Barco, Christie, Panasonic, Epson Pro, Sony, NEC),
   Wechselobjektive, interaktive Projektionssysteme.
3. LED-Wand-Module/-Prozessoren (Novastar, Brompton), Medienserver.
4. Video/AV-Pro: Blackmagic, Kreuzschienen, SDI/HDMI-Distribution, Kameras, 10G-Switches, Lichtpulte, Moving Heads,
   Veranstaltungstechnik-Konvolute.
5. Sonstiges für Events/Installationen: Traversen, Flightcases, Rigging, große Pro-Monitore, High-End-Workstations.
Nicht relevant: Consumer-TVs, Büro-PCs ohne dedizierte GPU, alte GPUs (RTX 30 / GTX und älter), Standard-Bürobeamer
unter 4.000 lm – außer als extrem billiges Konvolut. Standort AT/Süddeutschland bevorzugt, weit weg mit Hinweis.

TEXTFORMAT: Aurena: "Losnr | Titel | Tage | : | Std | Gebote: n | € Preis". Links mit "#los-n" sind Suchlinks.

AUFGABE: Wähle nur wirklich relevante Lose (streng, meistens sind es wenige oder keine). Je Los:
- id (aus den Daten), title (bereinigt, max. 70 Zeichen), platform, location (falls erkennbar, sonst ""),
  price (aktuelles Gebot, wie im Text), ends (Restzeit/Ende, wie im Text, sonst ""),
- tier: "top" (klar interessant, Priorität 1–2 oder sehr günstig) oder "weitere",
- note: 1 kurzer deutscher Satz (max. 110 Zeichen): grober Gebrauchtmarktwert vs. Gebot inkl. ca. 15–20 % Aufgeld + USt.,
  lohnt es sich? Nichts erfinden, was nicht aus dem Text ableitbar ist.

Antworte NUR mit JSON: {{"lots":[{{"id":int,"title":str,"platform":str,"location":str,"price":str,"ends":str,"tier":str,"note":str}}]}}

LOSE (JSON-Zeilen):
{lots}
"""


def rate(lots, key):
    lines = [json.dumps({"id": i, "site": l.get("site", ""), "url": l["url"][:140],
                         "text": re.sub(r"\s+", " ", l.get("text", ""))[:300]}, ensure_ascii=False)
             for i, l in enumerate(lots)]
    payload = {"contents": [{"role": "user", "parts": [{"text": PROMPT.format(lots="\n".join(lines))}]}],
               "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json", "maxOutputTokens": 16000}}
    last = None
    for model in gemini_models(key):
        for attempt in range(3):
            try:
                print(f"Gemini: {model} (Versuch {attempt + 1}), {len(lots)} Kandidaten")
                r = post_json(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                              payload, {"x-goog-api-key": key})
                text = "".join(p.get("text", "") for p in r["candidates"][0]["content"]["parts"])
                text = re.sub(r"^```(?:json)?|```$", "", text.strip()).strip()
                picked = json.loads(text)["lots"]
                USAGE.clear()
                USAGE.update(r.get("usageMetadata") or {})
                return model, picked
            except urllib.error.HTTPError as e:
                last = f"{model}: HTTP {e.code} {e.read().decode('utf-8', 'replace')[:300]}"
                print(last)
                if e.code in (429, 500, 503):
                    time.sleep(20 * (attempt + 1))
                    continue
                break
            except Exception as e:
                last = f"{model}: {e}"
                print(last)
                time.sleep(5)
    raise RuntimeError(f"Gemini-Bewertung fehlgeschlagen – {last}")


# ---------------------------------------------------------------- Telegram
def send(token, chat, text):
    r = post_json(f"https://api.telegram.org/bot{token}/sendMessage",
                  {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=30)
    if not r.get("ok"):
        raise RuntimeError(r)


def admin(text):
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    adm = (os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "") or os.environ.get("TELEGRAM_CHAT_ID", "")).strip()
    if tok and adm and not DRY:
        try:
            send(tok, adm, text)
        except Exception as e:
            print("Admin-Nachricht fehlgeschlagen:", e)


def split(blocks, limit=3900):
    msgs, cur = [], ""
    for b in blocks:
        if cur and len(cur) + len(b) + 1 > limit:
            msgs.append(cur)
            cur = b.lstrip("\n")
        else:
            cur += ("\n" if cur else "") + b
    if cur:
        msgs.append(cur)
    return msgs


def build(picked, lots, footer):
    items = []
    for p in picked:
        try:
            l = lots[int(p["id"])]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        items.append({**p, "url": l["url"]})
    top = [x for x in items if x.get("tier") == "top"]
    more = [x for x in items if x.get("tier") != "top"]
    day = dt.datetime.now(TZ).strftime("%d.%m.")
    blocks = [f"<b>🔨 Auktions-Scout · {day}</b>"]
    if not items:
        blocks.append("Heute nichts Relevantes neu dazugekommen.")
    if top:
        blocks.append(f"\n<b>⭐ Top-Treffer</b>  ·  {len(top)}")
        for x in top:
            meta = " · ".join(v for v in [x.get("platform", ""), x.get("location", "")] if v.strip())
            price = " · ".join(v for v in [("💶 " + x["price"]) if x.get("price") else "",
                                           ("⏳ " + x["ends"]) if x.get("ends") else ""] if v)
            e = f"\n🔹 <b><a href=\"{html.escape(x['url'])}\">{esc(x.get('title', 'Los'))}</a></b>"
            if meta:
                e += f"\n{esc(meta)}"
            if price:
                e += f"\n{esc(price)}"
            if x.get("note"):
                e += f"\n<i>{esc(x['note'])}</i>"
            blocks.append(e)
    if more:
        blocks.append(f"\n<b>Weitere interessante</b>  ·  {len(more)}")
        for x in more:
            extra = " · ".join(v for v in [x.get("price", ""), x.get("ends", "")] if v)
            blocks.append(f"• <a href=\"{html.escape(x['url'])}\">{esc(x.get('title', 'Los'))}</a>"
                          + (f" – {esc(extra)}" if extra else ""))
    blocks.append(f"\n<i>{footer}</i>")
    return split(blocks), items


def wait_for_send_time():
    if os.environ.get("GITHUB_EVENT_NAME") != "schedule":
        return True
    now = dt.datetime.now(TZ)
    hh, mm = map(int, str(DG.get("send_at", "07:45")).split(":"))
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if load("digest_status.json", {}).get("scheduled_day") == now.date().isoformat():
        print("Heute schon gesendet."); return False
    if now < target - dt.timedelta(minutes=75):
        print(f"Zu früh ({now:%H:%M})."); return False
    if now > target + dt.timedelta(hours=3):
        print(f"Zu spät ({now:%H:%M})."); return False
    if now < target:
        print(f"Warte bis {hh:02d}:{mm:02d} …", flush=True)
        time.sleep((target - now).total_seconds())
    return True


def main():
    if not wait_for_send_time():
        return
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    chats = [c.strip() for c in (chat + "," + os.environ.get("TELEGRAM_CC_CHAT_IDS", "")).split(",") if c.strip()]
    missing = [n for n, v in [("GEMINI_API_KEY", key), ("TELEGRAM_BOT_TOKEN", tok), ("TELEGRAM_CHAT_ID", chat)] if not v]
    if missing and not (DRY and key):
        raise SystemExit(f"Fehlende Secrets: {', '.join(missing)}")

    status_scrape = load("status.json", {})
    scraped = status_scrape.get("generated", "")
    warn = []
    if not scraped or scraped[:19] < (NOW - dt.timedelta(hours=30)).isoformat()[:19]:
        warn.append(f"Scraper-Daten veraltet ({scraped[:16] or 'keine'})")
    for site, v in status_scrape.get("sites", {}).items():
        if not v.get("cards") or v.get("errors"):
            warn.append(f"{site}: {v.get('cards', 0)} Lose, {v.get('errors', 0)} Fehler")

    lots = load("latest.json", {}).get("lots", [])
    rated = load("rated.json", {})
    cands = [l for l in lots if (RESEND or l["url"] not in rated) and INCLUDE.search(l.get("text", ""))]
    fresh = [l for l in lots if l["url"] not in rated]
    print(f"Lose={len(lots)} unbewertet={len(fresh)} nach Vorfilter={len(cands)}")

    model, picked = ("", [])
    if cands:
        model, picked = rate(cands, key)

    # Tokens mitzählen
    usage = load("usage.json", {})
    month = usage.setdefault(NOW.strftime("%Y-%m"), {"runs": 0, "prompt": 0, "output": 0, "thoughts": 0, "total": 0})
    run = {"prompt": USAGE.get("promptTokenCount", 0), "output": USAGE.get("candidatesTokenCount", 0),
           "thoughts": USAGE.get("thoughtsTokenCount", 0), "total": USAGE.get("totalTokenCount", 0)}
    if run["total"] and not DRY:
        month["runs"] += 1
        for k, v in run.items():
            month[k] += v

    footer = (f"{len(fresh)} neue Lose, {len(cands)} geprüft · Gemini {fmt(run['total'])} Tokens "
              f"(Monat {fmt(month['total'])}) · Stand {scraped[11:16] or '?'} UTC")
    if warn:
        footer += "\n⚠️ " + esc("; ".join(warn))
    msgs, items = build(picked, cands, footer)

    DEBUG.mkdir(exist_ok=True)
    (DEBUG / "digest_preview.txt").write_text("\n\n=====\n\n".join(msgs), encoding="utf-8")
    status = {"generated": NOW.isoformat(timespec="seconds"), "candidates": len(cands), "picked": len(items),
              "model": model, "tokens": run}
    if DRY:
        print("DRY_RUN – nichts gesendet.")
    else:
        for c in chats:
            for m in msgs:
                send(tok, c, m)
                time.sleep(1.2)
        for l in fresh:
            rated[l["url"]] = NOW.date().isoformat()
        old = (NOW.date() - dt.timedelta(days=120)).isoformat()
        save("rated.json", {u: d for u, d in rated.items() if d >= old})
        save("usage.json", usage)
        if os.environ.get("GITHUB_EVENT_NAME") == "schedule":
            status["scheduled_day"] = dt.datetime.now(TZ).date().isoformat()
        print(f"{len(msgs)} Nachricht(en), {len(items)} Lose gesendet.")
    save("digest_status.json", status)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"::error::{e}")
        admin(f"⚠️ <b>Auktions-Scout:</b> Bewertung/Versand fehlgeschlagen:\n{esc(str(e))[:500]}")
        sys.exit(1)
