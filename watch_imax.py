#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Surveillance IMAX 70mm - L'Odyssee @ Pathe Odysseum
Push iPhone via ntfy.sh
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------
# CONFIG
# --------------------------------------------------
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "pathe-odysseum-imax-CHANGE_MOI")
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")

CINEMA_SLUG = "cinema-pathe-odysseum"

# Evenement IMAX 70mm (correct) + film classique (fallback)
EVENT_SLUG = "l-odyssee-projection-imax-70mm-54413"
EVENT_ID = "54413"
FILM_SLUG = "l-odyssee-43836"
FILM_ID = "43836"

# Endpoints API - ordre de priorite (event d'abord)
API_SHOWTIME_URLS = [
    f"https://www.pathe.fr/api/event/{EVENT_SLUG}/showtimes/{CINEMA_SLUG}",
    f"https://www.pathe.fr/api/events/{EVENT_SLUG}/showtimes/{CINEMA_SLUG}",
    f"https://www.pathe.fr/api/event/{EVENT_ID}/showtimes/{CINEMA_SLUG}",
    f"https://www.pathe.fr/api/show/{FILM_SLUG}/showtimes/{CINEMA_SLUG}",
    f"https://www.pathe.fr/api/show/{FILM_ID}/showtimes/{CINEMA_SLUG}",
    f"https://www.pathe.fr/api/cinema/{CINEMA_SLUG}/shows",
    f"https://www.pathe.fr/api/cinemas/{CINEMA_SLUG}/showtimes",
]

# Pages HTML de secours
HTML_URLS = [
    f"https://www.pathe.fr/evenements/{EVENT_SLUG}",
    f"https://www.pathe.fr/cinemas/{CINEMA_SLUG}",
    f"https://www.pathe.fr/films/{FILM_SLUG}?cinema={CINEMA_SLUG}",
]

BOOKING_URL = f"https://www.pathe.fr/evenements/{EVENT_SLUG}"

IMAX_70_KEYWORDS = (
    "imax 70",
    "70mm",
    "70 mm",
    "imax(r) 70",
    "projection imax 70",
    "argentique",
)

STATE_FILE = Path(os.getenv("STATE_FILE", Path(__file__).parent / "state.json"))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "1800"))

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("imax-watcher")

session = requests.Session()
session.headers.update(
    {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/html, */*",
        "Accept-Language": "fr-FR,fr;q=0.9",
        "Origin": "https://www.pathe.fr",
        "Referer": f"https://www.pathe.fr/evenements/{EVENT_SLUG}",
    }
)


# --------------------------------------------------
# Etat
# --------------------------------------------------
def load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("state.json corrompu, reset")
    return {
        "known_show_ids": [],
        "known_dates": [],
        "last_notify_hash": None,
        "last_notify_at": None,
        "last_check_at": None,
        "last_api_url": None,
    }


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# --------------------------------------------------
# Push ntfy (iPhone)
# --------------------------------------------------
def send_push(
    title: str,
    message: str,
    *,
    priority: int = 5,
    click_url: str | None = None,
    tags: list[str] | None = None,
) -> bool:
    if "CHANGE_MOI" in NTFY_TOPIC:
        log.error("Configure NTFY_TOPIC avant de lancer le script !")
        return False

    payload: dict[str, Any] = {
        "topic": NTFY_TOPIC,
        "title": title,
        "message": message,
        "priority": priority,
        "tags": tags or ["movie_camera", "tada"],
    }
    if click_url:
        payload["click"] = click_url
        payload["actions"] = [
            {"action": "view", "label": "Reserver", "url": click_url, "clear": True}
        ]

    try:
        r = session.post(
            f"{NTFY_SERVER}/",
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        r.raise_for_status()
        log.info("Push OK : %s", title)
        return True
    except requests.RequestException as e:
        log.error("Push ntfy echouee : %s", e)
        return False


def notify_if_new(state: dict, title: str, message: str, click_url: str) -> None:
    msg_hash = hashlib.sha256(f"{title}|{message}".encode()).hexdigest()
    now = datetime.now(timezone.utc)
    last_at = state.get("last_notify_at")
    if state.get("last_notify_hash") == msg_hash and last_at:
        try:
            delta = (now - datetime.fromisoformat(last_at)).total_seconds()
            if delta < COOLDOWN_SECONDS:
                log.info("Cooldown (%.0fs restants), skip notif", COOLDOWN_SECONDS - delta)
                return
        except ValueError:
            pass
    if send_push(title, message, click_url=click_url):
        state["last_notify_hash"] = msg_hash
        state["last_notify_at"] = now.isoformat()


# --------------------------------------------------
# Parsing
# --------------------------------------------------
def is_imax_70(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in IMAX_70_KEYWORDS)


def parse_api_payload(data: Any, source_is_event: bool = False) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    def walk(obj: Any, date_hint: str | None = None) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(k)):
                    walk(v, date_hint=str(k))
                else:
                    time_val = (
                        obj.get("time")
                        or obj.get("startsAt")
                        or obj.get("startTime")
                        or obj.get("hour")
                        or obj.get("start")
                    )
                    show_id = str(
                        obj.get("id")
                        or obj.get("showtimeId")
                        or obj.get("ref")
                        or obj.get("uuid")
                        or ""
                    )
                    label_parts = []
                    for f in (
                        "experience",
                        "format",
                        "screenType",
                        "screen",
                        "version",
                        "tags",
                        "label",
                        "title",
                        "name",
                        "diffusionVersion",
                    ):
                        val = obj.get(f)
                        if isinstance(val, list):
                            label_parts.extend(str(x) for x in val)
                        elif val:
                            label_parts.append(str(val))
                    label = " ".join(label_parts)

                    bookable = obj.get(
                        "isBookable",
                        obj.get("bookable", obj.get("available", obj.get("isAvailable"))),
                    )
                    seats = (
                        obj.get("seatsAvailable")
                        or obj.get("availableSeats")
                        or obj.get("freeSeats")
                        or obj.get("remainingSeats")
                    )

                    if time_val and (show_id or date_hint):
                        t = str(time_val)
                        hhmm = t[11:16] if "T" in t else t[:5]
                        results.append(
                            {
                                "id": show_id or f"{date_hint}-{hhmm}-{label}",
                                "date": date_hint or str(obj.get("date", ""))[:10],
                                "time": hhmm,
                                "label": label.strip() or ("IMAX 70mm" if source_is_event else ""),
                                "version": str(
                                    obj.get("version")
                                    or obj.get("language")
                                    or obj.get("diffusionVersion")
                                    or ""
                                ),
                                "bookable": bool(bookable) if bookable is not None else True,
                                "seats": seats,
                                "url": obj.get("bookingUrl")
                                or obj.get("url")
                                or BOOKING_URL,
                            }
                        )
                        return
                    walk(v, date_hint=date_hint)
        elif isinstance(obj, list):
            for item in obj:
                walk(item, date_hint=date_hint)

    walk(data)

    seen: set[str] = set()
    unique = []
    for s in results:
        if s["id"] not in seen:
            seen.add(s["id"])
            unique.append(s)
    return unique


def filter_imax_70(shows: list[dict], source_is_event: bool) -> list[dict]:
    """Si la source est l'evenement 70mm, on garde tout. Sinon on filtre."""
    if source_is_event:
        for s in shows:
            if not s.get("label"):
                s["label"] = "IMAX 70mm"
        return shows
    return [
        s
        for s in shows
        if is_imax_70(f"{s.get('label', '')} {s.get('version', '')}")
    ]


# --------------------------------------------------
# Fetch
# --------------------------------------------------
def fetch_via_api() -> tuple[list[dict[str, Any]], str | None]:
    for url in API_SHOWTIME_URLS:
        try:
            r = session.get(url, timeout=20)
            log.info("API %s -> %s", url, r.status_code)

            if r.status_code != 200:
                try:
                    err = r.json()
                    log.warning("  body: %s", err)
                except Exception:
                    log.warning("  body: %s", r.text[:200])
                continue

            ct = r.headers.get("content-type", "")
            if "json" not in ct:
                continue

            data = r.json()
            if isinstance(data, dict):
                err = str(data.get("error", data.get("message", ""))).lower()
                if "no movie" in err or "not allowed" in err:
                    log.warning("  refuse: %s", data)
                    continue

            source_is_event = "/event" in url
            parsed = parse_api_payload(data, source_is_event=source_is_event)
            imax = filter_imax_70(parsed, source_is_event)

            if imax:
                log.info("API OK (%d seances IMAX 70) : %s", len(imax), url)
                return imax, url

            log.info("  JSON OK mais 0 seance IMAX 70 (brut=%d)", len(parsed))
        except (requests.RequestException, ValueError) as e:
            log.debug("API fail %s : %s", url, e)

    return [], None


def fetch_via_html() -> list[dict[str, Any]]:
    shows: list[dict[str, Any]] = []

    for url in HTML_URLS:
        try:
            r = session.get(url, timeout=25)
            r.raise_for_status()
        except requests.RequestException as e:
            log.warning("HTML fail %s : %s", url, e)
            continue

        soup = BeautifulSoup(r.text, "html.parser")

        next_data = soup.find("script", id="__NEXT_DATA__")
        if next_data and next_data.string:
            try:
                payload = json.loads(next_data.string)
                source_is_event = "evenement" in url
                parsed = parse_api_payload(payload, source_is_event=source_is_event)
                imax = filter_imax_70(parsed, source_is_event)
                if imax:
                    log.info("HTML+JSON OK : %s (%d)", url, len(imax))
                    return imax
            except json.JSONDecodeError:
                pass

        page_text = soup.get_text(" ", strip=True)
        if not is_imax_70(page_text) and "evenement" not in url:
            continue

        date_re = re.compile(
            r"(\d{4}-\d{2}-\d{2})"
            r"|((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun|Lun|Mar|Mer|Jeu|Ven|Sam|Dim)\.?\s+\d{1,2}\s+\w+)",
            re.I,
        )
        time_re = re.compile(r"\b([01]?\d|2[0-3])[h:]([0-5]\d)\b")

        for el in soup.find_all(
            string=re.compile(r"70\s*mm|IMAX\s*70|Projection IMAX", re.I)
        ):
            parent = el.find_parent(["section", "div", "article", "li"]) or el.parent
            if not parent:
                continue
            block = parent.get_text(" ", strip=True)
            dates = date_re.findall(block)
            times = time_re.findall(block)
            if not times:
                grand = parent.find_parent(["section", "div", "article"])
                if grand:
                    block = grand.get_text(" ", strip=True)
                    dates = date_re.findall(block)
                    times = time_re.findall(block)

            date_str = None
            if dates:
                raw = dates[0]
                date_str = raw[0] if raw[0] else raw[1]

            for t in times:
                hhmm = f"{int(t[0]):02d}:{t[1]}"
                sid = f"html-{date_str or 'unk'}-{hhmm}"
                shows.append(
                    {
                        "id": sid,
                        "date": date_str or "?",
                        "time": hhmm,
                        "label": "IMAX 70mm",
                        "version": "VOSTFR" if "vost" in block.lower() else "",
                        "bookable": not any(
                            w in block.lower()
                            for w in ("complet", "epuise", "sold out", "indisponible")
                        ),
                        "seats": None,
                        "url": BOOKING_URL,
                    }
                )

        if shows:
            log.info("HTML DOM OK : %s (%d)", url, len(shows))
            break

    seen: set[str] = set()
    unique = []
    for s in shows:
        if s["id"] not in seen:
            seen.add(s["id"])
            unique.append(s)
    return unique


def fetch_showtimes() -> tuple[list[dict[str, Any]], str | None]:
    shows, api_url = fetch_via_api()
    if shows:
        return shows, api_url
    log.info("API vide -> fallback HTML")
    return fetch_via_html(), None


# --------------------------------------------------
# Main logic
# --------------------------------------------------
def format_show(s: dict[str, Any]) -> str:
    seats = f" ({s['seats']} places)" if s.get("seats") is not None else ""
    status = "OK" if s.get("bookable", True) else "COMPLET"
    ver = f" {s['version']}" if s.get("version") else ""
    return f"- {s['date']} {s['time']}{ver} : {s.get('label') or 'IMAX 70mm'}{seats} [{status}]"


def run_check(*, force_notify: bool = False) -> int:
    state = load_state()
    state["last_check_at"] = datetime.now(timezone.utc).isoformat()

    log.info("Check IMAX 70mm Odysseum...")
    shows, api_url = fetch_showtimes()
    if api_url:
        state["last_api_url"] = api_url

    if not shows:
        log.warning("Aucune seance IMAX 70mm trouvee")
        if force_notify:
            send_push(
                "IMAX watcher - 0 seance",
                "Aucun endpoint n'a renvoye de seances. Verifie les URLs / le WAF.",
                priority=3,
                tags=["warning"],
                click_url=BOOKING_URL,
            )
        save_state(state)
        return 1

    bookable = [s for s in shows if s.get("bookable", True)]
    known_ids = set(state.get("known_show_ids") or [])
    known_dates = set(state.get("known_dates") or [])

    current_ids = {s["id"] for s in shows}
    current_dates = {s["date"] for s in shows if s.get("date") and s["date"] != "?"}

    new_shows = [s for s in shows if s["id"] not in known_ids]
    new_bookable = [s for s in bookable if s["id"] not in known_ids]
    new_dates = sorted(current_dates - known_dates)

    log.info(
        "%d IMAX 70 (%d bookables) | +%d new | +%d dates | api=%s",
        len(shows),
        len(bookable),
        len(new_shows),
        len(new_dates),
        api_url or "html",
    )

    is_first_run = not known_ids and not force_notify
    if is_first_run:
        log.info("Premier run - baseline")
        state["known_show_ids"] = sorted(current_ids)
        state["known_dates"] = sorted(current_dates)
        save_state(state)
        send_push(
            "Watcher IMAX 70mm actif",
            f"Baseline : {len(shows)} seance(s) jusqu'au "
            f"{max(current_dates) if current_dates else '?'}.\n"
            f"API: {api_url or 'HTML fallback'}",
            priority=3,
            click_url=BOOKING_URL,
            tags=["white_check_mark", "movie_camera"],
        )
        return 0

    if new_dates:
        lines = [format_show(s) for s in bookable if s["date"] in new_dates] or [
            format_show(s) for s in shows if s["date"] in new_dates
        ]
        msg = f"Nouvelles dates : {', '.join(new_dates)}\n\n" + "\n".join(lines[:12])
        notify_if_new(state, "Nouvelles dates IMAX 70mm !", msg, BOOKING_URL)
    elif new_bookable:
        msg = "Places dispo :\n\n" + "\n".join(format_show(s) for s in new_bookable[:12])
        notify_if_new(state, "Places IMAX 70mm disponibles !", msg, BOOKING_URL)
    elif force_notify:
        msg = f"{len(bookable)} bookable(s):\n\n" + "\n".join(
            format_show(s) for s in bookable[:15]
        )
        send_push("IMAX 70mm - etat", msg, click_url=BOOKING_URL, priority=3)

    state["known_show_ids"] = sorted(known_ids | current_ids)
    state["known_dates"] = sorted(known_dates | current_dates)
    if len(state["known_show_ids"]) > 500:
        state["known_show_ids"] = sorted(current_ids)

    save_state(state)
    return 0


def main() -> None:
    if "--test-push" in sys.argv:
        ok = send_push(
            "Test IMAX watcher",
            "ntfy OK sur iPhone",
            priority=4,
            tags=["white_check_mark"],
            click_url=BOOKING_URL,
        )
        sys.exit(0 if ok else 1)

    if "--probe" in sys.argv:
        for url in API_SHOWTIME_URLS:
            try:
                r = session.get(url, timeout=15)
                snippet = r.text[:180].replace("\n", " ")
                print(f"[{r.status_code}] {url}\n  -> {snippet}\n")
            except Exception as e:
                print(f"[ERR] {url}\n  -> {e}\n")
        sys.exit(0)

    force = "--notify" in sys.argv
    sys.exit(run_check(force_notify=force))


if __name__ == "__main__":
    main()
