import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from playwright.sync_api import sync_playwright


FILM_URL = "https://www.cinemacity.cz/films/odyssea/7268s2r"
STATE_FILE = Path("screenings_state.json")
NTFY_TOPIC = os.environ["NTFY_TOPIC"]

# Upozorňovat jen na projekce po 2. srpnu 2026.
MINIMUM_DATE = date(2026, 8, 12)

CZECH_MONTHS = {
    "ledna": 1,
    "února": 2,
    "brezna": 3,
    "března": 3,
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


def send_notification(title: str, message: str, click_url: str = FILM_URL) -> None:
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
    # Formát například 9. 8. 2026
    numeric = re.search(
        r"\b(\d{1,2})\.\s*(\d{1,2})\.\s*(2026)\b",
        text,
    )
    if numeric:
        day, month, year = map(int, numeric.groups())
        return date(year, month, day)

    # Formát například 9. srpna 2026
    written = re.search(
        r"\b(\d{1,2})\.?\s+([a-záčďéěíňóřšťúůýž]+)(?:\s+(2026))?\b",
        text.lower(),
    )
    if written:
        day = int(written.group(1))
        month_name = written.group(2)
        year = int(written.group(3) or 2026)

        month = CZECH_MONTHS.get(month_name)
        if month:
            return date(year, month, day)

    return None


def find_screenings() -> list[dict]:
    screenings: dict[str, dict] = {}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(
            locale="cs-CZ",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 Chrome/150 Safari/537.36"
            ),
        )

        page.goto(FILM_URL, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(8000)

        # Pokus o zavření cookie lišty.
        for label in [
            "Přijmout vše",
            "Souhlasím",
            "Accept all",
            "Allow all",
        ]:
            try:
                page.get_by_text(label, exact=False).first.click(timeout=1500)
                break
            except Exception:
                pass

        page.wait_for_timeout(3000)

        elements = page.locator("a, button")
        count = elements.count()

        for index in range(count):
            element = elements.nth(index)

            try:
                element_text = element.inner_text(timeout=1000).strip()
            except Exception:
                continue

            times = re.findall(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b", element_text)
            if not times:
                continue

            try:
                context = element.evaluate(
                    """
                    element => {
                        let node = element;
                        let result = element.innerText || "";

                        for (let i = 0; i < 8 && node.parentElement; i++) {
                            node = node.parentElement;
                            const text = node.innerText || "";

                            if (text.length <= 2500) {
                                result = text;
                            }

                            if (
                                /IMAX[- ]?70mm/i.test(text) &&
                                (
                                    /2026/.test(text) ||
                                    /ledna|února|března|dubna|května|června|července|srpna|září|října|listopadu|prosince/i.test(text)
                                )
                            ) {
                                return text;
                            }
                        }

                        return result;
                    }
                    """
                )
            except Exception:
                continue

            combined_text = f"{context}\n{element_text}"

            if not re.search(r"IMAX[\s-]*70\s*mm|IMAX-70mm", combined_text, re.I):
                continue

            screening_date = parse_date(combined_text)
            if not screening_date or screening_date <= MINIMUM_DATE:
                continue

            href = element.get_attribute("href")
            purchase_url = urljoin(FILM_URL, href) if href else FILM_URL

            for screening_time in times:
                identifier = (
                    f"{screening_date.isoformat()}|"
                    f"{screening_time}|{purchase_url}"
                )

                screenings[identifier] = {
                    "id": identifier,
                    "date": screening_date.isoformat(),
                    "time": screening_time,
                    "url": purchase_url,
                }

        browser.close()

    return sorted(
        screenings.values(),
        key=lambda item: (item["date"], item["time"]),
    )


def load_previous_ids() -> set[str]:
    if not STATE_FILE.exists():
        return set()

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return set(data.get("screening_ids", []))
    except Exception:
        return set()


def save_state(screenings: list[dict]) -> None:
    state = {
        "updated_at": datetime.now().isoformat(),
        "screening_ids": [item["id"] for item in screenings],
        "screenings": screenings,
    }

    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> None:
    current_screenings = find_screenings()
    previous_ids = load_previous_ids()
    first_run = not STATE_FILE.exists()

    if first_run:
        save_state(current_screenings)

        send_notification(
            "Odyssea monitor je spuštěný",
            (
                "✅ GitHub monitor funguje.\n"
                f"Aktuálně nalezených projekcí po 2. 8. 2026: "
                f"{len(current_screenings)}.\n"
                "Na tyto současné termíny tě monitor znovu neupozorní."
            ),
            FILM_URL,
        )
        return

    new_screenings = [
        screening
        for screening in current_screenings
        if screening["id"] not in previous_ids
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

    save_state(current_screenings)


if __name__ == "__main__":
    main()
