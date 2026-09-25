# auktions-scout

Täglicher Scraper (GitHub Actions + Playwright) für Insolvenz-/Firmenauflösungs-Auktionen in AT/DE.
Sucht AV-Hardware, RTX-40/50-GPUs usw. Eine geplante Claude-Aufgabe liest danach `data/new.json`,
bewertet die Lose und schickt den Digest.

- Plattformen & Suchbegriffe: `config.yaml`
- Ergebnisse: `data/latest.json` (alle), `data/new.json` (neu seit letztem Lauf), `data/status.json`
- Diagnose: `debug/` (Such-URLs, API-Aufrufe, Screenshots)
- Manuell starten: Actions → scout → Run workflow
