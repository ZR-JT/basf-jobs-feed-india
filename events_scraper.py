import asyncio
import json
import re
from playwright.async_api import async_playwright
from datetime import datetime

EVENTS_URL = "https://www.basf.com/global/de/careers/application/events"

async def scrape_events():
    all_events = []
    captured_responses = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )

        # ── Alle JSON-Responses intercepten ──────────────────────────────────
        async def handle_response(response):
            url = response.url
            content_type = response.headers.get("content-type", "")
            if "json" in content_type and response.status == 200:
                try:
                    body = await response.json()
                    captured_responses.append({"url": url, "body": body})
                    print(f"  📡 JSON-Response: {url[:100]}")
                except Exception:
                    pass

        context.on("response", handle_response)

        page = await context.new_page()

        print(f"📅 Lade Events: {EVENTS_URL}")
        try:
            await page.goto(EVENTS_URL, timeout=60000, wait_until="domcontentloaded")
        except Exception as e:
            print(f"  ⚠ goto Fehler (ignoriert): {e}")

        await page.wait_for_timeout(6000)

        # ── "Mehr anzeigen" per JavaScript klicken ───────────────────────────
        clicks = 0
        for _ in range(20):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(2000)

            # Alle Buttons auf der Seite finden und nach Text filtern
            clicked = await page.evaluate("""
                () => {
                    const buttons = Array.from(document.querySelectorAll('button, a'));
                    const btn = buttons.find(b => 
                        b.innerText && (
                            b.innerText.trim().includes('Mehr anzeigen') ||
                            b.innerText.trim().includes('mehr anzeigen') ||
                            b.innerText.trim().includes('Load more') ||
                            b.innerText.trim().includes('Weitere')
                        )
                    );
                    if (btn) {
                        btn.click();
                        return btn.innerText.trim();
                    }
                    return null;
                }
            """)

            if clicked:
                clicks += 1
                print(f"  🖱 Klick {clicks} — Button: '{clicked}'")
                await page.wait_for_timeout(3000)
            else:
                print(f"  ✅ Kein Button mehr — {clicks} Klicks gesamt")
                break

        # ── Seiten-HTML für Debugging speichern ───────────────────────────────
        html_content = await page.content()
        with open("events_debug.html", "w", encoding="utf-8") as f:
            f.write(html_content)
        print(f"  💾 events_debug.html gespeichert ({len(html_content)} Zeichen)")

        # ── Captured JSON-Responses nach Event-Daten durchsuchen ─────────────
        print(f"\n  📡 {len(captured_responses)} JSON-Responses gefangen")

        for resp in captured_responses:
            body = resp["body"]
            url = resp["url"]
            events_found = extract_events_from_json(body, url)
            if events_found:
                print(f"  ✅ {len(events_found)} Events aus: {url[:80]}")
                all_events.extend(events_found)

        # ── Fallback: DOM direkt parsen ───────────────────────────────────────
        if len(all_events) == 0:
            print("\n  🔄 JSON leer — versuche DOM-Parsing...")

            selectors = [
                "li", "article", "[class*='event']", "[class*='Event']",
                "[class*='teaser']", "[class*='Teaser']",
                "[class*='card']", "[class*='Card']",
                "[class*='item']", "[class*='Item']",
            ]

            for selector in selectors:
                items = await page.query_selector_all(selector)
                if len(items) >= 5:
                    print(f"  Selector '{selector}': {len(items)} Elemente")
                    events_from_dom = await parse_dom_items(items)
                    if events_from_dom:
                        all_events.extend(events_from_dom)
                        print(f"  ✅ {len(events_from_dom)} Events via DOM")
                        break

        await browser.close()

    # Deduplizieren
    seen = set()
    unique_events = []
    for e in all_events:
        key = (e.get("title", ""), e.get("date_iso", "") or e.get("date_text", ""))
        if key not in seen and key[0]:
            seen.add(key)
            unique_events.append(e)

    unique_events.sort(key=lambda e: e.get("date_iso") or e.get("date_text") or "")

    print(f"\n✅ {len(unique_events)} Events total")

    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    rows = ""
    for e in unique_events:
        rows += f"""<div class="event">
  <h2>{e.get('title', '')}</h2>
  <p><strong>Datum:</strong> {e.get('date_text', '')}{f" ({e['date_iso']})" if e.get('date_iso') and e['date_iso'] != e.get('date_text') else ""}</p>
  {f"<p><strong>Ort:</strong> {e['location']}</p>" if e.get('location') else ""}
  {f"<p><strong>Format:</strong> {e['format']}</p>" if e.get('format') else ""}
  {f'<p><strong>Link:</strong> {e["url"]}</p>' if e.get('url') else ""}
</div>
"""

    html = f"""<!DOCTYPE html>
<html lang="de">
<head><meta charset="UTF-8"><title>BASF Events &amp; Termine</title></head>
<body>
<h1>BASF Events &amp; Termine</h1>
<p>Stand: {timestamp} | {len(unique_events)} Veranstaltungen</p>
{rows}
</body>
</html>"""

    with open("events.html", "w", encoding="utf-8") as f:
        f.write(html)

    print(f"✅ events.html gespeichert!")


def extract_events_from_json(data, url):
    events = []

    if isinstance(data, list):
        for item in data:
            e = try_parse_event(item)
            if e:
                events.append(e)

    elif isinstance(data, dict):
        for key in ["items", "events", "results", "data", "content",
                    "entries", "list", "records", "hits"]:
            if key in data and isinstance(data[key], list):
                for item in data[key]:
                    e = try_parse_event(item)
                    if e:
                        events.append(e)
                if events:
                    return events

        for v in data.values():
            if isinstance(v, (list, dict)):
                sub = extract_events_from_json(v, url)
                if sub:
                    events.extend(sub)

    return events


def try_parse_event(item):
    if not isinstance(item, dict):
        return None

    title = (
        item.get("title") or item.get("name") or item.get("headline") or
        item.get("eventName") or item.get("event_name") or
        item.get("Titel") or ""
    )
    if not title or len(str(title)) < 4:
        return None

    date_iso = (
        item.get("date") or item.get("startDate") or item.get("start_date") or
        item.get("eventDate") or item.get("datePosted") or
        item.get("Datum") or ""
    )
    date_text = str(date_iso)[:10] if date_iso else ""

    url = (
        item.get("url") or item.get("link") or item.get("href") or
        item.get("detailUrl") or ""
    )
    if url and not url.startswith("http"):
        url = f"https://www.basf.com{url}"

    location = (
        item.get("location") or item.get("city") or item.get("place") or
        item.get("venue") or item.get("Ort") or ""
    )

    fmt = item.get("format") or item.get("type") or item.get("mode") or ""

    return {
        "title": str(title).strip(),
        "date_text": date_text,
        "date_iso": str(date_iso)[:10] if date_iso else "",
        "location": str(location).strip() if location else "",
        "format": str(fmt).strip() if fmt else "",
        "url": str(url).strip() if url else "",
    }


async def parse_dom_items(items):
    events = []
    skip_titles = {
        "Mehr anzeigen", "Load more", "Alles Entfernen",
        "Laufende und künftige", "Filtern", "Filter", ""
    }

    for item in items:
        try:
            title_el = await item.query_selector(
                "h2, h3, h4, [class*='title'], [class*='Title'], "
                "[class*='headline'], strong"
            )
            if not title_el:
                continue
            title = (await title_el.inner_text()).strip()
            if not title or title in skip_titles or len(title) < 4:
                continue

            date_el = await item.query_selector(
                "time, [class*='date'], [class*='Date'], [datetime]"
            )
            date_text = ""
            date_iso = ""
            if date_el:
                date_text = (await date_el.inner_text()).strip()
                date_iso = await date_el.get_attribute("datetime") or ""

            link_el = await item.query_selector("a[href]")
            url = ""
            if link_el:
                href = await link_el.get_attribute("href") or ""
                if href.startswith("http"):
                    url = href
                elif href.startswith("/"):
                    url = f"https://www.basf.com{href}"

            loc_el = await item.query_selector(
                "[class*='location'], [class*='place'], [class*='city']"
            )
            location = ""
            if loc_el:
                location = (await loc_el.inner_text()).strip()

            events.append({
                "title": title,
                "date_text": date_text,
                "date_iso": date_iso,
                "location": location,
                "format": "",
                "url": url,
            })

        except Exception:
            continue

    return events


asyncio.run(scrape_events())
