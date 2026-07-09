import os
import json
import re
import time
import asyncio
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
    "gemini-2.0-flash",
    "gemini-1.5-flash"
]

api_key = os.environ.get("GEMINI_API_KEY")

if not api_key:
    raise RuntimeError("GEMINI_API_KEY is missing.")

os.makedirs("data", exist_ok=True)


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


def extract_urls_from_text_and_html(text, html, base_url):
    found = set()
    combined = text + "\n" + html

    absolute_matches = re.findall(
        r"https://clinical\.saambulance\.sa\.gov\.au/[A-Za-z0-9_\-\/\.\?\=\&%]+",
        combined
    )

    relative_matches = re.findall(
        r"/tabs/[A-Za-z0-9_\-\/\.\?\=\&%]+",
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

        for link in links:
            url = clean_url(urljoin(base_url, link))

            if useful_url(url):
                urls.add(url)

    except Exception:
        pass

    try:
        html = await page.content()
        text = await page.locator("body").inner_text(timeout=30000)

        extracted_urls = extract_urls_from_text_and_html(text, html, base_url)

        for url in extracted_urls:
            if useful_url(url):
                urls.add(url)

    except Exception:
        pass

    return urls


async def collect_clickable_routes(context, url, level):
    discovered = set()
    clicked_labels = []

    page = await context.new_page()

    try:
        await page.goto(url, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(3000)

        await prepare_current_page_if_needed(page, level)
        await page.wait_for_timeout(2500)

        basic_links = await collect_basic_links(page, page.url)

        for link in basic_links:
            discovered.add(link)

        candidates = await page.locator(
            "a, button, ion-card, ion-item, mat-card"
        ).all()

        for element in candidates[:100]:
            try:
                if not await element.is_visible(timeout=700):
                    continue

                label = await element.inner_text(timeout=1000)
                label = re.sub(r"\s+", " ", label).strip()

                if not label:
                    continue

                if len(label) > 120:
                    continue

                lower_label = label.lower()

                skip_terms = [
                    "select your level",
                    "set your clinical level",
                    "disclaimer",
                    "ok",
                    "level",
                    "recent",
                    "favourites",
                    "favorites",
                    "tools",
                    "checklists",
                    "cppro",
                    "search"
                ]

                skip = False

                for term in skip_terms:
                    if term in lower_label:
                        skip = True
                        break

                if skip:
                    continue

                before_url = clean_url(page.url)

                await element.click(timeout=5000)
                await page.wait_for_timeout(2000)

                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass

                await prepare_current_page_if_needed(page, level)

                after_url = clean_url(page.url)

                if useful_url(after_url) and after_url != before_url:
                    discovered.add(after_url)

                    clicked_labels.append(
                        {
                            "clinical_level": level,
                            "from": url,
                            "label": label[:120],
                            "to": after_url
                        }
                    )

                more_links = await collect_basic_links(page, after_url)

                for link in more_links:
                    discovered.add(link)

                await page.goto(url, wait_until="networkidle", timeout=60000)
                await page.wait_for_timeout(1500)
                await prepare_current_page_if_needed(page, level)

            except Exception:
                try:
                    await page.goto(url, wait_until="networkidle", timeout=60000)
                    await page.wait_for_timeout(1000)
                    await prepare_current_page_if_needed(page, level)

                except Exception:
                    pass

                continue

    except Exception as error:
        print(f"[{level}] Click discovery failed on {url}: {error}")

    await page.close()

    return discovered, clicked_labels


async def get_rendered_page(context, url, level):
    page = await context.new_page()

    await page.goto(url, wait_until="networkidle", timeout=60000)
    await page.wait_for_timeout(3000)

    await prepare_current_page_if_needed(page, level)
    await page.wait_for_timeout(3000)

    text = await page.locator("body").inner_text(timeout=30000)
    html = await page.content()
    title = await page.title()

    links = await page.eval_on_selector_all(
        "a[href]",
        "els => els.map(a => a.href)"
    )

    await page.close()

    return title, text, links, html


async def initialise_level_context(browser, level):
    context = await browser.new_context()
    page = await context.new_page()

    selected = False
    current_url = ""
    home_text = ""
    home_html = ""
    links = []

    try:
        selected = await prepare_site_for_level(page, level)
        current_url = clean_url(page.url)
        home_text = await page.locator("body").inner_text(timeout=30000)
        home_html = await page.content()

        links = await page.eval_on_selector_all(
            "a[href]",
            "els => els.map(a => a.href)"
        )

    except Exception as error:
        print(f"Failed to initialise level context for {level}: {error}")

    await page.close()

    return context, selected, current_url, home_text, home_html, links


async def crawl_for_level(browser, level):
    context, selected, current_url, home_text, home_html, starting_links = await initialise_level_context(browser, level)

    crawl_log = [
        {
            "clinical_level": level,
            "selected_level_successfully": selected,
            "current_url_after_selection": current_url,
            "home_text_length": len(home_text)
        }
    ]

    click_log = []
    source_pages = []
    queue = []
    visited = set()
    pages = []

    if useful_url(current_url):
        queue.append(clean_url(current_url))

    for start_url in START_URLS:
        if useful_url(start_url):
            queue.append(clean_url(start_url))

    for link in starting_links:
        link = clean_url(urljoin(HOME_URL, link))

        if useful_url(link):
            queue.append(link)

    for link in extract_urls_from_text_and_html(home_text, home_html, HOME_URL):
        if useful_url(link):
            queue.append(link)

    if current_url:
        try:
            discovered, clicked = await collect_clickable_routes(context, current_url, level)

            for item in clicked:
                click_log.append(item)

            for link in discovered:
                if useful_url(link):
                    queue.append(link)

        except Exception as error:
            print(f"[{level}] Initial click discovery failed: {error}")

    queue = list(dict.fromkeys([q for q in queue if useful_url(q)]))

    pages_since_new_content = 0

    while queue and len(visited) < MAX_VISITED_URLS_PER_LEVEL and len(pages) < MAX_CONTENT_PAGES_PER_LEVEL:
        if pages_since_new_content >= STOP_IF_NO_NEW_CONTENT_FOR:
            print(f"[{level}] Stopping after {STOP_IF_NO_NEW_CONTENT_FOR} pages without new content.")
            break

        url = clean_url(queue.pop(0))

        if url in visited:
            continue

        if not useful_url(url):
            continue

        visited.add(url)

        try:
            print(f"[{level}] Opening: {url}")
            title, text, links, html = await get_rendered_page(context, url, level)

        except Exception as error:
            msg = f"[{level}] Failed to open {url}: {error}"
            print(msg)
            crawl_log.append(msg)
            pages_since_new_content = pages_since_new_content + 1
            continue

        text = text.strip()
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)

        content_category = categorise_url(url)

        msg = f"[{level}] Extracted {len(text)} characters from {url} [{content_category}]"
        print(msg)
        crawl_log.append(msg)

        if (
            len(text) > 300
            and is_detail_content_page(url)
            and not looks_like_disclaimer_text(text)
            and looks_like_real_content(text)
        ):
            pages.append(
                {
                    "clinical_level": level,
                    "content_category": content_category,
                    "title": title,
                    "url": url,
                    "text": text[:14000]
                }
            )

            source_pages.append(
                {
                    "clinical_level": level,
                    "content_category": content_category,
                    "title": title,
                    "url": url,
                    "text_length": len(text)
                }
            )

            pages_since_new_content = 0

        else:
            pages_since_new_content = pages_since_new_content + 1
            print(f"[{level}] Discovery page only: {url}")

        for link in links:
            link = clean_url(urljoin(url, link))

            if useful_url(link) and link not in visited and link not in queue:
                queue.append(link)

        for link in extract_urls_from_text_and_html(text, html, url):
            if useful_url(link) and link not in visited and link not in queue:
                queue.append(link)

        try:
            discovered, clicked = await collect_clickable_routes(context, url, level)

            for item in clicked:
                click_log.append(item)

            for link in discovered:
                if useful_url(link) and link not in visited and link not in queue:
                    queue.append(link)

        except Exception as error:
            print(f"[{level}] Click discovery failed during crawl: {error}")

        await asyncio.sleep(0.4)

    await context.close()

    return pages, crawl_log, click_log, source_pages


def extract_json(text):
    text = text.strip()
    text = re.sub(r"^```json", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"^```", "", text).strip()
    text = re.sub(r"```$", "", text).strip()

    start = text.find("[")
    end = text.rfind("]")

    if start == -1 or end == -1:
        print("No JSON array found in Gemini response.")
        print(text[:1000])
        return []

    try:
        return json.loads(text[start:end + 1])

    except Exception as error:
        print("JSON parse error:", error)
        print(text[:1000])
        return []


def call_gemini(model, prompt):
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
            "temperature": 0.1,
            "maxOutputTokens": 4096
        }
    }

    response = requests.post(url, json=payload, timeout=90)
    response.raise_for_status()

    data = response.json()

    return data["candidates"][0]["content"]["parts"][0]["text"]


def generate_from_page(page):
    clinical_level = page["clinical_level"]
    content_category = page["content_category"]

    prompt = f'''
You are creating public formative revision questions for undergraduate paramedicine students.

Selected clinical level:
{clinical_level}

Source content category:
{content_category}

Use ONLY the source text supplied below.

Rules:
- Do not use outside knowledge.
- Do not infer anything not explicitly stated in the source text.
- Do not provide clinical advice or operational advice.
- Do not create questions from disclaimers, copyright statements, legal statements, website liability text, navigation text, Tools, Checklists, or CPPROs.
- If the source text does not contain useful clinical practice guideline, medicine, or calculator content relevant to the selected clinical level, return [].
- The explanation must briefly explain why the answer is correct using only the source text.

Create up to {QUESTIONS_PER_PAGE} questions.
Prefer 2 multiple choice and 2 true/false.

Return ONLY a valid JSON array.
Do not include markdown.

Each item must contain:
question, type, options, answer, explanation, source, source_title, clinical_level, content_category.

For MCQ, options must contain exactly 4 strings and answer must match one option exactly.
For true/false, options must be ["True", "False"] and answer must be "True" or "False".

Source title:
{page["title"]}

Source URL:
{page["url"]}

Source text:
{page["text"]}
'''

    for model in GEMINI_MODELS:
        try:
            print(f"[{clinical_level}] Trying Gemini model {model}")
            response_text = call_gemini(model, prompt)
            items = extract_json(response_text)

            if items:
                print(f"[{clinical_level}] Model {model} produced {len(items)} questions.")
                return items

        except Exception as error:
            print(f"[{clinical_level}] Model {model} failed: {error}")

    return []


def question_looks_like_disclaimer(question, answer, explanation, source_title):
    combined = (
        question + " " +
        answer + " " +
        explanation + " " +
        source_title
    ).lower()

    terms = [
        "liability",
        "not intended to serve as health",
        "medical or treatment advice",
        "information purposes only",
        "external websites",
        "does not represent or warrant",
        "maximum extent permitted by law",
        "does not endorse"
    ]

    for term in terms:
        if term in combined:
            return True

    return False


def clean_questions(items, clinical_level, content_category):
    cleaned = []

    for item in items:
        if not isinstance(item, dict):
            continue

        question = str(item.get("question", "")).strip()
        qtype = str(item.get("type", "")).strip().lower()
        answer = str(item.get("answer", "")).strip()
        explanation = str(item.get("explanation", "")).strip()
        source = str(item.get("source", "")).strip()
        source_title = str(item.get("source_title", "")).strip()
        options = item.get("options", [])

        if not question or not qtype or not answer or not source or not explanation:
            continue

        source_clean = clean_url(source)

        if is_excluded_url(source_clean):
            continue

        if source_clean in [ROOT_URL, HOME_URL]:
            continue

        if "Home | SA Ambulance Service" in source_title:
            continue

        if question_looks_like_disclaimer(question, answer, explanation, source_title):
            continue

        if qtype == "mcq":
            if not isinstance(options, list):
                continue

            options = [str(option).strip() for option in options if str(option).strip()]

            if len(options) != 4:
                continue

            if answer not in options:
                continue

        elif qtype == "tf":
            options = ["True", "False"]

            if answer not in ["True", "False"]:
                continue

        else:
            continue

        cleaned.append(
            {
                "question": question,
                "type": qtype,
                "options": options,
                "answer": answer,
                "explanation": explanation,
                "source": source,
                "source_title": source_title,
                "clinical_level": clinical_level,
                "content_category": content_category
            }
        )

    return cleaned


async def main():
    print("Starting temporary Paramedic-only test crawl.")

    all_pages = []
    all_questions = []
    all_crawl_logs = []
    all_click_logs = []
    all_source_pages = []
    seen_question_keys = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        for level in CLINICAL_LEVELS:
            print(f"Starting crawl for clinical level: {level}")

            pages, crawl_log, click_log, source_pages = await crawl_for_level(browser, level)

            print(f"{level}: crawled {len(pages)} usable detail pages.")

            all_pages.extend(pages)
            all_crawl_logs.extend(crawl_log)
            all_click_logs.extend(click_log)
            all_source_pages.extend(source_pages)

            for i, page in enumerate(pages, start=1):
                print(f"[{level}] Generating questions for page {i}/{len(pages)}: {page['url']}")

                raw = generate_from_page(page)
                valid = clean_questions(raw, level, page["content_category"])

                unique_valid = []

                for question in valid:
                    key = (
                        question["clinical_level"],
                        question["content_category"],
                        question["source"],
                        question["question"].lower().strip()
                    )

                    if key not in seen_question_keys:
                        seen_question_keys.add(key)
                        unique_valid.append(question)

                print(f"[{level}] Kept {len(unique_valid)} valid unique questions.")

                all_questions.extend(unique_valid)

                time.sleep(1)

        await browser.close()

    if not all_questions:
        all_questions = [
            {
                "question": "The quiz generator ran, but no valid questions were produced.",
                "type": "tf",
                "options": ["True", "False"],
                "answer": "True",
                "explanation": "No valid source-grounded questions were generated during this run.",
                "source": HOME_URL,
                "source_title": "Fallback",
                "clinical_level": "All",
                "content_category": "Fallback"
            }
        ]

    counts_by_level = {}
    counts_by_category = {}

    for question in all_questions:
        level = question.get("clinical_level", "Unknown")
        category = question.get("content_category", "Unknown")

        counts_by_level[level] = counts_by_level.get(level, 0) + 1
        counts_by_category[category] = counts_by_category.get(category, 0) + 1

    metadata = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "temporary_test_mode": True,
        "clinical_levels": CLINICAL_LEVELS,
        "included_sources": {
            "clinical_practice_guidelines": HOME_URL,
            "medicines": MEDICINES_URL,
            "calculators": CALCULATORS_URL
        },
        "excluded_sources": EXCLUDED_URL_PREFIXES,
        "max_visited_urls_per_level": MAX_VISITED_URLS_PER_LEVEL,
        "max_content_pages_per_level": MAX_CONTENT_PAGES_PER_LEVEL,
        "stop_if_no_new_content_for": STOP_IF_NO_NEW_CONTENT_FOR,
        "page_count": len(all_pages),
        "question_count": len(all_questions),
        "question_count_by_level": counts_by_level,
        "question_count_by_category": counts_by_category,
        "clicked_routes_count": len(all_click_logs)
    }

    with open("data/questions.json", "w", encoding="utf-8") as f:
        json.dump(all_questions, f, ensure_ascii=False, indent=2)

    with open("data/metadata.json", "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)

    with open("data/crawl_log.json", "w", encoding="utf-8") as f:
        json.dump(all_crawl_logs, f, ensure_ascii=False, indent=2)

    with open("data/click_log.json", "w", encoding="utf-8") as f:
        json.dump(all_click_logs, f, ensure_ascii=False, indent=2)

    with open("data/source_pages.json", "w", encoding="utf-8") as f:
        json.dump(all_source_pages, f, ensure_ascii=False, indent=2)

    print(f"Saved {len(all_questions)} questions from {len(all_pages)} detail pages.")


if __name__ == "__main__":
    asyncio.run(main())
