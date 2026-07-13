import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

BASE_URL = "https://clinical.saambulance.sa.gov.au"
URLS = [
    f"{BASE_URL}/tabs/home",
    f"{BASE_URL}/tabs/medicines",
    f"{BASE_URL}/tabs/calculators",
]
OUT = Path("debug_saas")

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

    buttons = []
    try:
        count = await page.locator("button, [role=button], a, ion-button, mat-select, select").count()
        for index in range(min(count, 80)):
            locator = page.locator("button, [role=button], a, ion-button, mat-select, select").nth(index)
            try:
                text = (await locator.inner_text(timeout=1000)).strip()
            except Exception:
                text = ""
            try:
                href = await locator.get_attribute("href", timeout=1000)
            except Exception:
                href = ""
            buttons.append(f"{index}: text={text!r} href={href!r}")
    except Exception as exc:
        buttons.append(f"ERROR reading controls: {exc!r}")

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
    text_path.write_text(
        "\n".join([
            f"url: {url}",
            f"current_url: {current_url}",
            f"title: {title}",
            f"body_text_length: {len(body_text)}",
            f"body_html_length: {len(body_html)}",
            f"body_text_error: {body_text_error}",
            f"body_html_error: {body_html_error}",
            "",
            "FIRST 4000 BODY TEXT CHARS:",
            body_text[:4000],
            "",
            "VISIBLE CONTROLS:",
            *buttons,
            "",
         *  "LINKS:",
            *links,
  *     ]),
        encoding="utf-8",*    )

    print(f"{name}: title={*itle!r} body_text_length={len(body*text)} body_html_length={len(body_*tml)}", flush=True)
    print(f"Sa*ed {screenshot_path}, {html_path},*{text_path}", flush=True)

async d*f main():
    OUT.mkdir(exist_ok=T*ue)
    async with async_playwrigh*() as playwright:
        browser * await playwright.chromium.launch(*eadless=True)
        context = aw*it browser.new_context(
          * viewport={"width": 1440, "height"* 1200},
            user_agent=(
 *              "Mozilla/5.0 (Window* NT 10.0; Win64; x64) "
          *     "AppleWebKit/537.36 (KHTML, l*ke Gecko) "
                "Chrom*/126.0.0.0 Safari/537.36"
        *   ),
        )
        page = awa*t context.new_page()
        for i*dex, url in enumerate(URLS, start=*):
            await inspect_page(*age, url, f"page_{index}")
       *await context.close()
        awai* browser.close()

if __name__ == "*_main__":
    asyncio.run(main())
