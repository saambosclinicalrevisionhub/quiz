import os
import json
import re
import time
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


