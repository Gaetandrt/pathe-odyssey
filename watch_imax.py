#!/usr/bin/env python3
"""
Surveillance IMAX 70mm - L'Odyssée @ Pathé Odysseum
Envoie une push iPhone via ntfy.sh dès qu'une nouvelle date
ou des places disponibles apparaissent.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# ──────────────────────────────────────────────
# CONFIG — à adapter
# ──────────────────────────────────────────────
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "pathe-odysseum-imax-CHANGE_MOI")
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")

# Pathé Odysseum + L'Odyssée — projection IMAX 70mm (show dédié)
CINEMA_SLUG = "cinema-pathe-odysseum"
FILM_SLUG = "l-odyssee-projection-imax-70mm-54413"
FILM_ID = "54413"
BOOKING_URL = f"https://www.pathe.fr/films/{FILM_SLUG}?cinema={CINEMA_SLUG}"
SHOWTIMES_URL = (
    f"https://www.pathe.fr/api/show/{FILM_SLUG}/showtimes/{CINEMA_SLUG}"
)

# Fichier d'état (persiste entre les runs)
STATE_FILE = Path(os.getenv("STATE_FILE", Path(__file__).parent / "state.json"))

# Intervalle mini entre deux notifs identiques (anti-spam), en secondes
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "1800"))  # 30 min

# Anti-spam pour les alertes d'erreur / rate-limit
ERROR_COOLDOWN_SECONDS = int(os.getenv("ERROR_COOLDOWN_SECONDS", "3600"))  # 1 h

# Intervalle entre deux checks en mode --loop (Docker)
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))  # 5 min

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
        "Referer": "https://www.pathe.fr/",
    }
)


class RateLimitError(Exception):
    """HTTP 429 / rate-limit Pathé ou ntfy."""

    def __init__(self, message: str, *, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class ApiError(Exception):
    """Erreur API Pathé non récupérable pour ce cycle."""


# ──────────────────────────────────────────────
# État
# ──────────────────────────────────────────────
def load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("state.json corrompu, reset")
    return {
        "known_show_ids": [],
        "last_notify_hash": None,
        "last_notify_at": None,
        "last_error_hash": None,
        "last_error_at": None,
        "last_check_at": None,
    }


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ──────────────────────────────────────────────
# Notification push iPhone (ntfy)
# ──────────────────────────────────────────────
def send_push(
    title: str,
    message: str,
    *,
    priority: int = 5,
    click_url: str | None = None,
    tags: list[str] | None = None,
) -> bool:
    """Envoie une notification push via ntfy.sh."""
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
            {
                "action": "view",
                "label": "Réserver",
                "url": click_url,
                "clear": True,
            }
        ]

    try:
        r = session.post(
            f"{NTFY_SERVER}/",
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        r.raise_for_status()
        log.info("Push envoyée : %s", title)
        return True
    except requests.RequestException as e:
        log.error("Échec push ntfy : %s", e)
        return False


def _cooldown_active(
    state: dict,
    *,
    hash_key: str,
    at_key: str,
    msg_hash: str,
    cooldown: int,
) -> bool:
    if state.get(hash_key) != msg_hash:
        return False
    last_at = state.get(at_key)
    if not last_at:
        return False
    try:
        delta = (datetime.now(timezone.utc) - datetime.fromisoformat(last_at)).total_seconds()
    except ValueError:
        return False
    if delta < cooldown:
        log.info("Cooldown actif (%.0fs restants), skip notif", cooldown - delta)
        return True
    return False


def notify_if_new(state: dict, title: str, message: str, click_url: str) -> None:
    """Anti-spam : n'envoie pas 2 fois le même message dans le cooldown."""
    msg_hash = hashlib.sha256(f"{title}|{message}".encode()).hexdigest()
    if _cooldown_active(
        state,
        hash_key="last_notify_hash",
        at_key="last_notify_at",
        msg_hash=msg_hash,
        cooldown=COOLDOWN_SECONDS,
    ):
        return

    if send_push(title, message, click_url=click_url):
        state["last_notify_hash"] = msg_hash
        state["last_notify_at"] = datetime.now(timezone.utc).isoformat()


def notify_error(
    state: dict,
    title: str,
    message: str,
    *,
    tags: list[str] | None = None,
) -> None:
    """Push d'erreur / rate-limit avec cooldown dédié (évite le spam)."""
    msg_hash = hashlib.sha256(f"{title}|{message}".encode()).hexdigest()
    if _cooldown_active(
        state,
        hash_key="last_error_hash",
        at_key="last_error_at",
        msg_hash=msg_hash,
        cooldown=ERROR_COOLDOWN_SECONDS,
    ):
        return

    if send_push(title, message, priority=4, tags=tags or ["warning", "rotating_light"]):
        state["last_error_hash"] = msg_hash
        state["last_error_at"] = datetime.now(timezone.utc).isoformat()
        save_state(state)


# ──────────────────────────────────────────────
# Récupération des séances
# ──────────────────────────────────────────────
def parse_time(time_val: Any) -> str:
    """'2026-08-11 13:30:00' / ISO → '13:30'."""
    s = str(time_val).strip()
    if "T" in s:
        return s[11:16]
    if " " in s and len(s) >= 16:
        return s.split(" ", 1)[1][:5]
    if re.fullmatch(r"\d{1,2}:\d{2}", s):
        hh, mm = s.split(":")
        return f"{int(hh):02d}:{mm}"
    return s[:5]


def show_id_from(obj: dict[str, Any], date: str, hhmm: str) -> str:
    ref = obj.get("refCmd") or obj.get("id") or obj.get("showtimeId") or ""
    if ref:
        # ex. https://s.pathe.fr/fr/V3335S214676/booking → V3335S214676
        m = re.search(r"(V\d+S\d+)", str(ref))
        return m.group(1) if m else str(ref)
    return f"{date}-{hhmm}-{obj.get('version', '')}"


def normalize_show(obj: dict[str, Any], date_hint: str | None = None) -> dict[str, Any] | None:
    time_val = obj.get("time") or obj.get("startsAt") or obj.get("startTime")
    if not time_val:
        return None

    hhmm = parse_time(time_val)
    date = date_hint or str(time_val)[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        date = "?"

    status = str(obj.get("status", "")).lower()
    bookable = status in ("available", "almostfull", "fewseats", "")
    if status in ("soldout", "cancelled", "canceled", "unavailable"):
        bookable = False

    version = str(obj.get("version") or "").upper()
    if version == "VOST":
        version = "VOSTFR"

    booking = (
        obj.get("refCmd")
        or obj.get("bookingUrl")
        or obj.get("url")
        or BOOKING_URL
    )

    return {
        "id": show_id_from(obj, date, hhmm),
        "date": date,
        "time": hhmm,
        "label": "IMAX 70mm",
        "version": version,
        "bookable": bookable,
        "seats": obj.get("seatsAvailable") or obj.get("availableSeats"),
        "status": status or ("available" if bookable else "unknown"),
        "url": booking,
        "auditorium": obj.get("auditoriumName") or "IMAX",
    }


def parse_api_payload(data: Any, date_hint: str | None = None) -> list[dict[str, Any]]:
    """
    Parse Pathé showtimes :
    - dict { 'YYYY-MM-DD': [show, ...] }
    - list [show, ...] (endpoint /date)
    """
    results: list[dict[str, Any]] = []

    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                show = normalize_show(item, date_hint)
                if show:
                    results.append(show)
        return dedupe_shows(results)

    if isinstance(data, dict):
        # Endpoint calendrier complet
        date_keys = [k for k in data if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(k))]
        if date_keys:
            for k in date_keys:
                results.extend(parse_api_payload(data[k], date_hint=str(k)))
            return dedupe_shows(results)

        # Objet séance unique
        show = normalize_show(data, date_hint)
        if show:
            results.append(show)

    return dedupe_shows(results)


def dedupe_shows(shows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for s in shows:
        if s["id"] not in seen:
            seen.add(s["id"])
            unique.append(s)
    return unique


def _retry_after_seconds(response: requests.Response) -> int | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def fetch_via_api() -> list[dict[str, Any]]:
    """Endpoint dédié IMAX 70mm — calendrier complet."""
    urls = [
        f"{SHOWTIMES_URL}?language=fr",
        SHOWTIMES_URL,
    ]
    last_status: int | None = None
    last_error: str | None = None

    for url in urls:
        try:
            r = session.get(url, timeout=20)
            last_status = r.status_code

            if r.status_code == 429:
                retry = _retry_after_seconds(r)
                raise RateLimitError(
                    f"Rate-limit Pathé (429) sur {url}"
                    + (f" — Retry-After {retry}s" if retry else ""),
                    retry_after=retry,
                )

            if r.status_code in (403, 503):
                last_error = f"HTTP {r.status_code}"
                log.warning("API %s → %s", url, r.status_code)
                continue

            if r.status_code != 200:
                last_error = f"HTTP {r.status_code}"
                log.warning("API %s → %s", url, r.status_code)
                continue

            if "json" not in r.headers.get("content-type", ""):
                last_error = "réponse non-JSON"
                continue

            parsed = parse_api_payload(r.json())
            if parsed:
                bookable = sum(1 for s in parsed if s.get("bookable"))
                log.info(
                    "API OK : %d séances IMAX 70mm (%d dispo)",
                    len(parsed),
                    bookable,
                )
                return parsed
            last_error = "JSON vide / non parsable"
        except RateLimitError:
            raise
        except (requests.RequestException, ValueError) as e:
            last_error = str(e)
            log.warning("API fail %s : %s", url, e)

    if last_status in (403, 503) or last_error:
        raise ApiError(
            f"Impossible de récupérer les séances Pathé"
            f" (dernier statut={last_status}, détail={last_error})"
        )
    return []


def fetch_showtimes() -> list[dict[str, Any]]:
    return fetch_via_api()


# ──────────────────────────────────────────────
# Logique principale
# ──────────────────────────────────────────────
def format_show(s: dict[str, Any]) -> str:
    seats = f" ({s['seats']} places)" if s.get("seats") is not None else ""
    status = "✅" if s.get("bookable", True) else "❌ complet"
    ver = f" {s['version']}" if s.get("version") else ""
    return f"• {s['date']} {s['time']}{ver} — {s['label']}{seats} {status}"


def handle_check_failure(state: dict, exc: BaseException) -> None:
    if isinstance(exc, RateLimitError):
        retry = f"\nRetry-After : {exc.retry_after}s" if exc.retry_after else ""
        notify_error(
            state,
            "⚠️ Rate-limit Pathé",
            f"{exc}{retry}\nLe watcher réessaiera au prochain cycle.",
            tags=["hourglass", "warning"],
        )
        return

    if isinstance(exc, ApiError):
        notify_error(
            state,
            "⚠️ Erreur API Pathé",
            str(exc),
            tags=["warning"],
        )
        return

    notify_error(
        state,
        "🚨 Erreur watcher IMAX",
        f"{type(exc).__name__}: {exc}",
        tags=["rotating_light", "x"],
    )


def run_check(*, force_notify: bool = False) -> int:
    state = load_state()
    state["last_check_at"] = datetime.now(timezone.utc).isoformat()

    log.info("Check IMAX 70mm Odysseum…")
    try:
        shows = fetch_showtimes()
    except (RateLimitError, ApiError) as e:
        log.error("%s", e)
        handle_check_failure(state, e)
        save_state(state)
        return 1

    if not shows:
        log.warning("Aucune séance IMAX 70mm trouvée (API vide ou changée)")
        notify_error(
            state,
            "⚠️ Aucune séance IMAX 70mm",
            "L'API a répondu sans séance. Le show Pathé a peut-être changé de slug.",
            tags=["warning"],
        )
        if force_notify:
            send_push(
                "IMAX watcher — aucune séance",
                "Le script n'a trouvé aucune séance IMAX 70mm.",
                priority=3,
                tags=["warning"],
            )
        save_state(state)
        return 1

    known_ids = set(state.get("known_show_ids") or [])
    current_ids = {s["id"] for s in shows}
    new_shows = sorted(
        (s for s in shows if s["id"] not in known_ids),
        key=lambda s: (s.get("date") or "", s.get("time") or ""),
    )

    log.info(
        "%d séances IMAX 70 | +%d nouvelles",
        len(shows),
        len(new_shows),
    )

    # Premier run : baseline silencieuse (pas de notif)
    if not known_ids and not force_notify:
        log.info("Premier run — baseline enregistrée (%d séances), silence", len(shows))
        state["known_show_ids"] = sorted(current_ids)
        save_state(state)
        return 0

    # Uniquement les séances qui viennent d'apparaître
    if new_shows:
        msg = f"{len(new_shows)} nouvelle(s) séance(s) :\n\n" + "\n".join(
            format_show(s) for s in new_shows[:15]
        )
        if len(new_shows) > 15:
            msg += f"\n… +{len(new_shows) - 15} autres"
        notify_if_new(
            state,
            "🎟️ Nouvelle séance IMAX 70mm !",
            msg,
            BOOKING_URL,
        )
    elif force_notify:
        bookable = [s for s in shows if s.get("bookable", True)]
        msg = f"{len(bookable)} séance(s) bookable(s) :\n\n" + "\n".join(
            format_show(s) for s in bookable[:15]
        )
        send_push("IMAX 70mm — état actuel", msg, click_url=BOOKING_URL, priority=3)
    else:
        log.info("Aucune nouvelle séance")

    state["known_show_ids"] = sorted(known_ids | current_ids)
    if len(state["known_show_ids"]) > 500:
        state["known_show_ids"] = sorted(current_ids)

    save_state(state)
    return 0


def run_loop() -> None:
    log.info(
        "Mode boucle — check toutes les %ds (topic=%s)",
        CHECK_INTERVAL_SECONDS,
        NTFY_TOPIC if "CHANGE_MOI" not in NTFY_TOPIC else "NON CONFIGURÉ",
    )
    while True:
        try:
            run_check()
        except Exception as e:
            log.exception("Erreur pendant le check — retry au prochain cycle")
            try:
                handle_check_failure(load_state(), e)
            except Exception:
                log.exception("Impossible d'envoyer la notif d'erreur")
        time.sleep(CHECK_INTERVAL_SECONDS)


def main() -> None:
    force = "--notify" in sys.argv or "--init-notify" in sys.argv
    test = "--test-push" in sys.argv
    loop = "--loop" in sys.argv or os.getenv("LOOP", "").lower() in ("1", "true", "yes")

    if test:
        ok = send_push(
            "Test IMAX watcher",
            "Si tu lis ça sur ton iPhone, ntfy est bien configuré 👍",
            priority=4,
            tags=["white_check_mark"],
            click_url=BOOKING_URL,
        )
        sys.exit(0 if ok else 1)

    if loop:
        run_loop()
        return

    code = run_check(force_notify=force)
    sys.exit(code)


if __name__ == "__main__":
    main()
