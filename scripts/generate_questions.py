import os
import json
import re
import asyncio
import hashlib
import requests

from urllib.parse import urljoin, urlparse
from datetime import datetime, timezone
from playwright.async_api import async_playwright

DOMAIN = "clinical.saambulance.sa.gov.au"

ROOT_URL = "https://clinical.saambulance.sa.gov.au"
HOME_URL = "https://clinical.saambulance.sa.gov.au/tabs/home"
MEDICINES_URL = "https://clinical.saambulance.sa.gov.au/tabs/medicines"
CALCULATORS_URL = "https://clinical.saambulance.sa.gov.au/tabs/calculators"

START_URLS = [
    HOME_URL,
    MEDICINES_URL,
    CALCULATORS_URL
]

incremental_paramedic_test = True

EXCLUDED_URL_PREFIXES = [
    "https://clinical.saambulance.sa.gov.au/tabs/checklists",
    "https://clinical.saambulance.sa.gov.au/tabs/checklists/cppro-s",
    "https://clinical.saambulance.sa.gov.au/tabs/tools"
]

CLINICAL_LEVELS = [
    "Paramedic"
]

MAX_VISITED_URLS_PER_LEVEL = 40
MAX_CONTENT_PAGES_PER_LEVEL = 8
STOP_IF_NO_NEW_CONTENT_FOR = 12
QUESTIONS_PER_PAGE = 3

GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash"
]

DATA_DIR = "data"

QUESTIONS_PATH = os.path.join(DATA_DIR, "questions.json")
QUESTIONS_BY_SOURCE_PATH = os.path.join(DATA_DIR, "questions_by_source.json")
PAGE_HASHES_PATH = os.path.join(DATA_DIR, "page_hashes.json")
METADATA_PATH = os.path.join(DATA_DIR, "metadata.json")
CRAWL_LOG_PATH = os.path.join(DATA_DIR, "crawl_log.json")
CLICK_LOG_PATH = os.path.join(DATA_DIR, "click_log.json")
SOURCE_PAGES_PATH = os.path.join(DATA_DIR, "source_pages.json")
GEMINI_DEBUG_PATH = os.path.join(DATA_DIR, "gemini_debug.json")

api_key = os.environ.get("GEMINI_API_KEY")

if not api_key:
    raise RuntimeError("GEMINI_API_KEY is missing.")

os.makedirs(DATA_DIR, exist_ok=True)


def load_json(path, default):
    if not os.path.exists(path):
        return default

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def clean_url(url):
    if not url:
        return ""

    return url.split("#")[0].rstrip("/")


def is_internal(url):
    parsed = urlparse(url)
    return parsed.netloc == DOMAIN


def is_excluded_url(url):
    cleaned = clean_url(url)

    for prefix in EXCLUDED_URL_PREFIXES:
        if cleaned.startswith(prefix):
            return True

    return False


def is_allowed_site_area(url):
    cleaned = clean_url(url)

    if not is_internal(cleaned):
        return False

    if is_excluded_url(cleaned):
        return False

    allowed_prefixes = [
        HOME_URL,
        MEDICINES_URL,
        CALCULATORS_URL
    ]

    for prefix in allowed_prefixes:
        if cleaned.startswith(prefix):
            return True

    return False


def useful_url(url):
    cleaned = clean_url(url)

    if not cleaned:
        return False

    if not is_internal(cleaned):
        return False

    if is_excluded_url(cleaned):
        return False

    if not is_allowed_site_area(cleaned):
        return False

    bad_parts = [
        "/tabs/checklists",
        "/cppro",
        "/favourites",
        "/favorites",
        "/recent"
    ]

    for part in bad_parts:
        if part in cleaned.lower():
            return False

    return True


def categorise_url(url):
    cleaned = clean_url(url)

    if cleaned.startswith(MEDICINES_URL):
        return "Medicine"

    if cleaned.startswith(CALCULATORS_URL):
        return "Calculator"

    if cleaned.startswith(HOME_URL):
        return "Clinical Practice Guideline"

    return "Other"


def is_detail_content_page(url):
    cleaned = clean_url(url)

    if not is_allowed_site_area(cleaned):
        return False

    if cleaned in [ROOT_URL, HOME_URL, MEDICINES_URL, CALCULATORS_URL]:
        return False

    if cleaned.startswith(HOME_URL) and "/page/" in cleaned:
        return True

    if cleaned.startswith(MEDICINES_URL + "/"):
        return True

    if cleaned.startswith(CALCULATORS_URL + "/"):
        return True

    return False


def looks_like_disclaimer_text(text):
    lowered = text.lower()

    disclaimer_terms = [
        "not intended to serve as health",
        "medical or treatment advice",
        "information purposes only",
        "saas does not represent or warrant",
        "to the maximum extent permitted by law",
        "liability in negligence",
        "external websites",
        "does not endorse any external website"
    ]

    count = 0

    for term in disclaimer_terms:
        if term in lowered:
            count = count + 1

    return count >= 2


def looks_like_real_content(text):
    lowered = text.lower()

    content_terms = [
        "principle",
        "principles",
        "guideline",
        "indications",
        "contraindications",
        "management",
        "assessment",
        "treatment",
        "dose",
        "dosing",
        "administration",
        "precautions",
        "clinical",
        "medicine",
        "calculator",
        "calculation",
        "considerations"
    ]

    for term in content_terms:
        if term in lowered:
            return True

    return False


def normalise_text_for_hash(text):
    text = text.lower()
    text = re.sub(r"\s+", " ", text)
    text = text.strip()
    return text


def calculate_hash(text):
    normalised = normalise_text_for_hash(text)
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def make_source_key(clinical_level, url):
    return clinical_level + "|" + clean_url(url)


def extract_urls_from_text_and_html(text, html, base_url):
    found = set()
    combined = text + "\n" + html

    absolute_matches = re.findall(
        r"https://clinical\.saambulance\.sa\.gov\.au/[A-Za-z0-9_\-/\.\?\=\&%]+",
        combined
    )

    relative_matches = re.findall(
        r"/tabs/[A-Za-z0-9_\-/\.\?\=\&%]+",
        combined
    )

    for match in absolute_matches:
        url = clean_url(match)

        if useful_url(url):
            found.add(url)

    for match in relative_matches:
        url = clean_url(urljoin(base_url, match))

        if useful_url(url):
            found.add(url)

    return found


async def click_text_if_present(page, texts):
    for text in texts:
        try:
            locator = page.get_by_text(text, exact=True)

            if await locator.count() > 0:
                await locator.first.click(timeout=5000)
                await page.wait_for_timeout(2500)
                return True

        except Exception:
            pass

    return False


async def click_disclaimer_ok_if_present(page):
    return await click_text_if_present(
        page,
        [
            "OK",
            "Ok",
            "I agree",
            "Agree",
            "Accept",
            "Continue"
        ]
    )


async def click_level_on_select_page(page, level):
    selected = False

    print(f"Trying to select level: {level}")

    try:
        locator = page.get_by_text(level, exact=True)

        if await locator.count() > 0:
            await locator.first.click(timeout=7000)
            await page.wait_for_timeout(4000)
            selected = True
            print(f"Selected level by exact text: {level}")

    except Exception as error:
        print(f"Could not click exact level text {level}: {error}")

    if selected:
        return True

    try:
        locator = page.get_by_text(level, exact=False)

        if await locator.count() > 0:
            await locator.first.click(timeout=7000)
            await page.wait_for_timeout(4000)
            selected = True
            print(f"Selected level by partial text: {level}")

    except Exception as error:
        print(f"Could not click partial level text {level}: {error}")

    if selected:
        return True

    try:
        candidates = await page.locator(
            "button, a, ion-item, mat-list-item, div, span"
        ).all()

        for element in candidates[:150]:
            try:
                if not await element.is_visible(timeout=500):
                    continue

                label = await element.inner_text(timeout=1000)
                label = re.sub(r"\s+", " ", label).strip()

                if label == level:
                    await element.click(timeout=7000)
                    await page.wait_for_timeout(4000)
                    selected = True
                    print(f"Selected level by element scan: {level}")
                    break

            except Exception:
                continue

    except Exception:
        pass

    return selected


async def prepare_site_for_level(page, level):
    await page.goto(ROOT_URL, wait_until="networkidle", timeout=60000)
    await page.wait_for_timeout(3000)

    await click_disclaimer_ok_if_present(page)
    await page.wait_for_timeout(3000)

    try:
        body_text = await page.locator("body").inner_text(timeout=30000)
    except Exception:
        body_text = ""

    selected = False

    if "select your level" in body_text.lower() or level in body_text:
        selected = await click_level_on_select_page(page, level)

    if not selected:
        try:
            await page.goto(HOME_URL, wait_until="networkidle", timeout=60000)
            await page.wait_for_timeout(3000)

            await click_disclaimer_ok_if_present(page)
            await page.wait_for_timeout(2000)

            body_text = await page.locator("body").inner_text(timeout=30000)

            if "select your level" in body_text.lower() or level in body_text:
                selected = await click_level_on_select_page(page, level)

        except Exception as error:
            print(f"Could not navigate to level screen for {level}: {error}")

    await page.wait_for_timeout(4000)

    try:
        await page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        pass

    return selected


async def prepare_current_page_if_needed(page, level):
    try:
        await click_disclaimer_ok_if_present(page)

        body_text = await page.locator("body").inner_text(timeout=30000)

        if "select your level" in body_text.lower():
            await click_level_on_select_page(page, level)

    except Exception:
        pass


async def collect_basic_links(page, base_url):
    urls = set()

    try:
        links = await page.eval_on_selector_all(
            "a[href]",
            "els => els.map(a => a.href)"
        )

        for href in links:
            href = clean_url(href)

            if useful_url(href):
                urls.add(href)

    except Exception:
        pass

    try:
        text = await page.locator("body").inner_text(timeout=30000)
        html = await page.content()
        urls.update(extract_urls_from_text_and_html(text, html, base_url))

    except Exception:
        pass

    return urls


async def collect_click_discovered_links(page):
    urls = set()
    clicked_labels = []

    selectors = "button, a, ion-item, mat-list-item, [role=button], div, span"

    try:
        elements = await page.locator(selectors).all()
    except Exception:
        return urls, clicked_labels

    for element in elements[:120]:
        try:
            if not await element.is_visible(timeout=500):
                continue

            label = await element.inner_text(timeout=1000)
            label = re.sub(r"\s+", " ", label).strip()

            if len(label) < 3 or len(label) > 90:
                continue

            current_url = clean_url(page.url)

            await element.click(timeout=3000)
            await page.wait_for_timeout(1500)

            new_url = clean_url(page.url)

            if useful_url(new_url):
                urls.add(new_url)
                clicked_labels.append(label)

            if new_url != current_url:
                try:
                    await page.go_back(wait_until="networkidle", timeout=15000)
                    await page.wait_for_timeout(1000)
                except Exception:
                    try:
                        await page.goto(current_url, wait_until="networkidle", timeout=30000)
                    except Exception:
                        pass

        except Exception:
            continue

    return urls, clicked_labels


async def extract_page_content(page, url, level):
    try:
        await page.goto(url, wait_until="networkidle", timeout=60000)
    except Exception:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)

    await page.wait_for_timeout(2500)
    await prepare_current_page_if_needed(page, level)
    await page.wait_for_timeout(1500)

    try:
        text = await page.locator("body").inner_text(timeout=30000)
    except Exception:
        text = ""

    try:
        title = await page.title()
    except Exception:
        title = ""

    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    return {
        "url": clean_url(url),
        "title": title.strip(),
        "type": categorise_url(url),
        "text": text,
        "hash": calculate_hash(text)
    }


def build_prompt(page_record, clinical_level):
    source_type = page_record.get("type", "Clinical Practice Guideline")
    title = page_record.get("title", "")
    url = page_record.get("url", "")
    text = page_record.get("text", "")[:9000]

    return f"""
You are generating revision quiz questions for Australian paramedicine students.

Clinical level: {clinical_level}
Source type: {source_type}
Source title: {title}
Source URL: {url}

Use only the source text below. Do not invent facts.
Focus on principles, guidelines, indications, contraindications, management, assessment, treatment, doses, precautions, or clinically relevant calculations.

Return valid JSON only. Return an array of exactly {QUESTIONS_PER_PAGE} objects.
Each object must have these keys:
- question
- options
- correctAnswer
- explanation
- clinicalLevel
- sourceUrl
- sourceTitle
- sourceType

For options, provide an array of exactly 4 answer options.
The correctAnswer must exactly match one of the options.
Keep explanations brief and educational.

SOURCE TEXT:
{text}
""".strip()


def call_gemini(prompt):
    last_error = None

    for model in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

        payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": prompt
                        }
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 4096
            }
        }

        try:
            response = requests.post(url, json=payload, timeout=90)

            if response.status_code != 200:
                last_error = f"{model}: HTTP {response.status_code} {response.text[:500]}"
                continue

            data = response.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]

            text = text.strip()
            text = re.sub(r"^```json", "", text).strip()
            text = re.sub(r"^```", "", text).strip()
            text = re.sub(r"```$", "", text).strip()

            questions = json.loads(text)

            if isinstance(questions, list):
                return questions, model, None

            last_error = f"{model}: JSON response was not a list"

        except Exception as error:
            last_error = f"{model}: {error}"

    return [], None, last_error


def normalise_question(question, page_record, clinical_level):
    q = dict(question)

    q["clinicalLevel"] = clinical_level
    q["sourceUrl"] = page_record.get("url", "")
    q["sourceTitle"] = page_record.get("title", "")
    q["sourceType"] = page_record.get("type", "Clinical Practice Guideline")
    q["updatedAt"] = now_iso()

    return q


async def crawl_for_level(browser, clinical_level, previous_hashes):
    context = await browser.new_context()
    page = await context.new_page()

    selected = await prepare_site_for_level(page, clinical_level)

    crawl_log = {
        "clinicalLevel": clinical_level,
        "selectedLevel": selected,
        "incrementalParamedicTest": incremental_paramedic_test,
        "startedAt": now_iso(),
        "visited": [],
        "contentPages": [],
        "unchangedPages": [],
        "changedPages": [],
        "newPages": [],
        "errors": []
    }

    click_log = []
    queue = list(START_URLS)
    seen = set()
    content_pages = []
    no_new_content_count = 0

    while queue and len(seen) < MAX_VISITED_URLS_PER_LEVEL:
        url = clean_url(queue.pop(0))

        if url in seen:
            continue

        if not useful_url(url):
            continue

        seen.add(url)

        print(f"Visiting {clinical_level}: {url}")

        try:
            page_record = await extract_page_content(page, url, clinical_level)
            text = page_record.get("text", "")

            crawl_log["visited"].append(url)

            basic_links = await collect_basic_links(page, url)
            click_links, labels = await collect_click_discovered_links(page)

            click_log.append({
                "url": url,
                "labelsClicked": labels,
                "linksFound": sorted(click_links)
            })

            discovered_links = basic_links.union(click_links)

            for found in sorted(discovered_links):
                if found not in seen and found not in queue and useful_url(found):
                    queue.append(found)

            if is_detail_content_page(url) and len(text) >= 500 and looks_like_real_content(text) and not looks_like_disclaimer_text(text):
                source_key = make_source_key(clinical_level, url)
                old_hash = previous_hashes.get(source_key)
                new_hash = page_record["hash"]

                page_record["clinicalLevel"] = clinical_level
                page_record["sourceKey"] = source_key
                page_record["capturedAt"] = now_iso()

                content_pages.append(page_record)
                crawl_log["contentPages"].append(url)

                if old_hash is None:
                    crawl_log["newPages"].append(url)
                    no_new_content_count = 0
                elif old_hash != new_hash:
                    crawl_log["changedPages"].append(url)
                    no_new_content_count = 0
                else:
                    crawl_log["unchangedPages"].append(url)
                    no_new_content_count = no_new_content_count + 1

                if len(content_pages) >= MAX_CONTENT_PAGES_PER_LEVEL:
                    print("Content page limit reached for this test run.")
                    break

                if no_new_content_count >= STOP_IF_NO_NEW_CONTENT_FOR:
                    print("Stopping early because no new or changed content has been found recently.")
                    break

        except Exception as error:
            crawl_log["errors"].append({
                "url": url,
                "error": str(error)
            })

            print(f"Error visiting {url}: {error}")

    crawl_log["finishedAt"] = now_iso()

    await context.close()

    return content_pages, crawl_log, click_log


def rebuild_flat_questions(questions_by_source):
    flat = []

    for source_key in sorted(questions_by_source.keys()):
        items = questions_by_source[source_key]

        if isinstance(items, list):
            flat.extend(items)

    return flat


async def main():
    previous_hashes = load_json(PAGE_HASHES_PATH, {})
    questions_by_source = load_json(QUESTIONS_BY_SOURCE_PATH, {})
    old_source_pages = load_json(SOURCE_PAGES_PATH, {})

    if isinstance(previous_hashes, list):
        previous_hashes = {}

    if isinstance(questions_by_source, list):
        questions_by_source = {}

    if isinstance(old_source_pages, list):
        old_source_pages = {}

    all_crawl_logs = []
    all_click_logs = []

    new_hashes = dict(previous_hashes)
    new_source_pages = dict(old_source_pages)

    changed_or_new_source_keys = []
    discovered_source_keys_this_run = set()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        for clinical_level in CLINICAL_LEVELS:
            content_pages, crawl_log, click_log = await crawl_for_level(
                browser,
                clinical_level,
                previous_hashes
            )

            all_crawl_logs.append(crawl_log)
            all_click_logs.extend(click_log)

            for page_record in content_pages:
                source_key = page_record["sourceKey"]
                discovered_source_keys_this_run.add(source_key)

                old_hash = previous_hashes.get(source_key)
                new_hash = page_record["hash"]

                new_hashes[source_key] = new_hash

                new_source_pages[source_key] = {
                    "clinicalLevel": page_record["clinicalLevel"],
                    "url": page_record["url"],
                    "title": page_record["title"],
                    "type": page_record["type"],
                    "hash": page_record["hash"],
                    "capturedAt": page_record["capturedAt"]
                }

                if old_hash != new_hash or not questions_by_source.get(source_key):
                    changed_or_new_source_keys.append(source_key)

                    prompt = build_prompt(page_record, clinical_level)
                    questions, model, error = call_gemini(prompt)

                    debug_record = load_json(GEMINI_DEBUG_PATH, [])

                    if not isinstance(debug_record, list):
                        debug_record = []

                    debug_record.append({
                        "sourceKey": source_key,
                        "url": page_record["url"],
                        "model": model,
                        "error": error,
                        "questionCount": len(questions),
                        "updatedAt": now_iso()
                    })

                    save_json(GEMINI_DEBUG_PATH, debug_record)

                    if questions:
                        questions_by_source[source_key] = [
                            normalise_question(q, page_record, clinical_level) for q in questions
                        ]
                    else:
                        print(f"No questions generated for {source_key}: {error}")

        await browser.close()

    if incremental_paramedic_test:
        scope_prefixes = [level + "|" for level in CLINICAL_LEVELS]

        for source_key in list(questions_by_source.keys()):
            in_scope = any(source_key.startswith(prefix) for prefix in scope_prefixes)

            if in_scope and source_key not in discovered_source_keys_this_run:
                print(f"Removing questions for removed or no longer discovered source: {source_key}")
                questions_by_source.pop(source_key, None)
                new_hashes.pop(source_key, None)
                new_source_pages.pop(source_key, None)

    flat_questions = rebuild_flat_questions(questions_by_source)

    metadata = {
        "generatedAt": now_iso(),
        "mode": "incremental_paramedic_test",
        "clinicalLevels": CLINICAL_LEVELS,
        "incrementalParamedicTest": incremental_paramedic_test,
        "questionCount": len(flat_questions),
        "sourceCount": len(questions_by_source),
        "changedOrNewSourceCount": len(changed_or_new_source_keys),
        "changedOrNewSourceKeys": changed_or_new_source_keys
    }

    save_json(QUESTIONS_BY_SOURCE_PATH, questions_by_source)
    save_json(QUESTIONS_PATH, flat_questions)
    save_json(PAGE_HASHES_PATH, new_hashes)
    save_json(SOURCE_PAGES_PATH, new_source_pages)
    save_json(METADATA_PATH, metadata)
    save_json(CRAWL_LOG_PATH, all_crawl_logs)
    save_json(CLICK_LOG_PATH, all_click_logs)

    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
