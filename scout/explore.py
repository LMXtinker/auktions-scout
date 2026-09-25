"""Diagnose: öffnet URLs, protokolliert Eingabefelder, Such-Links und API-Aufrufe (debug/explore.json)."""
import asyncio, json, re, sys
from pathlib import Path
from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).parent))
from scrape import UA, accept_cookies  # noqa: E402

NOISE = re.compile(r"cookielaw|onetrust|exponea|segment|criteo|reddit|stripe|google|facebook|hotjar|usercentrics|sentry|datadog", re.I)
URLS = sys.argv[1:]
OUT = Path(__file__).resolve().parent.parent / "debug" / "explore.json"


async def main():
    res = []
    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        for url in URLS:
            ctx = await b.new_context(user_agent=UA, locale="de-AT", viewport={"width": 1440, "height": 2000})
            page = await ctx.new_page()
            calls = []

            async def on_resp(r):
                try:
                    if NOISE.search(r.url) or "json" not in r.headers.get("content-type", ""):
                        return
                    calls.append({"m": r.request.method, "url": r.url, "post": (r.request.post_data or "")[:2000],
                                  "status": r.status, "body": (await r.text())[:2500]})
                except Exception:
                    pass
            page.on("response", on_resp)
            url, _, typed = url.partition("|")
            entry = {"url": url, "typed": typed}
            try:
                if url.startswith("GET:"):
                    r = await ctx.request.get(url[4:], headers={"accept": "application/json"})
                    entry["probe_status"] = r.status
                    entry["probe_body"] = (await r.text())[:2500]
                    res.append(entry); await ctx.close(); continue
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await accept_cookies(page)
                if typed:
                    await page.wait_for_timeout(2000)
                    inp = page.locator("input[type=text]:visible, input[type=search]:visible").first
                    await inp.click(); await inp.fill(typed); await page.wait_for_timeout(2500)
                    entry["suggest_text"] = (await page.evaluate("document.body.innerText"))[:1500]
                    await inp.press("Enter"); await page.wait_for_timeout(4000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=12000)
                except Exception:
                    pass
                for _ in range(3):
                    await page.mouse.wheel(0, 2500)
                    await page.wait_for_timeout(1000)
                entry["final_url"] = page.url
                entry["inputs"] = await page.evaluate("""[...document.querySelectorAll('input,textarea')].map(e=>({tag:e.tagName,type:e.type,name:e.name,ph:e.placeholder,aria:e.getAttribute('aria-label'),vis:!!(e.offsetWidth||e.offsetHeight)}))""")
                entry["search_links"] = await page.evaluate("""[...document.querySelectorAll('a[href],button')].filter(e=>/such|search|find/i.test((e.href||'')+' '+(e.innerText||'')+' '+(e.getAttribute('aria-label')||''))).slice(0,40).map(e=>({tag:e.tagName,href:e.href||'',text:(e.innerText||e.getAttribute('aria-label')||'').slice(0,60)}))""")
                entry["sample_links"] = await page.evaluate("""[...new Set([...document.querySelectorAll('a[href]')].map(a=>a.href))].slice(0,150)""")
                entry["text_head"] = (await page.evaluate("document.body.innerText"))[:3000]
            except Exception as e:
                entry["error"] = str(e)[:300]
            entry["calls"] = calls[:40]
            res.append(entry)
            await ctx.close()
        await b.close()
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")


asyncio.run(main())
