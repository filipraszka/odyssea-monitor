import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from playwright.sync_api import Page, sync_playwright


FILM_URL = "https://www.cinemacity.cz/films/odyssea/7268s2r"
CINEMA_URL = "https://www.cinemacity.cz/cinemas/flora/1052"

STATE_FILE = Path("screenings_state.json")
DEBUG_FILE = Path("monitor_debug.txt")

NTFY_TOPIC = os.environ["NTFY_TOPIC"]

# Zajímají nás pouze projekce 13. 8. 2026 a později.
MINIMUM_DATE = date(2026, 8, 12)

MONTHS = {
    "ledna": 1,
    "února": 2,
    "unora": 2,
    "března": 3,
    "brezna": 3,
    "dubna": 4,
    "května": 5,
    "kvetna": 5,
    "června": 6,
    "cervna": 6,
    "července": 7,
    "cervence": 7,
    "srpna": 8,
    "září": 9,
    "zari": 9,
    "října": 10,
    "rijna": 10,
    "listopadu": 11,
    "prosince": 12,
}


def send_notification(
    title: str,
    message: str,
    click_url: str = FILM_URL,
) -> None:
    response = requests.post(
        "https://ntfy.sh",
        json={
            "topic": NTFY_TOPIC,
            "title": title,
            "message": message,
            "priority": 5,
            "tags": ["movie_camera", "ticket"],
            "click": click_url,
        },
        timeout=30,
    )
    response.raise_for_status()


def parse_date(text: str) -> date | None:
    numeric_patterns = [
        r"\b(\d{1,2})\.\s*(\d{1,2})\.\s*(2026)\b",
        r"\b(2026)-(\d{1,2})-(\d{1,2})\b",
    ]

    match = re.search(numeric_patterns[0], text)
    if match:
        day, month, year = map(int, match.groups())

        try:
            return date(year, month, day)
        except ValueError:
            return None

    match = re.search(numeric_patterns[1], text)
    if match:
        year, month, day = map(int, match.groups())

        try:
            return date(year, month, day)
        except ValueError:
            return None

    written = re.search(
        r"\b(\d{1,2})\.?\s+"
        r"(ledna|února|unora|března|brezna|dubna|května|kvetna|"
        r"června|cervna|července|cervence|srpna|září|zari|"
        r"října|rijna|listopadu|prosince)"
        r"(?:\s+(2026))?\b",
        text.lower(),
    )

    if written:
        day = int(written.group(1))
        month = MONTHS[written.group(2)]
        year = int(written.group(3) or 2026)

        try:
            return date(year, month, day)
        except ValueError:
            return None

    return None


def accept_cookies(page: Page) -> None:
    labels = [
        "Přijmout vše",
        "Povolit vše",
        "Souhlasím",
        "Accept all",
        "Allow all",
    ]

    for label in labels:
        try:
            page.get_by_text(label, exact=False).first.click(timeout=1500)
            page.wait_for_timeout(1000)
            return
        except Exception:
            continue


def normalize_url(url: str | None, base_url: str) -> str:
    if not url:
        return FILM_URL

    return urljoin(base_url, url)


def get_date_from_element(element) -> date | None:
    try:
        text = element.inner_text(timeout=1000)
    except Exception:
        text = ""

    screening_date = parse_date(text)

    if screening_date:
        return screening_date

    attributes = [
        "aria-label",
        "title",
        "data-date",
        "data-show-date",
        "datetime",
        "href",
    ]

    for attribute in attributes:
        try:
            value = element.get_attribute(attribute)
        except Exception:
            value = None

        if value:
            screening_date = parse_date(value)

            if screening_date:
                return screening_date

    return None


def collect_date_controls(page: Page) -> list:
    controls = page.locator(
        "button, a, [role='button'], input[type='radio'], "
        "[data-date], [data-show-date]"
    )

    found = []
    seen = set()

    for index in range(controls.count()):
        element = controls.nth(index)
        screening_date = get_date_from_element(element)

        if not screening_date:
            continue

        key = screening_date.isoformat()

        if key in seen:
            continue

        seen.add(key)
        found.append((screening_date, element))

    return sorted(found, key=lambda item: item[0])


def find_odyssea_cards(page: Page):
    selectors = [
        "text=Odyssea",
        "[data-film-name*='Odyssea' i]",
        "[aria-label*='Odyssea' i]",
        "[title*='Odyssea' i]",
    ]

    elements = []

    for selector in selectors:
        try:
            locator = page.locator(selector)

            for index in range(min(locator.count(), 20)):
                elements.append(locator.nth(index))
        except Exception:
            continue

    return elements


def get_card_context(element) -> tuple[str, list[dict]]:
    try:
        result = element.evaluate(
            """
            element => {
                let node = element;
                let bestNode = element;
                let bestText = element.innerText || element.textContent || "";

                for (let i = 0; i < 10 && node; i++) {
                    const text = node.innerText || node.textContent || "";

                    const hasOdyssea = /odyssea/i.test(text);
                    const hasTime = /(?:[01]?\\d|2[0-3]):[0-5]\\d/.test(text);
                    const hasFormat = /imax|70\\s*mm/i.test(text);

                    if (
                        hasOdyssea &&
                        hasTime &&
                        text.length < 6000
                    ) {
                        bestNode = node;
                        bestText = text;

                        if (hasFormat) {
                            break;
                        }
                    }

                    node = node.parentElement;
                }

                const links = Array.from(
                    bestNode.querySelectorAll("a[href]")
                ).map(link => ({
                    text: link.innerText || link.textContent || "",
                    href: link.href
                }));

                return {
                    text: bestText,
                    links: links
                };
            }
            """
        )

        return result.get("text", ""), result.get("links", [])

    except Exception:
        return "", []


def extract_screenings_from_page(
    page: Page,
    selected_date: date | None,
    source_url: str,
) -> list[dict]:
    results = {}

    for odyssea_element in find_odyssea_cards(page):
        card_text, links = get_card_context(odyssea_element)

        if not card_text:
            continue

        if "odyssea" not in card_text.lower():
            continue

        # Musí jít o formát IMAX 70 mm.
        if not re.search(
            r"IMAX(?:[\s-]*70(?:\s*mm)?)?|70\s*mm",
            card_text,
            re.IGNORECASE,
        ):
            continue

        screening_date = parse_date(card_text) or selected_date

        if not screening_date:
            continue

        times = sorted(
            set(
                re.findall(
                    r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b",
                    card_text,
                )
            )
        )

        if not times:
            continue

        purchase_links = []

        for link in links:
            href = normalize_url(link.get("href"), source_url)
            link_text = link.get("text", "")

            if re.search(
                r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b",
                link_text,
            ):
                purchase_links.append((link_text, href))

        for screening_time in times:
            purchase_url = FILM_URL

            for link_text, href in purchase_links:
                if screening_time in link_text:
                    purchase_url = href
                    break

            identifier = (
                f"{screening_date.isoformat()}|"
                f"{screening_time}|{purchase_url}"
            )

            results[identifier] = {
                "id": identifier,
                "date": screening_date.isoformat(),
                "time": screening_time,
                "url": purchase_url,
            }

    return list(results.values())


def scan_page(page: Page, url: str) -> tuple[list[dict], str]:
    page.goto(
        url,
        wait_until="domcontentloaded",
        timeout=90000,
    )

    page.wait_for_timeout(7000)
    accept_cookies(page)
    page.wait_for_timeout(3000)

    all_results = {}
    debug_sections = []

    try:
        body_text = page.locator("body").inner_text(timeout=10000)
    except Exception:
        body_text = ""

    debug_sections.append(
        f"\n\n===== {url} – výchozí stránka =====\n{body_text}"
    )

    initial_results = extract_screenings_from_page(
        page=page,
        selected_date=None,
        source_url=url,
    )

    for result in initial_results:
        all_results[result["id"]] = result

    date_controls = collect_date_controls(page)

    for screening_date, control in date_controls:
        try:
            control.scroll_into_view_if_needed(timeout=3000)
            control.click(timeout=5000)
            page.wait_for_timeout(2500)
        except Exception:
            continue

        try:
            body_text = page.locator("body").inner_text(timeout=10000)
        except Exception:
            body_text = ""

        debug_sections.append(
            f"\n\n===== {url} – {screening_date.isoformat()} =====\n"
            f"{body_text}"
        )

        date_results = extract_screenings_from_page(
            page=page,
            selected_date=screening_date,
            source_url=url,
        )

        for result in date_results:
            all_results[result["id"]] = result

    return list(all_results.values()), "".join(debug_sections)


def find_screenings() -> tuple[list[dict], list[dict]]:
    all_screenings = {}
    debug_output = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)

        page = browser.new_page(
            locale="cs-CZ",
            viewport={"width": 1440, "height": 1800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 "
                "Chrome/150 Safari/537.36"
            ),
        )

        for url in [FILM_URL, CINEMA_URL]:
            try:
                results, debug_text = scan_page(page, url)

                for result in results:
                    all_screenings[result["id"]] = result

                debug_output.append(debug_text)

            except Exception as error:
                debug_output.append(
                    f"\n\nCHYBA PŘI ČTENÍ {url}:\n{error!r}"
                )

        browser.close()

    DEBUG_FILE.write_text(
        "\n".join(debug_output),
        encoding="utf-8",
    )

    all_found = sorted(
        all_screenings.values(),
        key=lambda item: (item["date"], item["time"]),
    )

    relevant = [
        item
        for item in all_found
        if datetime.strptime(
            item["date"],
            "%Y-%m-%d",
        ).date() > MINIMUM_DATE
    ]

    return all_found, relevant


def load_previous_ids() -> set[str]:
    if not STATE_FILE.exists():
        return set()

    try:
        state = json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )
        return set(state.get("screening_ids", []))
    except Exception:
        return set()


def save_state(
    all_screenings: list[dict],
    relevant_screenings: list[dict],
) -> None:
    state = {
        "updated_at": datetime.now().isoformat(),
        "minimum_date_exclusive": MINIMUM_DATE.isoformat(),
        "all_found_count": len(all_screenings),
        "relevant_count": len(relevant_screenings),
        "screening_ids": [
            item["id"]
            for item in relevant_screenings
        ],
        "all_found_screenings": all_screenings,
        "relevant_screenings": relevant_screenings,
    }

    STATE_FILE.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def main() -> None:
    all_screenings, relevant_screenings = find_screenings()

    state_exists = STATE_FILE.exists()
    previous_ids = load_previous_ids()

    if not state_exists:
        save_state(all_screenings, relevant_screenings)

        send_notification(
            "Odyssea monitor je spuštěný",
            (
                "✅ Monitor funguje.\n"
                f"Celkem rozpoznaných IMAX 70mm projekcí: "
                f"{len(all_screenings)}.\n"
                f"Projekcí po 12. 8. 2026: "
                f"{len(relevant_screenings)}.\n"
                "Upozornění přijde pouze na nově přidané projekce "
                "od 13. 8. 2026."
            ),
        )
        return

    new_screenings = [
        item
        for item in relevant_screenings
        if item["id"] not in previous_ids
    ]

    for screening in new_screenings:
        readable_date = datetime.strptime(
            screening["date"],
            "%Y-%m-%d",
        ).strftime("%d. %m. %Y")

        send_notification(
            "Nová projekce Odyssea IMAX 70 mm",
            (
                "🎬 Odyssea – IMAX 70 mm\n"
                f"📅 {readable_date}\n"
                f"🕒 {screening['time']}\n"
                "🎟️ Klepnutím otevřeš nákup vstupenek."
            ),
            screening["url"],
        )

    save_state(all_screenings, relevant_screenings)


if __name__ == "__main__":
    main()
