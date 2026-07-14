import asyncio
import hashlib
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urldefrag, urljoin, urlparse, urlunparse

import requests
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

BASE_URL = "https://clinical.saambulance.sa.gov.au"
START_URLS = [
    f"{BASE_URL}/tabs/home",
    f"{BASE_URL}/tabs/medicines",
    f"{BASE_URL}/tabs/calculators",
]

CLINICAL_LEVELS = [
    "Ambulance Assist",
    "Ambulance Responder",
    "Ambulance Officer",
    "Ambulance Officer Extended Scope",
    "Paramedic",
    "Intensive Care Paramedic",
    "Extended Care Paramedic",
]

DATA_DIR = Path("data")
QUESTIONS_FILE = DATA_DIR / "questions.json"
PAGE_RECORDS_FILE = DATA_DIR / "page_records.json"
CRAWL_FILE = DATA_DIR / "crawled_pages.json"

MAX_PAGES_PER_LEVEL = int(os.getenv("MAX_PAGES_PER_LEVEL", "10"))
MIN_TEXT_CHARS = int(os.getenv("MIN_TEXT_CHARS", "500"))
QUESTIONS_PER_PAGE = int(os.getenv("QUESTIONS_PER_PAGE", "2"))
GEMINI_BATCH_SIZE = int(os.getenv("GEMINI_BATCH_SIZE", "3"))
GEMINI_DELAY_SECONDS = float(os.getenv("GEMINI_DELAY_SECONDS", "25"))
GEMINI_MAX_RETRIES = int(os.getenv("GEMINI_MAX_RETRIES", "3"))
REQUEST_TIMEOUT_SECONDS = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "90"))
PAGE_TIMEOUT_MS = int(os.getenv("PAGE_TIMEOUT_MS", "60000"))
MAX_DISCOVERY_URLS_PER_LEVEL = int(os.getenv("MAX_DISCOVERY_URLS_PER_LEVEL", "250"))

GEMINI_MODELS = [
    model.strip()
    for model in os.getenv("GEMINI_MODELS", "gemini-2.5-flash,gemini-2.0-flash").split(",")
    if model.strip()
]


def log(message: str) -> None:
    print(message, flush=True)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except Exception as exc:
        log(f"Warning: could not read {path}: {exc}")
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")
    tmp_path.replace(path)


def normalise_space(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def clean_text(text: str) -> str:
    text = text or ""
    text = text.replace("\u00a0", " ")
    text = re.sub(r"\s+", " ", text)
    for noise in [
        "Clinical Practice Guidelines",
        "SA Ambulance Service",
        "Skip to content",
        "Open menu",
        "Close menu",
    ]:
        text = text.replace(noise, " ")
    return normalise_space(text)


def clean_generation_text(text: str) -> str:
    text = str(text or "")
    replacements = {
        "Ambulance Assist (AA)": "Ambulance Assist",
        "Ambulance Responder (AR)": "Ambulance Responder",
        "Ambulance Officer (AO)": "Ambulance Officer",
        "Ambulance Officer Extended Scope (AOES)": "Ambulance Officer Extended Scope",
        "Paramedic (P)": "Paramedic",
        "Paramedic(P)": "Paramedic",
        "Paramedic (Para)": "Paramedic",
        "Paramedic(Para)": "Paramedic",
        "Intensive Care Paramedic (ICP)": "Intensive Care Paramedic",
        "Intensive Care Paramedic(ICP)": "Intensive Care Paramedic",
        "Extended Care Paramedic (ECP)": "Extended Care Paramedic",
        "Extended Care Paramedic(ECP)": "Extended Care Paramedic",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = re.sub(r"\bParamedic\s*\(\s*Para\s*\)", "Paramedic", text, flags=re.IGNORECASE)
    text = re.sub(r"\b([A-Za-z ]+?)\s*\(\s*(AA|AR|AO|AOES|P|ICP|ECP)\s*\)", r"\1", text)
    return normalise_space(text)


def canonical_url(url: str) -> str:
    if not url:
        return ""
    url, _fragment = urldefrag(str(url))
    url = url.replace("&amp;", "&")
    parsed = urlparse(url)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc or urlparse(BASE_URL).netloc
    path = re.sub(r"/+$", "", parsed.path or "/")
    query_pairs = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() in {"title", "ref"}:
            continue
        query_pairs.append((key, value))
    query = urlencode(query_pairs, doseq=True)
    return urlunparse((scheme, netloc, path, "", query, ""))


def source_key(clinical_level: str, url: str) -> str:
    return f"{clinical_level}|{canonical_url(url)}"


def page_hash(text: str) -> str:
    return hashlib.sha256(clean_text(text).encode("utf-8")).hexdigest()


def infer_source_type(url: str, title: str) -> str:
    lower = f"{url} {title}".lower()
    if "/medicines" in lower:
        return "Medicine"
    if "/calculators" in lower or "calculator" in lower:
        return "Calculator"
    return "Clinical Practice Guideline"


def title_from_url(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    slug = path.split("/")[-1] if path else "Guideline"
    slug = re.sub(r"-[a-z]{1,5}$", "", slug, flags=re.IGNORECASE)
    return " ".join(word.capitalize() for word in slug.replace("-", " ").split()) or "Guideline"


def is_same_site_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return parsed.netloc in {"", "clinical.saambulance.sa.gov.au"}
    except Exception:
        return False


def is_candidate_detail_url(url: str) -> bool:
    url_lower = url.lower()
    return "/page/" in url_lower and any(
        part in url_lower for part in ["/tabs/home", "/tabs/medicines", "/tabs/calculators"]
    )


async def body_text(page) -> str:
    try:
        return await page.locator("body").inner_text(timeout=5000)
    except Exception:
        return ""


async def accept_disclaimer_if_present(page) -> None:
    text = await body_text(page)
    has_disclaimer = "Disclaimer" in text or "These clinical practice guidelines" in text

    candidates = []
    try:
        candidates.append(page.get_by_text("OK", exact=True))
    except Exception:
        pass
    try:
        candidates.append(page.locator("button", has_text=re.compile(r"^\s*OK\s*$", re.IGNORECASE)))
    except Exception:
        pass
    try:
        candidates.append(page.locator("ion-button", has_text=re.compile(r"^\s*OK\s*$", re.IGNORECASE)))
    except Exception:
        pass

    for candidate in candidates:
        try:
            if await candidate.count() > 0:
                await candidate.first().click(timeout=5000)
                await page.wait_for_timeout(2000)
                log("Accepted disclaimer.")
                return
        except Exception:
            continue

    if not has_disclaimer:
        return

    try:
        controls = page.locator("button, ion-button, [role=button]")
        count = await controls.count()
        for index in range(min(count, 50)):
            control = controls.nth(index)
            try:
                control_text = normalise_space(await control.inner_text(timeout=1000))
            except Exception:
                control_text = ""
            if control_text.upper() == "OK":
                await control.click(timeout=5000)
                await page.wait_for_timeout(2000)
                log("Accepted disclaimer.")
                return
    except Exception:
        pass


def looks_like_disclaimer_only(text: str) -> bool:
    cleaned = normalise_space(text)
    if not cleaned:
        return False
    return "Disclaimer" in cleaned and "These clinical practice guidelines" in cleaned and "Favourites" in cleaned


async def open_level_selector(page) -> None:
    for selector_text in ["Level", "Clinical Level"]:
        try:
            locator = page.get_by_text(selector_text, exact=True)
            if await locator.count() > 0:
                await locator.first().click(timeout=5000)
                await page.wait_for_timeout(1000)
                return
        except Exception:
            pass

    selectors = [
        "button",
        "[role=button]",
        "ion-button",
        "mat-select",
        "select",
        ".mat-mdc-select",
        ".mat-select",
    ]
    for selector in selectors:
        try:
            elements = page.locator(selector)
            count = await elements.count()
            for index in range(min(count, 50)):
                element = elements.nth(index)
                try:
