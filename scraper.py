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


async def fetch_jobs_page(page, page_number):
    """POST to /services/recruiting/v1/jobs from within the browser context."""
    return await page.evaluate(
        """async ([endpoint, pageNumber]) => {
            try {
                const resp = await fetch(endpoint, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRF-Token': window.CSRFToken || ''
                    },
                    body: JSON.stringify({
                        locale: 'en_US',
                        pageNumber: pageNumber,
                        sortBy: 'date',
                        keywords: '',
                        location: 'India',
                        facetFilters: {},
                        brand: '',
                        skills: [],
                        categoryId: 0
                    })
                });
                if (!resp.ok) {
                    const body = await resp.text();
                    return { __error: `HTTP ${resp.status}: ${body.slice(0, 300)}` };
                }
                return await resp.json();
            } catch (e) {
                return { __error: String(e) };
            }
        }""",
        [SF_ENDPOINT, page_number]
    )


async def scrape_jobs():
    all_raw_jobs = []

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()

        print("Lade basf.jobs/search ...")
        await page.goto(
            "https://basf.jobs/search",
            timeout=60000,
            wait_until="domcontentloaded"
        )
        await page.wait_for_timeout(4000)

        csrf_token = await page.evaluate("window.CSRFToken")
        if not csrf_token:
            print("❌ Kein CSRF Token gefunden!")
            await browser.close()
            return
        print("✅ CSRF Token gefunden")

        page_number = 0
        total_reported = None
        detected_page_size = None

        while True:
            print(f"  Lade Seite {page_number} ...")
            data = await fetch_jobs_page(page, page_number)

            if "__error" in data:
                print(f"❌ Fehler auf Seite {page_number}: {data['__error']}")
                break

            batch = (
                data.get("jobs") or
                data.get("jobResults") or
                data.get("results") or
                []
            )

            if page_number == 0:
                total_reported = (
                    data.get("total") or
                    data.get("noOfJobs") or
                    data.get("totalCount") or
                    data.get("count")
                )
                print(f"  API meldet insgesamt: {total_reported} Jobs")

            if not batch:
                print(f"  Keine Jobs auf Seite {page_number}, Abbruch.")
                break

            if detected_page_size is None:
                detected_page_size = (
                    data.get("pageSize") or
                    data.get("resultsPerPage") or
                    len(batch)
                )

            all_raw_jobs.extend(batch)
            print(f"  Seite {page_number}: {len(batch)} Jobs (gesamt: {len(all_raw_jobs)})")

            if isinstance(total_reported, int) and len(all_raw_jobs) >= total_reported:
                break
            if detected_page_size and len(batch) < detected_page_size:
                break
            page_number += 1

        await browser.close()

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
