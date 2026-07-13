import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

BASE_URL = "https://clinical.saambulance.sa.gov.au"
URLS = [
    (f"{BASE_URL}/tabs/home", "page_1"),
    (f"{BASE_URL}/tabs/medicines", "page_2"),
    (f"{BASE_URL}/tabs/calculators", "page_3"),
]
OUT = Path("debug_saas")

async def safe_inner_text(locator, timeout=1000):
    try:
        return (await locator.inner_text(timeout=timeout)).strip()
    except Exception:
        return ""

async def safe_attribute(locator, name, timeout=1000):
    try:
        value = await locator.get_attribute(name, timeout=timeout)
        return value or ""
    except Exception:
        return ""

async def inspect_page(page, url, name):
    print(f"Opening {url}", flush=True)
    await page.goto(url, wait_until="domcontentloaded", timeout=90000)
    await page.wait_for_timeout(30000)

    title = await page.title()
    current_url = page.url
    body_text = ""
    body_html = ""
    body_text_error = ""
    body_html_error = ""

    try:
        body_text = await page.locator("body").inner_text(timeout=10000)
    except Exception as exc:
        body_text_error = repr(exc)

    try:
        body_html = await page.content()
    except Exception as exc:
        body_html_error = repr(exc)

    controls = []
    selector = "button, [role=button], a, ion-button, mat-select, select"
    try:
        count = await page.locator(selector).count()
        for index in range(min(count, 80)):
            locator = page.locator(selector).nth(index)
            text = await safe_inner_text(locator)
            href = await safe_attribute(locator, "href")
            controls.append(f"{index}: text={text!r} href={href!r}")
    except Exception as exc:
        controls.append(f"ERROR reading controls: {exc!r}")

    links = []
    try:
        hrefs = await page.locator("a[href]").evaluate_all("els => els.map(a => a.href)")
        for href in hrefs[:120]:
            links.append(str(href))
    except Exception as exc:
        links.append(f"ERROR reading links: {exc!r}")

    screenshot_path = OUT / f"{name}.png"
    html_path = OUT / f"{name}.html"
    text_path = OUT / f"{name}.txt"

    await page.screenshot(path=str(screenshot_path), full_page=True)
    html_path.write_text(body_html, encoding="utf-8")

    lines = []
    lines.append(f"url: {url}")
    lines.append(f"current_url: {current_url}")
    lines.append(f"title: {title}")
    lines.append(f"body_text_length: {len(body_text)}")
    lines.append(f"body_html_length: {len(body_html)}")
    lines.append(f"body_text_error: {body_text_error}")
    lines.append(f"body_html_error: {body_html_error}")
    lines.append("")
    lines.append("FIRST 4000 BODY TEXT CHARS:")
    lines.append(body_text[:4000])
    lines.append("")
    lines.append("VISIBLE CONTROLS:")
    lines.extend(controls)
    lines.append("")
    lines.append("LINKS:")
    lines.extend(links)

    text_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"{name}: title={title!r} body_text_length={len(body_text)} body_html_length={len(body_html)}", flush=True)
    print(f"Saved {screenshot_path}, {html_path}, {text_path}", flush=True)

async def main():
    OUT.mkdir(exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1440, "height": 1200},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        for url, name in URLS:
            await inspect_page(page, url, name)
        await context.close()
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
