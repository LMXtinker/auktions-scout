"""Einmalig: Chat-IDs aller Personen ausgeben, die dem Bot geschrieben haben."""
import json
import os
import urllib.request

tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
if not tok:
    raise SystemExit("TELEGRAM_BOT_TOKEN fehlt (Repository secret).")
with urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/getMe", timeout=30) as r:
    me = json.load(r)["result"]
print(f"::notice::Bot: @{me.get('username')}")
with urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/getWebhookInfo", timeout=30) as r:
    wh = json.load(r)["result"]
print(f"::notice::Webhook: {wh.get('url') or 'keiner'} pending={wh.get('pending_update_count')}")
with urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/getUpdates", timeout=30) as r:
    upd = json.load(r)
chats = {}
for u in upd.get("result", []):
    m = u.get("message") or u.get("edited_message") or {}
    c = m.get("chat") or {}
    if c.get("id"):
        chats[c["id"]] = (c.get("first_name", ""), c.get("username", ""), (m.get("text") or "")[:20])
if not chats:
    print("::notice::Keine Nachrichten gefunden (Updates bleiben nur ~24 h erhalten).")
for cid, (name, user, text) in chats.items():
    print(f"::notice::CHAT_ID={cid} Name={name} @{user} letzte Nachricht={text!r}")
