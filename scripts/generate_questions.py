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
    "Ambulance Assist",
    "Ambulance Responder",
    "Ambulance Officer",
    "Ambulance Officer Extended Scope",
    "Paramedic",
    "Intensive Care Paramedic",
    "Extended Care Paramedic"
]

MAX_VISITED_URLS_PER_LEVEL = 60
MAX_CONTENT_PAGES_PER_LEVEL = 10
STOP_IF_NO_NEW_CONTENT_FOR = 18
QUESTIONS_PER_PAGE = 2

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
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)


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


def strip_superfluous_question_prefix(question):
    text = str(question).strip()

    patterns = [
        r"^according to the sa ambulance service guidelines?,?\s+",
        r"^according to sa ambulance service guidelines?,?\s+",
        r"^according to the saas guidelines?,?\s+",
        r"^according to saas guidelines?,?\s+",
        r"^according to the guideline,?\s+",
        r"^according to the source text,?\s+",
        r"^based on the source text,?\s+",
        r"^based on the guideline,?\s+",
        r"^in the sa ambulance service guideline,?\s+",
        r"^in the saas guideline,?\s+"
    ]

    for pattern in patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE).strip()

    if text:
        text = text[0].upper() + text[1:]

    return text


def clean_generated_text(text):
    if text is None:
        return ""

    cleaned = str(text).strip()

    replacements = [
        (r"Paramedic\s{0,}\(\s{0,}Para\s{0,}\)", "Paramedic"),
        (r"Intensive Care Paramedic\s{0,}\(\s{0,}ICP\s{0,}\)", "Intensive Care Paramedic"),
        (r"Extended Care Paramedic\s{0,}\(\s{0,}ECP\s{0,}\)", "Extended Care Paramedic"),
        (r"Ambulance Officer Extended Scope\s{0,}\(\s{0,}AOES\s{0,}\)", "Ambulance Officer Extended Scope"),
        (r"Ambulance Officer\s{0,}\(\s{0,}AO\s{0,}\)", "Ambulance Officer"),
        (r"Ambulance Responder\s{0,}\(\s{0,}AR\s{0,}\)", "Ambulance Responder"),
        (r"Ambulance Assist\s{0,}\(\s{0,}AA\s{0,}\)", "Ambulance Assist"),
        (r"\(\s{0,}Para\s{0,}\)", ""),
        (r"\(\s{0,}ICP\s{0,}\)", ""),
        (r"\(\s{0,}ECP\s{0,}\)", ""),
        (r"\(\s{0,}AOES\s{0,}\)", ""),
        (r"\(\s{0,}AO\s{0,}\)", ""),
        (r"\(\s{0,}AR\s{0,}\)", ""),
        (r"\(\s{0,}AA\s{0,}\)", "")
    ]

    for pattern, replacement in replacements:
        cleaned = re.sub(pattern, replacement, cleaned, flags=re.IGNORECASE)

    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"\s+([,.;:?])", r"\1", cleaned)
    cleaned = cleaned.strip()

    return cleaned


def clean_question_record(question_record):
    if not isinstance(question_record, dict):
        return question_record

    cleaned_record = dict(question_record)

    if "question" in cleaned_record:
        cleaned_record["question"] = clean_generated_text(cleaned_record.get("question", ""))

    if "explanation" in cleaned_record:
        cleaned_record["explanation"] = clean_generated_text(cleaned_record.get("explanation", ""))

    if "correctAnswer" in cleaned_record:
        cleaned_record["correctAnswer"] = clean_generated_text(cleaned_record.get("correctAnswer", ""))

    if "answer" in cleaned_record:
        cleaned_record["answer"] = clean_generated_text(cleaned_record.get("answer", ""))

    if isinstance(cleaned_record.get("options"), list):
        cleaned_options = []

        for option in cleaned_record["options"]:
            cleaned_options.append(clean_generated_text(option))

        cleaned_record["options"] = cleaned_options

    return cleaned_record


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
