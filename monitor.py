import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from playwright.sync_api import Response, sync_playwright


FILM_URL = "https://www.cinemacity.cz/films/odyssea/7268s2r"
CINEMA_URL = "https://www.cinemacity.cz/cinemas/flora/1052"

NTFY_TOPIC = os.environ["NTFY_TOPIC"]

STATE_FILE = Path("screenings_state.json")
DEBUG_FILE = Path("monitor_debug.json")

# Hlídáme pouze projekce od 13. 8. 2026 včetně.
MINIMUM_DATE_EXCLUSIVE = date(2026, 8, 12)

TITLE_WORDS = ("odyssea", "the odyssey")
FORMAT_PATTERNS = (
    r"imax[\s_-]*70(?:[\s_-]*mm)?",
    r"70[\s_-]*mm",
)

ISO_DATETIME_RE = re.compile(
    r"\b(20\d{2})-(\d{2})-(\d{2})[T ]"
    r"([01]\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?"
)
CZ_DATETIME_RE = re.compile(
    r"\b(\d{1,2})\.\s*(\d{1,2})\.\s*(20\d{2})"
    r"(?:\s+|[^0-9]{1,20})"
    r"([01]?\d|2[0-3]):([0-5]\d)\b"
)
URL_RE = re.compile(r"https?://[^\s\"'<>]+")


def send_notification(
    title: str,
    message: str,
    click_url: str = FILM_URL,
    priority: int = 5,
) -> None:
    response = requests.post(
        "https://ntfy.sh",
        json={
            "topic": NTFY_TOPIC,
            "title": title,
            "message": message,
            "priority": priority,
            "tags": ["movie_camera", "ticket"],
            "click": click_url,
        },
        timeout=30,
    )
    response.raise_for_status()


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def contains_title(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in TITLE_WORDS)


def contains_format(text: str) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in FORMAT_PATTERNS)


def extract_datetimes(text: str) -> set[tuple[date, str]]:
    found: set[tuple[date, str]] = set()

    for match in ISO_DATETIME_RE.finditer(text):
        year, month, day, hour, minute = map(int, match.groups())
        try:
            found.add((date(year, month, day), f"{hour:02d}:{minute:02d}"))
        except ValueError:
            pass

    for match in CZ_DATETIME_RE.finditer(text):
        day, month, year, hour, minute = map(int, match.groups())
        try:
            found.add((date(year, month, day), f"{hour:02d}:{minute:02d}"))
        except ValueError:
            pass

    return found


def extract_booking_url(value: Any, base_url: str) -> str:
    preferred_keys = (
        "bookingUrl",
        "bookingURL",
        "booking_url",
        "ticketUrl",
        "ticketURL",
        "ticket_url",
        "purchaseUrl",
        "purchaseURL",
        "purchase_url",
        "deepLink",
        "deeplink",
        "url",
        "href",
    )

    if isinstance(value, dict):
        for key in preferred_keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return urljoin(base_url, candidate.strip())

    text = compact_json(value)
    urls = URL_RE.findall(text)

    for url in urls:
        lower = url.lower()
        if any(word in lower for word in ("book", "ticket", "buy", "purchase", "booking")):
            return url.rstrip("\\,}]")

    return FILM_URL


def walk_json(value: Any):
    yield value

    if isinstance(value, dict):
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def extract_screenings_from_json(
    payload: Any,
    source_url: str,
) -> list[dict]:
    results: dict[str, dict] = {}

    for node in walk_json(payload):
        if not isinstance(node, (dict, list)):
            continue

        text = compact_json(node)

        # Kandidát musí ve stejném JSON podstromu obsahovat film i formát.
        if not contains_title(text) or not contains_format(text):
            continue

        datetimes = extract_datetimes(text)
        if not datetimes:
            continue

        booking_url = extract_booking_url(node, source_url)

        for screening_date, screening_time in datetimes:
            identifier = (
                f"{screening_date.isoformat()}|"
                f"{screening_time}|{booking_url}"
            )
            results[identifier] = {
                "id": identifier,
                "date": screening_date.isoformat(),
                "time": screening_time,
                "url": booking_url,
                "source": source_url,
            }

    return list(results.values())


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}

    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(all_screenings: list[dict], relevant: list[dict]) -> None:
    STATE_FILE.write_text(
        json.dumps(
            {
                "updated_at": datetime.now().isoformat(),
                "minimum_date_exclusive": MINIMUM_DATE_EXCLUSIVE.isoformat(),
                "all_found_count": len(all_screenings),
                "relevant_count": len(relevant),
                "screening_ids": [item["id"] for item in relevant],
                "all_found_screenings": all_screenings,
                "relevant_screenings": relevant,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def find_screenings() -> tuple[list[dict], dict]:
    captured: list[dict] = []
    results: dict[str, dict] = {}

    def handle_response(response: Response) -> None:
        content_type = (response.headers.get("content-type") or "").lower()

        if "json" not in content_type:
            return

        try:
            payload = response.json()
        except Exception:
            return

        entry = {
            "url": response.url,
            "status": response.status,
            "content_type": content_type,
            "payload": payload,
        }
        captured.append(entry)

        for item in extract_screenings_from_json(payload, response.url):
            results[item["id"]] = item

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)

        context = browser.new_context(
            locale="cs-CZ",
            viewport={"width": 1440, "height": 1800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/150 Safari/537.36"
            ),
        )

        page = context.new_page()
        page.on("response", handle_response)

        for url in (FILM_URL, CINEMA_URL):
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(12000)

            # Kliknutí na dostupné datumové ovladače vyvolá další API požadavky.
            controls = page.locator(
                "button, a, [role='button'], input[type='radio'], "
                "[data-date], [data-show-date]"
            )

            limit = min(controls.count(), 250)

            for index in range(limit):
                control = controls.nth(index)

                try:
                    text = (
                        (control.inner_text(timeout=500) or "")
                        + " "
                        + (control.get_attribute("aria-label") or "")
                        + " "
                        + (control.get_attribute("title") or "")
                        + " "
                        + (control.get_attribute("data-date") or "")
                        + " "
                        + (control.get_attribute("datetime") or "")
                    )
                except Exception:
                    continue

                if not (
                    re.search(r"\b\d{1,2}\.\s*\d{1,2}\.", text)
                    or re.search(r"\b20\d{2}-\d{2}-\d{2}\b", text)
                    or re.search(
                        r"led|úno|bře|dub|kvě|čvn|čvc|srp|zář|říj|lis|pro",
                        text,
                        re.IGNORECASE,
                    )
                ):
                    continue

                try:
                    control.click(timeout=2000)
                    page.wait_for_timeout(1200)
                except Exception:
                    pass

        browser.close()

    debug = {
        "captured_json_response_count": len(captured),
        "captured_responses": captured,
        "extracted_screenings": sorted(
            results.values(),
            key=lambda item: (item["date"], item["time"], item["url"]),
        ),
    }

    DEBUG_FILE.write_text(
        json.dumps(debug, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    screenings = debug["extracted_screenings"]
    return screenings, debug


def main() -> None:
    all_screenings, debug = find_screenings()

    # Důležité: při selhání čtení dat se běh nemá tvářit zeleně.
    if not all_screenings:
        send_notification(
            "Odyssea monitor: chyba kontroly",
            (
                "⚠️ Monitor dnes nerozpoznal žádnou projekci "
                "Odyssey v IMAX 70 mm.\n"
                f"Zachycených JSON odpovědí: "
                f"{debug['captured_json_response_count']}.\n"
                "GitHub běh skončí chybou, aby se problém neztratil."
            ),
            FILM_URL,
            priority=4,
        )
        raise RuntimeError(
            "Nebyly rozpoznány žádné projekce. "
            "Podrobnosti jsou v monitor_debug.json."
        )

    relevant = [
        item
        for item in all_screenings
        if datetime.strptime(item["date"], "%Y-%m-%d").date()
        > MINIMUM_DATE_EXCLUSIVE
    ]

    previous_state = load_state()
    previous_ids = set(previous_state.get("screening_ids", []))
    first_successful_run = not previous_state

    if first_successful_run:
        save_state(all_screenings, relevant)
        send_notification(
            "Odyssea API monitor je spuštěný",
            (
                "✅ Monitor načetl skutečná JSON data webu.\n"
                f"Celkem IMAX 70mm projekcí: {len(all_screenings)}.\n"
                f"Projekcí od 13. 8. 2026: {len(relevant)}.\n"
                "Další upozornění přijde jen na nově přidaný termín."
            ),
            FILM_URL,
        )
        return

    new_screenings = [
        item for item in relevant
        if item["id"] not in previous_ids
    ]

    for screening in new_screenings:
        readable_date = datetime.strptime(
            screening["date"], "%Y-%m-%d"
        ).strftime("%d. %m. %Y")

        send_notification(
            "Nová Odyssea v IMAX 70 mm",
            (
                "🎬 Odyssea – IMAX 70 mm\n"
                f"📅 {readable_date}\n"
                f"🕒 {screening['time']}\n"
                "🎟️ Klepnutím otevřeš nákup vstupenek."
            ),
            screening["url"],
        )

    save_state(all_screenings, relevant)


if __name__ == "__main__":
    main()
