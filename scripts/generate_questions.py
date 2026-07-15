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
                    text = normalise_space(await element.inner_text(timeout=1000))
                except Exception:
                    text = ""
                if text.lower() in {"level", "clinical level"} or "level" in text.lower():
                    await element.click(timeout=5000)
                    await page.wait_for_timeout(1000)
                    return
        except Exception:
            continue


async def click_level_option_by_dom(page, clinical_level: str) -> bool:
    try:
        clicked = await page.evaluate(
            r"""
            (level) => {
                const norm = (s) => (s || "").replace(/\s+/g, " ").trim();
                const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return (
                        style &&
                        style.display !== "none" &&
                        style.visibility !== "hidden" &&
                        rect.width > 0 &&
                        rect.height > 0
                    );
                };
                const selectors = [
                    "ion-item",
                    "ion-radio",
                    "button",
                    "[role='button']",
                    "[role='radio']",
                    "[role='option']",
                    "label",
                    ".alert-radio-label",
                    ".select-interface-option",
                    "span",
                    "div"
                ];
                const nodes = Array.from(document.querySelectorAll(selectors.join(",")));
                for (const el of nodes) {
                    if (!visible(el)) {
                        continue;
                    }
                    const text = norm(el.innerText || el.textContent);
                    if (text === level) {
                        const target =
                            el.closest("button, ion-item, ion-radio, [role='button'], [role='radio'], [role='option'], label") ||
                            el;
                        target.click();
                        return true;
                    }
                }
                return false;
            }
            """,
            clinical_level,
        )
        if clicked:
            await page.wait_for_timeout(2500)
            log(f"Selected level by DOM click: {clinical_level}")
            return True
    except Exception as exc:
        log(f"DOM level click failed for {clinical_level}: {exc}")
    return False


async def select_clinical_level(page, clinical_level: str) -> bool:
    log(f"Trying to select level: {clinical_level}")

    await accept_disclaimer_if_present(page)
    await page.wait_for_timeout(1000)

    try:
        exact = page.get_by_text(clinical_level, exact=True)
        if await exact.count() > 0:
            try:
                if await exact.first().is_visible(timeout=1000):
                    await exact.first().click(timeout=5000, force=True)
                    await page.wait_for_timeout(2500)
                    log(f"Selected level by visible exact text: {clinical_level}")
                    return True
            except Exception:
                pass
    except Exception:
        pass

    await open_level_selector(page)
    await page.wait_for_timeout(1000)

    if await click_level_option_by_dom(page, clinical_level):
        return True

    try:
        exact = page.get_by_text(clinical_level, exact=True)
        count = await exact.count()
        for index in range(min(count, 10)):
            option = exact.nth(index)
            try:
                if await option.is_visible(timeout=1000):
                    await option.click(timeout=5000, force=True)
                    await page.wait_for_timeout(2500)
                    log(f"Selected level by visible exact text after selector open: {clinical_level}")
                    return True
            except Exception:
                continue
    except Exception:
        pass

    try:
        escaped_level = re.escape(clinical_level)
        option = page.locator(
            "button, ion-button, [role=button], mat-option, ion-item, option, div, span",
            has_text=re.compile(rf"^\s*{escaped_level}\s*$", re.IGNORECASE),
        )
        count = await option.count()
        for index in range(min(count, 10)):
            candidate = option.nth(index)
            try:
                if await candidate.is_visible(timeout=1000):
                    await candidate.click(timeout=5000, force=True)
                    await page.wait_for_timeout(2500)
                    log(f"Selected level by locator fallback: {clinical_level}")
                    return True
            except Exception:
                continue
    except Exception:
        pass

    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass

    text = await body_text(page)
    log(f"Warning: could not confirm clinical level selection for: {clinical_level}")
    log(f"Visible body text sample after selection attempt: {normalise_space(text)[:500]}")
    return False


async def extract_page_text_and_links(page, url: str) -> Tuple[str, str, List[str]]:
    await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    await page.wait_for_timeout(3000)
    await accept_disclaimer_if_present(page)
    await page.wait_for_timeout(2000)

    title = ""
    for selector in ["h1", "h2", "ion-title", ".title", "title"]:
        try:
            locator = page.locator(selector)
            if await locator.count() > 0:
                title = normalise_space(await locator.first().inner_text(timeout=2000))
                if title:
                    break
        except Exception:
            continue

    if not title:
        try:
            title = normalise_space(await page.title())
        except Exception:
            title = title_from_url(url)

    best_text = ""
    for selector in ["main", "ion-content", "article", ".content", "body"]:
        try:
            locator = page.locator(selector)
            if await locator.count() > 0:
                candidate = clean_text(await locator.first().inner_text(timeout=5000))
                if len(candidate) > len(best_text):
                    best_text = candidate
        except Exception:
            continue

    if looks_like_disclaimer_only(best_text):
        log(f"Page still appears to be disclaimer-only after OK: {url}")
        best_text = ""

    links: List[str] = []
    try:
        hrefs = await page.locator("a[href]").evaluate_all("els => els.map(a => a.href)")
        for href in hrefs:
            if href and is_same_site_url(href):
                links.append(canonical_url(urljoin(BASE_URL, href)))
    except Exception:
        pass

    try:
        content = await page.content()
        body_links = re.findall(r"https://clinical\.saambulance\.sa\.gov\.au/[^\s\"'<>]+", content)
        for href in body_links:
            if is_same_site_url(href):
                links.append(canonical_url(href))
    except Exception:
        pass

    seen = set()
    unique_links = []
    for link in links:
        if link not in seen:
            seen.add(link)
            unique_links.append(link)

    return title or title_from_url(url), best_text, unique_links


async def crawl_level(browser, clinical_level: str) -> List[Dict[str, Any]]:
    context = await browser.new_context(
        viewport={"width": 1440, "height": 1200},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
    )
    page = await context.new_page()
    page.set_default_timeout(PAGE_TIMEOUT_MS)

    usable: List[Dict[str, Any]] = []
    queued: List[str] = [canonical_url(url) for url in START_URLS]
    queued_set = set(queued)
    visited = set()

    try:
        await page.goto(START_URLS[0], wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        await page.wait_for_timeout(3000)
        await accept_disclaimer_if_present(page)
        await page.wait_for_timeout(1500)
        await select_clinical_level(page, clinical_level)

        while queued and len(visited) < MAX_DISCOVERY_URLS_PER_LEVEL and len(usable) < MAX_PAGES_PER_LEVEL:
            url = queued.pop(0)
            queued_set.discard(url)

            if url in visited:
                continue
            visited.add(url)

            try:
                log(f"[{clinical_level}] Opening: {url}")
                title, text, links = await extract_page_text_and_links(page, url)
                source_type = infer_source_type(url, title)
                log(f"[{clinical_level}] Extracted {len(text)} characters from {url} [{source_type}]")

                if is_candidate_detail_url(url) and len(text) >= MIN_TEXT_CHARS:
                    key = source_key(clinical_level, url)
                    if not any(item["key"] == key for item in usable):
                        usable.append(
                            {
                                "key": key,
                                "clinicalLevel": clinical_level,
                                "sourceUrl": canonical_url(url),
                                "sourceTitle": title or title_from_url(url),
                                "sourceType": source_type,
                                "text": text,
                                "hash": page_hash(text),
                            }
                        )
                else:
                    log(f"[{clinical_level}] Discovery page only: {url}")

                for link in links:
                    if not link.startswith(BASE_URL):
                        continue
                    if link in visited or link in queued_set:
                        continue
                    if any(part in link for part in ["/tabs/home", "/tabs/medicines", "/tabs/calculators"]):
                        queued.append(link)
                        queued_set.add(link)

            except PlaywrightTimeoutError as exc:
                log(f"[{clinical_level}] Timeout opening {url}: {exc}")
            except Exception as exc:
                log(f"[{clinical_level}] Error opening {url}: {exc}")

    finally:
        await context.close()

    log(f"{clinical_level}: crawled {len(usable)} usable detail pages.")
    return usable


def extract_json_array(text: str) -> Optional[List[Any]]:
    if not text:
        return None

    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        data = json.loads(cleaned)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ["questions", "items", "data"]:
                if isinstance(data.get(key), list):
                    return data[key]
    except Exception:
        pass

    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start != -1 and end != -1 and end > start:
        fragment = cleaned[start : end + 1]
        try:
            data = json.loads(fragment)
            if isinstance(data, list):
                return data
        except Exception as exc:
            log(f"JSON parse error: {exc}")
            log(fragment[:1000])

    return None


def gemini_payload(prompt: str) -> Dict[str, Any]:
    return {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "topP": 0.8,
            "topK": 40,
            "maxOutputTokens": 8192,
            "responseMimeType": "application/json",
        },
    }


def build_batch_prompt(pages: List[Dict[str, Any]]) -> str:
    page_blocks = []
    for index, page in enumerate(pages, start=1):
        clipped_text = page["text"][:12000]
        page_blocks.append(
            "\n".join(
                [
                    f"PAGE {index}",
                    f"clinicalLevel: {page['clinicalLevel']}",
                    f"sourceUrl: {page['sourceUrl']}",
                    f"sourceTitle: {page['sourceTitle']}",
                    f"sourceType: {page['sourceType']}",
                    "content:",
                    clipped_text,
                ]
            )
        )

    return f"""
You are generating revision quiz questions for South Australian ambulance clinical guideline study.

Return ONLY valid JSON. Do not use markdown. Do not include commentary.

Create exactly {QUESTIONS_PER_PAGE} questions for each PAGE below.

Each JSON object must have these exact fields:
- question: string
- options: array of exactly 4 strings
- correctAnswer: string, exactly matching one option
- explanation: string
- clinicalLevel: string, exactly copied from the PAGE clinicalLevel
- sourceUrl: string, exactly copied from the PAGE sourceUrl
- sourceTitle: string, exactly copied from the PAGE sourceTitle
- sourceType: string, exactly copied from the PAGE sourceType

Rules:
- Questions must be answerable from the supplied page content only.
- Avoid questions about page wording, document metadata, app navigation, or source labels.
- Avoid trivial questions where all options are obviously wrong except one.
- Do not include clinical-level abbreviations such as (AA), (AR), (AO), (P), (ICP), (ECP), or (Para).
- Keep wording concise and clinically relevant.
- Prefer multiple choice clinical interpretation questions where possible.
- If the page content is a medicine monograph, focus on indications, contraindications, dose principles, route, precautions, or adverse effects.
- If the page content is too limited, still create the best possible questions from the content.

Return a single JSON array containing all question objects.

PAGES:
{chr(10).join(page_blocks)}
""".strip()


def call_gemini(prompt: str, clinical_label: str) -> Optional[List[Dict[str, Any]]]:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        log("Warning: GEMINI_API_KEY is not set. Skipping generation.")
        return None

    for attempt in range(1, GEMINI_MAX_RETRIES + 1):
        for model in GEMINI_MODELS:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
            try:
                log(f"[{clinical_label}] Trying Gemini model {model} attempt {attempt}")
                response = requests.post(url, json=gemini_payload(prompt), timeout=REQUEST_TIMEOUT_SECONDS)

                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        wait_seconds = int(retry_after)
                    else:
                        wait_seconds = min(300, 30 * attempt + random.randint(10, 30))
                    log(f"[{clinical_label}] Gemini rate limit on {model}. Waiting {wait_seconds} seconds.")
                    time.sleep(wait_seconds)
                    continue

                if response.status_code == 404:
                    log(f"[{clinical_label}] Model {model} not available. Skipping that model.")
                    continue

                response.raise_for_status()
                data = response.json()
                candidates = data.get("candidates", [])
                if not candidates:
                    log(f"[{clinical_label}] Gemini returned no candidates for {model}.")
                    continue

                parts = candidates[0].get("content", {}).get("parts", [])
                text = "\n".join(part.get("text", "") for part in parts if isinstance(part, dict))
                parsed = extract_json_array(text)
                if parsed is None:
                    log(f"[{clinical_label}] Could not parse JSON from {model}.")
                    continue

                log(f"[{clinical_label}] Model {model} produced {len(parsed)} raw questions.")
                return [item for item in parsed if isinstance(item, dict)]

            except requests.RequestException as exc:
                log(f"[{clinical_label}] Model {model} failed: {exc}")
            except Exception as exc:
                log(f"[{clinical_label}] Unexpected Gemini error for {model}: {exc}")

        if attempt < GEMINI_MAX_RETRIES:
            wait_seconds = min(300, 45 * attempt + random.randint(10, 30))
            log(f"[{clinical_label}] Waiting {wait_seconds} seconds before Gemini retry.")
            time.sleep(wait_seconds)

    log(f"[{clinical_label}] All Gemini attempts failed for this batch. Continuing without failing workflow.")
    return None


def validate_question(raw: Dict[str, Any], page_lookup: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    source_url = canonical_url(str(raw.get("sourceUrl", "")))
    clinical_level = clean_generation_text(str(raw.get("clinicalLevel", "")))
    key = source_key(clinical_level, source_url)

    page = page_lookup.get(key)
    if page is None:
        for candidate in page_lookup.values():
            if canonical_url(candidate["sourceUrl"]) == source_url:
                page = candidate
                break

    if page is None:
        return None

    question = clean_generation_text(str(raw.get("question", "")))
    options_raw = raw.get("options", [])
    if not isinstance(options_raw, list):
        return None

    options = [clean_generation_text(str(option)) for option in options_raw if str(option).strip()]
    options = options[:4]
    correct = clean_generation_text(str(raw.get("correctAnswer", "")))
    explanation = clean_generation_text(str(raw.get("explanation", "")))

    if len(options) != 4 or not question or not correct or not explanation:
        return None

    if correct not in options:
        lower_options = {option.lower(): option for option in options}
        correct = lower_options.get(correct.lower(), correct)

    if correct not in options:
        return None

    return {
        "question": question,
        "options": options,
        "correctAnswer": correct,
        "explanation": explanation,
        "clinicalLevel": page["clinicalLevel"],
        "sourceUrl": page["sourceUrl"],
        "sourceTitle": page["sourceTitle"],
        "sourceType": page["sourceType"],
    }


def question_identity(question: Dict[str, Any]) -> str:
    return "|".join(
        [
            clean_generation_text(str(question.get("clinicalLevel", ""))).lower(),
            canonical_url(str(question.get("sourceUrl", ""))).lower(),
            clean_generation_text(str(question.get("question", ""))).lower(),
        ]
    )


def group_existing_questions(questions: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for question in questions:
        if not isinstance(question, dict):
            continue

        clinical_level = clean_generation_text(str(question.get("clinicalLevel", "")))
        url = canonical_url(str(question.get("sourceUrl", "")))
        if not clinical_level or not url:
            continue

        question["clinicalLevel"] = clinical_level
        question["sourceUrl"] = url

        for field in ["question", "correctAnswer", "explanation", "sourceTitle", "sourceType"]:
            if field in question:
                question[field] = clean_generation_text(str(question[field]))

        if isinstance(question.get("options"), list):
            question["options"] = [clean_generation_text(str(option)) for option in question["options"]]

        key = source_key(clinical_level, url)
        grouped.setdefault(key, []).append(question)

    return grouped


def get_old_hash(page_records, key):
    old_record = page_records.get(key)

    if isinstance(old_record, dict):
        value = old_record.get("hash")
        return str(value) if value is not None else None

    if isinstance(old_record, str):
        return old_record

    return None


async def main() -> None:
    started = time.time()
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    existing_questions_raw = load_json(QUESTIONS_FILE, [])
    if not isinstance(existing_questions_raw, list):
        log("Warning: existing questions.json is not a list. Starting with an empty list.")
        existing_questions_raw = []

    existing_by_page = group_existing_questions(existing_questions_raw)

    page_records = load_json(PAGE_RECORDS_FILE, {})
    if not isinstance(page_records, dict):
        page_records = {}

    log("Starting incremental all-clinical-level crawl.")
    all_pages: List[Dict[str, Any]] = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            for clinical_level in CLINICAL_LEVELS:
                log(f"Starting crawl for clinical level: {clinical_level}")
                pages = await crawl_level(browser, clinical_level)
                all_pages.extend(pages)
        finally:
            await browser.close()

    page_lookup = {page["key"]: page for page in all_pages}

    save_json(
        CRAWL_FILE,
        [{key: value for key, value in page.items() if key != "text"} for page in all_pages],
    )

    changed_pages = []
    unchanged_keys = set()

    for page in all_pages:
        old_hash = get_old_hash(page_records, page["key"])
        has_existing_questions = bool(existing_by_page.get(page["key"]))

        if old_hash != page["hash"] or not has_existing_questions:
            changed_pages.append(page)
        else:
            unchanged_keys.add(page["key"])

    log(f"Crawled {len(all_pages)} usable pages total.")
    log(f"Changed or missing-question pages requiring generation: {len(changed_pages)}")
    log(f"Unchanged pages retaining existing questions: {len(unchanged_keys)}")

    final_questions = []
    for key in sorted(unchanged_keys):
        final_questions.extend(existing_by_page.get(key, []))

    generated_by_page: Dict[str, List[Dict[str, Any]]] = {}

    for start in range(0, len(changed_pages), GEMINI_BATCH_SIZE):
        batch = changed_pages[start : start + GEMINI_BATCH_SIZE]
        clinical_label = ", ".join(sorted({page["clinicalLevel"] for page in batch}))
        prompt = build_batch_prompt(batch)
        raw_questions = call_gemini(prompt, clinical_label)

        if raw_questions is None:
            for page in batch:
                fallback = existing_by_page.get(page["key"], [])
                if fallback:
                    log(f"[{page['clinicalLevel']}] Keeping existing questions for {page['sourceUrl']} after generation failure.")
                    generated_by_page.setdefault(page["key"], []).extend(fallback)
                else:
                    log(f"[{page['clinicalLevel']}] No questions generated for {page['sourceUrl']} due to Gemini failure.")
            continue

        valid_count = 0
        for raw in raw_questions:
            question = validate_question(raw, page_lookup)
            if question is None:
                continue
            key = source_key(question["clinicalLevel"], question["sourceUrl"])
            generated_by_page.setdefault(key, []).append(question)
            valid_count += 1

        log(f"{clinical_label}: accepted {valid_count} valid questions from batch.")

        if GEMINI_DELAY_SECONDS > 0 and start + GEMINI_BATCH_SIZE < len(changed_pages):
            log(f"Waiting {GEMINI_DELAY_SECONDS} seconds before next Gemini batch.")
            time.sleep(GEMINI_DELAY_SECONDS)

    for page in changed_pages:
        new_questions = generated_by_page.get(page["key"], [])
        if new_questions:
            final_questions.extend(new_questions)
        else:
            final_questions.extend(existing_by_page.get(page["key"], []))

    deduped = []
    seen_questions = set()
    for question in final_questions:
        identity = question_identity(question)
        if identity in seen_questions:
            continue
        seen_questions.add(identity)
        deduped.append(question)

    level_order = {level: index for index, level in enumerate(CLINICAL_LEVELS)}
    deduped.sort(
        key=lambda question: (
            level_order.get(question.get("clinicalLevel", ""), 999),
            str(question.get("sourceType", "")),
            str(question.get("sourceTitle", "")),
            str(question.get("question", "")),
        )
    )

    new_page_records = {}
    for page in all_pages:
        new_page_records[page["key"]] = {
            "hash": page["hash"],
            "clinicalLevel": page["clinicalLevel"],
            "sourceUrl": page["sourceUrl"],
            "sourceTitle": page["sourceTitle"],
            "sourceType": page["sourceType"],
            "lastSeen": int(time.time()),
        }

    minimum_safe_count = 1
    if existing_questions_raw:
        minimum_safe_count = max(1, int(len(existing_questions_raw) * 0.25))

    if existing_questions_raw and len(deduped) < minimum_safe_count:
        log(
            "Safety stop: generated question list is unexpectedly small. "
            f"Existing questions: {len(existing_questions_raw)}. "
            f"New questions: {len(deduped)}. "
            "Keeping existing questions.json unchanged."
        )
        log("Not writing questions.json or page_records.json.")
        return

    save_json(QUESTIONS_FILE, deduped)
    save_json(PAGE_RECORDS_FILE, new_page_records)

    by_level = {level: 0 for level in CLINICAL_LEVELS}
    for question in deduped:
        level = question.get("clinicalLevel", "")
        by_level[level] = by_level.get(level, 0) + 1

    log("Question counts by clinical level:")
    for level in CLINICAL_LEVELS:
        log(f"- {level}: {by_level.get(level, 0)}")

    elapsed = int(time.time() - started)
    log(f"Finished successfully in {elapsed} seconds. Wrote {len(deduped)} questions.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("Cancelled by user.")
        sys.exit(130)
