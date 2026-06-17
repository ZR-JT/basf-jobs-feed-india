import json
import os
import re
import asyncio
from playwright.async_api import async_playwright
from datetime import datetime

BASE_URL = "https://ZR-JT.github.io/basf-jobs-feed-india"
SF_ENDPOINT = "/services/recruiting/v1/jobs"


def strip_html(text):
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&[a-zA-Z]+;', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def slugify(text):
    text = text.lower().strip()
    text = re.sub(r'[äÄ]', 'ae', text)
    text = re.sub(r'[öÖ]', 'oe', text)
    text = re.sub(r'[üÜ]', 'ue', text)
    text = re.sub(r'[ß]', 'ss', text)
    text = re.sub(r'[^a-z0-9]+', '-', text)
    text = text.strip('-')
    return text


def extract_batch(data):
    """Try all known SuccessFactors field names for the jobs array."""
    for key in ("jobs", "jobPostings", "jobResults", "requisitions", "results", "data"):
        val = data.get(key)
        if val and isinstance(val, list):
            return val, key
    return [], None


def extract_total(data):
    for key in ("total", "noOfJobs", "totalCount", "count", "totalJobs"):
        val = data.get(key)
        if isinstance(val, int):
            return val
    return None


async def post_jobs(page, body):
    """POST to SF endpoint from within the browser context."""
    return await page.evaluate(
        """async ([endpoint, body]) => {
            try {
                const resp = await fetch(endpoint, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRF-Token': window.CSRFToken || ''
                    },
                    body: JSON.stringify(body)
                });
                if (!resp.ok) {
                    const txt = await resp.text();
                    return { __error: `HTTP ${resp.status}`, __body: txt.slice(0, 500) };
                }
                return await resp.json();
            } catch (e) {
                return { __error: String(e) };
            }
        }""",
        [SF_ENDPOINT, body]
    )


async def scrape_jobs():
    all_raw_jobs = []

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()

        # ── Intercept the first SF response to learn the real schema ─────────
        intercepted_schema = {}

        async def on_response(response):
            if SF_ENDPOINT in response.url and response.request.method == "POST" and not intercepted_schema:
                try:
                    data = await response.json()
                    intercepted_schema["keys"] = list(data.keys())
                    batch, key = extract_batch(data)
                    intercepted_schema["batch_key"] = key
                    intercepted_schema["sample_job_keys"] = list(batch[0].keys()) if batch else []
                    req_raw = await response.request.body()
                    intercepted_schema["req_body"] = json.loads(req_raw) if req_raw else {}
                except Exception as e:
                    intercepted_schema["error"] = str(e)

        page.on("response", on_response)

        print("Lade basf.jobs/search ...")
        await page.goto(
            "https://basf.jobs/search",
            timeout=60000,
            wait_until="networkidle"
        )
        await page.wait_for_timeout(3000)

        csrf_token = await page.evaluate("window.CSRFToken")
        print(f"CSRF Token: {'gefunden' if csrf_token else 'NICHT gefunden'}")

        if intercepted_schema:
            print(f"[schema] Response-Keys: {intercepted_schema.get('keys')}")
            print(f"[schema] Batch-Key: {intercepted_schema.get('batch_key')}")
            print(f"[schema] Job-Keys (erste 10): {intercepted_schema.get('sample_job_keys', [])[:10]}")
            print(f"[schema] Request-Body: {json.dumps(intercepted_schema.get('req_body', {}))[:200]}")
        else:
            print("[schema] Kein SF-Request beim Seitenload abgefangen")

        # ── India-Filter: zwei Varianten, erste die funktioniert wird genommen ─
        INDIA_FILTERS = [
            {"location": "India", "facetFilters": {}},
            {"location": "", "facetFilters": {"country": ["India"]}},
            {"location": "", "facetFilters": {"addressCountry": ["India"]}},
            {"location": "India", "facetFilters": {"country": ["India"]}},
        ]

        base_body = {
            "locale": "en_US",
            "sortBy": "date",
            "keywords": "",
            "brand": "",
            "skills": [],
            "categoryId": 0,
            "pageNumber": 0,
        }

        working_filter = None
        first_data = None

        for india_filter in INDIA_FILTERS:
            test_body = {**base_body, **india_filter, "pageNumber": 0}
            print(f"  Teste Filter: {india_filter} ...")
            data = await post_jobs(page, test_body)
            if "__error" in data:
                print(f"  ❌ {data['__error']} | {data.get('__body', '')[:150]}")
                continue
            batch, batch_key = extract_batch(data)
            total = extract_total(data)
            print(f"  → Response-Keys: {list(data.keys())} | batch_key={batch_key} | total={total} | batch={len(batch)}")
            if batch:
                working_filter = india_filter
                first_data = data
                print(f"  ✅ Filter funktioniert: {india_filter}")
                break

        if working_filter is None:
            print("❌ Kein India-Filter liefert Ergebnisse. Rohdaten aus Seitenload nutzen ...")
            # Fallback: use whatever the page loaded initially (no India filter),
            # we'll filter for India in Python
            working_filter = {"location": "", "facetFilters": {}}
            test_body = {**base_body, **working_filter, "pageNumber": 0}
            first_data = await post_jobs(page, test_body)
            if "__error" in first_data:
                print(f"❌ Auch Fallback schlägt fehl: {first_data['__error']}")
                await browser.close()
                return

        batch, batch_key = extract_batch(first_data)
        total_reported = extract_total(first_data)
        all_raw_jobs.extend(batch)
        print(f"Seite 0: {len(batch)} Jobs | Gesamt laut API: {total_reported}")

        if batch:
            detected_page_size = len(batch)
            page_number = 1
            while True:
                if isinstance(total_reported, int) and len(all_raw_jobs) >= total_reported:
                    break
                print(f"  Lade Seite {page_number} ...")
                body = {**base_body, **working_filter, "pageNumber": page_number}
                data = await post_jobs(page, body)
                if "__error" in data:
                    print(f"❌ Fehler Seite {page_number}: {data['__error']}")
                    break
                next_batch, _ = extract_batch(data)
                if not next_batch:
                    break
                all_raw_jobs.extend(next_batch)
                print(f"  Seite {page_number}: {len(next_batch)} Jobs (gesamt: {len(all_raw_jobs)})")
                if len(next_batch) < detected_page_size:
                    break
                page_number += 1

        await browser.close()

    print(f"Rohdaten: {len(all_raw_jobs)} Jobs")

    # Fallback India-Filter auf Python-Seite (falls kein API-Filter gesetzt)
    if working_filter == {"location": "", "facetFilters": {}}:
        before = len(all_raw_jobs)
        all_raw_jobs = [
            j for j in all_raw_jobs
            if "India" in str(j.get("country") or j.get("locationCountry") or
                              j.get("primaryLocation") or j.get("addresses") or "")
        ]
        print(f"Python-seitig auf India gefiltert: {before} → {len(all_raw_jobs)}")

    print(f"Rohdaten: {len(all_raw_jobs)} Jobs")

    # Deduplizieren (Sicherheitsnetz falls mehrere Locales zurückkommen)
    PREFERRED_LOCALES = ["en_US", "en_IN", "de_DE", "de_AT", "de_CH"]
    job_map = {}
    for job in all_raw_jobs:
        job_id = str(
            job.get("jobId") or
            job.get("jobReqId") or
            job.get("id") or ""
        )
        numeric_id = job_id.split("-")[0] if "-" in job_id else job_id
        if not numeric_id:
            continue
        lang = job.get("language") or job.get("locale") or ""
        if numeric_id not in job_map:
            job_map[numeric_id] = job
        else:
            curr_lang = job_map[numeric_id].get("language") or ""
            curr_pref = PREFERRED_LOCALES.index(curr_lang) if curr_lang in PREFERRED_LOCALES else 999
            new_pref = PREFERRED_LOCALES.index(lang) if lang in PREFERRED_LOCALES else 999
            if new_pref < curr_pref:
                job_map[numeric_id] = job

    print(f"Nach Deduplizierung: {len(job_map)} unique Jobs")

    jobs = []
    for numeric_id, job in job_map.items():
        # Location: SF kann flach oder verschachtelt sein
        primary_loc = job.get("primaryLocation") or {}
        if not isinstance(primary_loc, dict):
            primary_loc = {}
        locations_list = job.get("locations") or []
        first_loc = locations_list[0] if isinstance(locations_list, list) and locations_list else {}
        if not isinstance(first_loc, dict):
            first_loc = {}

        city = (
            job.get("city") or
            job.get("locationCity") or
            primary_loc.get("city") or
            first_loc.get("city") or
            "Unknown"
        )
        state = (
            job.get("state") or
            job.get("locationState") or
            primary_loc.get("state") or
            first_loc.get("state") or
            "Unknown"
        )
        country = (
            job.get("country") or
            job.get("locationCountry") or
            primary_loc.get("country") or
            first_loc.get("country") or
            "India"
        )

        # Recruiter
        rec_raw = job.get("recruiter") or {}
        if not isinstance(rec_raw, dict):
            rec_raw = {}
        recruiter = {}
        first = job.get("recruiterFirstName") or rec_raw.get("firstName") or ""
        last = job.get("recruiterLastName") or rec_raw.get("lastName") or ""
        email = job.get("recruiterEmail") or rec_raw.get("email") or ""
        phone = job.get("recruiterPhone") or rec_raw.get("phone") or ""
        name = f"{first} {last}".strip()
        if name:
            recruiter["name"] = name
        if email:
            recruiter["email"] = email
        if phone:
            recruiter["phone"] = phone

        raw_desc = job.get("description") or job.get("jobDescription") or ""
        description = strip_html(raw_desc)[:500]

        url = (
            job.get("url") or
            job.get("applyUrl") or
            job.get("link") or
            job.get("jobUrl") or
            f"https://basf.jobs/job/{numeric_id}/"
        )

        entry = {
            "job_id": numeric_id,
            "title": (job.get("title") or job.get("jobTitle") or "").strip(),
            "url": url,
            "city": city,
            "state": state,
            "country": country,
            "company": (
                job.get("company") or
                job.get("legalEntity") or
                job.get("companyName") or
                "BASF"
            ),
            "business_unit": job.get("businessUnit") or job.get("division") or "",
            "department": job.get("department") or "",
            "job_field": (
                job.get("jobField") or
                job.get("category") or
                job.get("jobCategory") or ""
            ),
            "job_level": job.get("jobLevel") or job.get("customfield1") or job.get("level") or "",
            "job_type": job.get("jobType") or job.get("customfield5") or job.get("employmentType") or "",
            "hybrid": job.get("hybrid") or False,
            "date_posted": (
                job.get("datePosted") or
                job.get("postDate") or
                job.get("publishDate") or ""
            ),
            "description": description,
            "recruiter": recruiter if recruiter else None,
        }
        entry = {k: v for k, v in entry.items() if v is not None and v != "" and v != {}}
        jobs.append(entry)

    # Nach Datum sortieren (neueste zuerst)
    jobs.sort(key=lambda j: j.get("date_posted", ""), reverse=True)

    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    # jobs.json speichern
    output = {
        "last_updated": timestamp,
        "total_active": len(jobs),
        "jobs": jobs
    }
    with open("jobs.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"✅ jobs.json gespeichert – {len(jobs)} Jobs!")

    # ── Nach Bundesstaat + Stadt gruppieren ──────────────────────────────────
    regions = {}
    for j in jobs:
        state = j.get("state", "Unknown")
        city = j.get("city", "Unknown")
        key = (state, city)
        if key not in regions:
            regions[key] = []
        regions[key].append(j)

    sorted_regions = sorted(regions.keys(), key=lambda k: (k[0].lower(), k[1].lower()))

    region_slugs = {
        (state, city): f"region-{slugify(state)}-{slugify(city)}"
        for state, city in sorted_regions
    }

    # ── Regionale HTML-Dateien ────────────────────────────────────────────────
    os.makedirs("regions", exist_ok=True)
    for (state, city) in sorted_regions:
        slug = region_slugs[(state, city)]
        region_jobs = regions[(state, city)]
        rows = ""
        for j in region_jobs:
            field_tag = f"[{j['job_field']}] " if j.get("job_field") else ""
            level_tag = f"[{j['job_level']}] " if j.get("job_level") else ""
            recruiter_info = ""
            if j.get("recruiter"):
                r = j["recruiter"]
                parts = [r["name"]] if r.get("name") else []
                if r.get("email"):
                    parts.append(f'<a href="mailto:{r["email"]}">{r["email"]}</a>')
                recruiter_info = f'<p class="recruiter">Recruiter: {", ".join(parts)}</p>'
            short_desc = j.get("description", "")
            rows += (
                f'<div class="job-card">'
                f'<h3><a href="{j.get("url", "")}">{j.get("title", "")}</a></h3>'
                f'<p>{field_tag}{level_tag}{j.get("date_posted", "")[:10]}</p>'
                f'<p>{short_desc[:200]}{"..." if len(short_desc) > 200 else ""}</p>'
                f'{recruiter_info}'
                f'</div>\n'
            )
        html = (
            f'<!DOCTYPE html>\n<html lang="en">\n'
            f'<head><meta charset="UTF-8"><title>BASF Jobs – {city}, {state}</title></head>\n'
            f'<body>\n<h1>BASF Jobs – {city}, {state}</h1>\n'
            f'<p>{len(region_jobs)} open position(s)</p>\n'
            f'{rows}</body>\n</html>'
        )
        with open(f"regions/{slug}.html", "w", encoding="utf-8") as f:
            f.write(html)
    print(f"✅ {len(sorted_regions)} regionale HTML-Dateien gespeichert!")

    # ── index.html ────────────────────────────────────────────────────────────
    current_state = None
    index_rows = ""
    for (state, city) in sorted_regions:
        if state != current_state:
            if current_state is not None:
                index_rows += "</ul></li>\n"
            index_rows += f"<li><strong>{state}</strong><ul>\n"
            current_state = state
        region_url = f"{BASE_URL}/regions/{region_slugs[(state, city)]}.html"
        index_rows += f'<li><a href="{region_url}">{city}</a><ul>\n'
        for j in regions[(state, city)]:
            field_tag = f"[{j['job_field']}] " if j.get("job_field") else ""
            level_tag = f"[{j['job_level']}] " if j.get("job_level") else ""
            index_rows += (
                f'<li>{j.get("date_posted", "")[:10]} – '
                f'{field_tag}{level_tag}'
                f'<a href="{j.get("url", "")}">{j.get("title", "")}</a></li>\n'
            )
        index_rows += "</ul></li>\n"
    if current_state is not None:
        index_rows += "</ul></li>\n"

    index_html = (
        f'<!DOCTYPE html>\n<html lang="en">\n'
        f'<head><meta charset="UTF-8"><title>BASF Jobs India – Overview</title></head>\n'
        f'<body>\n<h1>BASF Jobs India</h1>\n'
        f'<p>Total: {len(jobs)} positions | {len(sorted_regions)} locations</p>\n'
        f'<ul>\n{index_rows}</ul>\n</body>\n</html>'
    )
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(index_html)
    print("✅ index.html gespeichert!")

    # ── index_lite.html ───────────────────────────────────────────────────────
    lite_rows = ""
    current_state = None
    for (state, city) in sorted_regions:
        if state != current_state:
            if current_state is not None:
                lite_rows += "</ul>\n"
            lite_rows += f"<h2>{state}</h2>\n<ul>\n"
            current_state = state
        region_url = f"{BASE_URL}/regions/{region_slugs[(state, city)]}.html"
        count = len(regions[(state, city)])
        lite_rows += f'<li><a href="{region_url}">{city}</a> ({count} positions)</li>\n'
    if current_state is not None:
        lite_rows += "</ul>\n"

    lite_html = (
        f'<!DOCTYPE html>\n<html lang="en">\n'
        f'<head><meta charset="UTF-8"><title>BASF Jobs India – Location Overview</title></head>\n'
        f'<body>\n<h1>BASF Job Openings India</h1>\n'
        f'<p>Total: {len(jobs)} positions | {len(sorted_regions)} locations</p>\n'
        f'{lite_rows}</body>\n</html>'
    )
    with open("index_lite.html", "w", encoding="utf-8") as f:
        f.write(lite_html)
    print("✅ index_lite.html gespeichert!")


asyncio.run(scrape_jobs())
