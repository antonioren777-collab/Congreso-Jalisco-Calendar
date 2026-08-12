from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, date, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dateutil.relativedelta import relativedelta
from icalendar import Calendar, Event
from zoneinfo import ZoneInfo

from config import (
    GACETA_BASE_URL,
    GACETA_CALENDAR_URL,
    AGENDA_BASE_URL,
    MONTH_URL,
    OUTPUT_FILE,
    TIMEZONE,
    COMMISSION_PATTERNS,
    PLENO_PATTERNS,
    LOOK_AHEAD_MONTHS,
    REQUEST_TIMEOUT,
    USER_AGENT,
    START_YEAR,
    START_MONTH,
)

TZ = ZoneInfo(TIMEZONE)

MONTHS = {
    "enero": 1,
    "ene": 1,
    "febrero": 2,
    "feb": 2,
    "marzo": 3,
    "mar": 3,
    "abril": 4,
    "abr": 4,
    "mayo": 5,
    "may": 5,
    "junio": 6,
    "jun": 6,
    "julio": 7,
    "jul": 7,
    "agosto": 8,
    "ago": 8,
    "septiembre": 9,
    "sept": 9,
    "sep": 9,
    "setiembre": 9,
    "octubre": 10,
    "oct": 10,
    "noviembre": 11,
    "nov": 11,
    "diciembre": 12,
    "dic": 12,
}


# ============================================================
# UTILIDADES
# ============================================================

def normalize(text: str) -> str:
    text = html.unescape(text or "")
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def get_session() -> requests.Session:
    session = requests.Session()

    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept-Language": "es-MX,es;q=0.9,en;q=0.8",
        }
    )

    return session


def month_name(month: int) -> str:
    names = {
        1: "enero",
        2: "febrero",
        3: "marzo",
        4: "abril",
        5: "mayo",
        6: "junio",
        7: "julio",
        8: "agosto",
        9: "septiembre",
        10: "octubre",
        11: "noviembre",
        12: "diciembre",
    }

    return names[month]


# ============================================================
# CLASIFICACIÓN
# ============================================================

def is_pleno(title: str) -> bool:
    text = normalize(title).casefold()

    return any(
        pattern.casefold() in text
        for pattern in PLENO_PATTERNS
    )


def is_commission(title: str) -> bool:
    text = normalize(title).casefold()

    return any(
        pattern.casefold() in text
        for pattern in COMMISSION_PATTERNS
    )


def classify(title: str) -> str | None:

    if is_pleno(title):
        return "Pleno"

    if is_commission(title):
        return (
            "Comisión de Higiene, Salud y "
            "Prevención de las Adicciones"
        )

    return None


def is_target(title: str) -> bool:
    return classify(title) is not None


# ============================================================
# FECHAS
# ============================================================

def parse_date_time(
    text: str,
    default_year: int,
    default_month: int,
):
    text = normalize(text)

    # Ejemplos:
    # 9 Jul 2026 - 09:00
    # 15 Julio 2026 - 10:00
    # 21 Julio 2026 - 12:00

    pattern = re.compile(
        r"(\d{1,2})\s+"
        r"([A-Za-zÁÉÍÓÚáéíóúñÑ]+)\s+"
        r"(\d{4})"
        r"(?:\s*-\s*|\s+)"
        r"(\d{1,2}):(\d{2})",
        flags=re.I,
    )

    match = pattern.search(text)

    if match:
        day = int(match.group(1))
        month_text = match.group(2).casefold()
        year = int(match.group(3))
        hour = int(match.group(4))
        minute = int(match.group(5))

        month = MONTHS.get(month_text, default_month)

        try:
            return datetime(
                year,
                month,
                day,
                hour,
                minute,
                tzinfo=TZ,
            )
        except ValueError:
            return None

    # Formato:
    # 15/07/2026 - 10:00

    numeric = re.compile(
        r"(\d{1,2})[/-]"
        r"(\d{1,2})[/-]"
        r"(\d{4})"
        r"\s*(?:-|a las)?\s*"
        r"(\d{1,2}):(\d{2})",
        flags=re.I,
    )

    match = numeric.search(text)

    if match:
        try:
            return datetime(
                int(match.group(3)),
                int(match.group(2)),
                int(match.group(1)),
                int(match.group(4)),
                int(match.group(5)),
                tzinfo=TZ,
            )
        except ValueError:
            return None

    return None


def parse_date_only(
    text: str,
    default_year: int,
    default_month: int,
):
    text = normalize(text)

    pattern = re.compile(
        r"(\d{1,2})\s+"
        r"([A-Za-zÁÉÍÓÚáéíóúñÑ]+)"
        r"(?:\s+(\d{4}))?",
        flags=re.I,
    )

    for match in pattern.finditer(text):

        day = int(match.group(1))
        month_text = match.group(2).casefold()

        month = MONTHS.get(month_text)

        if month is None:
            continue

        year = (
            int(match.group(3))
            if match.group(3)
            else default_year
        )

        if month != default_month:
            continue

        try:
            return date(year, month, day)
        except ValueError:
            continue

    return None


def parse_end_time(
    text: str,
    start: datetime,
):
    text = normalize(text)

    pattern = re.compile(
        r"-\s*(\d{1,2}):(\d{2})"
        r"\s+a\s+"
        r"(\d{1,2}):(\d{2})",
        flags=re.I,
    )

    match = pattern.search(text)

    if not match:
        return start + timedelta(hours=1)

    end = start.replace(
        hour=int(match.group(3)),
        minute=int(match.group(4)),
    )

    if end <= start:
        end += timedelta(days=1)

    return end


# ============================================================
# GACETA
# ============================================================

def verify_gaceta_calendar(
    session: requests.Session,
) -> bool:
    """
    Comprueba que el calendario oficial de la Gaceta
    siga disponible.

    No dependemos del texto extraído del PDF para identificar
    las fechas, porque las marcas gráficas del calendario no
    son fiables mediante extracción de texto.
    """

    try:
        response = session.get(
            GACETA_CALENDAR_URL,
            timeout=REQUEST_TIMEOUT,
        )

        response.raise_for_status()

        content_type = response.headers.get(
            "Content-Type",
            "",
        ).lower()

        if "pdf" not in content_type:
            print(
                "ADVERTENCIA: la Gaceta respondió, "
                "pero no indicó PDF."
            )

        if len(response.content) < 10_000:
            print(
                "ADVERTENCIA: el PDF de la Gaceta "
                "parece demasiado pequeño."
            )

        print(
            "Gaceta: calendario oficial 2026 "
            "disponible."
        )

        return True

    except requests.RequestException as exc:

        print(
            "ADVERTENCIA: no se pudo consultar "
            f"la Gaceta: {exc}"
        )

        return False


# ============================================================
# AGENDA PARLAMENTARIA
# ============================================================

def extract_events_from_month(
    session: requests.Session,
    year: int,
    month: int,
) -> list[dict]:

    url = MONTH_URL.format(
        year=year,
        month=month,
    )

    print(
        f"Consultando agenda: "
        f"{year}-{month:02d}"
    )

    try:
        response = session.get(
            url,
            timeout=REQUEST_TIMEOUT,
        )

        response.raise_for_status()

    except requests.RequestException as exc:

        print(
            f"ERROR agenda {year}-{month:02d}: "
            f"{exc}"
        )

        return []

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    results = []
    seen = set()

    # Buscamos enlaces relacionados con la agenda.

    candidates = soup.select(
        'a[href*="/agenda/"], '
        'a[href*="/agenda-parlamentaria/"]'
    )

    # Si cambia la estructura del HTML,
    # examinamos todos los enlaces.

    if not candidates:
        candidates = soup.find_all(
            "a",
            href=True,
        )

    for link in candidates:

        title = normalize(
            link.get_text(
                " ",
                strip=True,
            )
        )

        if not title:
            continue

        if not is_target(title):
            continue

        href = link.get("href")

        event_url = (
            urljoin(
                AGENDA_BASE_URL,
                href,
            )
            if href
            else url
        )

        # ----------------------------------------------------
        # Buscar fecha y hora alrededor del enlace.
        # ----------------------------------------------------

        container = link
        candidate_texts = []

        for _ in range(10):

            if container is None:
                break

            text = normalize(
                container.get_text(
                    " ",
                    strip=True,
                )
            )

            if text:
                candidate_texts.append(text)

            parsed = parse_date_time(
                text,
                year,
                month,
            )

            if parsed:
                break

            container = container.parent

        full_text = max(
            candidate_texts,
            key=len,
            default=title,
        )

        start = parse_date_time(
            full_text,
            year,
            month,
        )

        if start is None:
            continue

        end = parse_end_time(
            full_text,
            start,
        )

        category = classify(title)

        if category is None:
            continue

        # ----------------------------------------------------
        # Deduplicación.
        # ----------------------------------------------------

        key = (
            category,
            start.isoformat(),
            title.casefold(),
        )

        if key in seen:
            continue

        seen.add(key)

        location = ""
        description = ""

        # ----------------------------------------------------
        # Consultar ficha individual.
        # ----------------------------------------------------

        try:

            detail = session.get(
                event_url,
                timeout=REQUEST_TIMEOUT,
            )

            if detail.ok:

                detail_soup = BeautifulSoup(
                    detail.text,
                    "html.parser",
                )

                body = normalize(
                    detail_soup.get_text(
                        " ",
                        strip=True,
                    )
                )

                location_match = re.search(
                    r"Lugar:\s*(.+?)"
                    r"(?:Descripción:|Tipo de agenda:|$)",
                    body,
                    flags=re.I,
                )

                if location_match:

                    location = normalize(
                        location_match.group(1)
                    )

                description_match = re.search(
                    r"Descripción:\s*(.+?)"
                    r"(?:Tipo de agenda:|$)",
                    body,
                    flags=re.I,
                )

                if description_match:

                    description = normalize(
                        description_match.group(1)
                    )

        except requests.RequestException:
            pass

        results.append(
            {
                "title": title,
                "category": category,
                "start": start,
                "end": end,
                "url": event_url,
                "location": location,
                "description": description,
                "source": "Agenda Parlamentaria",
            }
        )

    print(
        f"{year}-{month:02d}: "
        f"{len(results)} eventos encontrados"
    )

    return results


# ============================================================
# BÚSQUEDA ESPECÍFICA DE SESIONES EXTRAORDINARIAS
# ============================================================

def extract_extraordinary_from_site(
    session: requests.Session,
    year: int,
    month: int,
) -> list[dict]:

    """
    Segunda búsqueda independiente.

    Se revisa la página de boletines para recuperar
    sesiones extraordinarias que puedan no aparecer
    como tarjetas normales de la agenda.
    """

    results = []
    seen = set()

    for page in range(0, 8):

        url = (
            f"{AGENDA_BASE_URL}/boletines"
            if page == 0
            else f"{AGENDA_BASE_URL}/boletines?page={page}"
        )

        try:

            response = session.get(
                url,
                timeout=REQUEST_TIMEOUT,
            )

            response.raise_for_status()

        except requests.RequestException:
            continue

        soup = BeautifulSoup(
            response.text,
            "html.parser",
        )

        links = soup.find_all(
            "a",
            href=True,
        )

        for link in links:

            href = link.get("href", "")

            if "/boletines/" not in href:
                continue

            article_url = urljoin(
                AGENDA_BASE_URL,
                href,
            )

            try:

                article = session.get(
                    article_url,
                    timeout=REQUEST_TIMEOUT,
                )

                if not article.ok:
                    continue

            except requests.RequestException:
                continue

            article_soup = BeautifulSoup(
                article.text,
                "html.parser",
            )

            body = normalize(
                article_soup.get_text(
                    " ",
                    strip=True,
                )
            )

            lower = body.casefold()

            if (
                "sesión extraordinaria"
                not in lower
                and
                "sesion extraordinaria"
                not in lower
            ):
                continue

            # IMPORTANTE:
            # No usamos automáticamente la fecha del boletín.
            # Primero intentamos encontrar una fecha de sesión.

            date_match = re.search(
                r"(?:sesión extraordinaria|"
                r"sesion extraordinaria)"
                r".{0,500}?"
                r"(\d{1,2})\s+"
                r"([A-Za-zÁÉÍÓÚáéíóúñÑ]+)"
                r"(?:\s+(\d{4}))?"
                r"(?:\s*(?:a las|-)\s*)?"
                r"(\d{1,2})?:?"
                r"(\d{2})?",
                body,
                flags=re.I,
            )

            if not date_match:
                continue

            day = int(date_match.group(1))
            month_text = date_match.group(2).casefold()

            session_month = MONTHS.get(
                month_text
            )

            if session_month != month:
                continue

            session_year = (
                int(date_match.group(3))
                if date_match.group(3)
                else year
            )

            if session_year != year:
                continue

            hour = (
                int(date_match.group(4))
                if date_match.group(4)
                else 0
            )

            minute = (
                int(date_match.group(5))
                if date_match.group(5)
                else 0
            )

            try:

                start = datetime(
                    session_year,
                    session_month,
                    day,
                    hour,
                    minute,
                    tzinfo=TZ,
                )

            except ValueError:
                continue

            key = (
                "Pleno",
                start.date().isoformat(),
            )

            if key in seen:
                continue

            seen.add(key)

            results.append(
                {
                    "title": "Sesión Extraordinaria",
                    "category": "Pleno",
                    "start": start,
                    "end": start + timedelta(hours=1),
                    "url": article_url,
                    "location": "",
                    "description": (
                        "Sesión extraordinaria del "
                        "Pleno identificada en "
                        "información oficial del "
                        "Congreso de Jalisco."
                    ),
                    "source": "Boletín oficial",
                }
            )

    return results


# ============================================================
# DEDUPLICACIÓN GLOBAL
# ============================================================

def deduplicate_events(
    events: list[dict],
) -> list[dict]:

    unique = {}

    for event in events:

        key = (
            event["category"],
            event["start"].date().isoformat(),
            normalize(
                event["title"]
            ).casefold(),
        )

        if key not in unique:

            unique[key] = event

            continue

        # Si tenemos dos versiones del mismo evento,
        # conservamos la que tenga más información.

        current = unique[key]

        current_score = sum(
            bool(current.get(field))
            for field in (
                "location",
                "description",
                "url",
            )
        )

        new_score = sum(
            bool(event.get(field))
            for field in (
                "location",
                "description",
                "url",
            )
        )

        if new_score > current_score:
            unique[key] = event

    return sorted(
        unique.values(),
        key=lambda item: item["start"],
    )


# ============================================================
# ICS
# ============================================================

def event_uid(event: dict) -> str:

    raw = (
        f"{event['category']}|"
        f"{event['start'].isoformat()}|"
        f"{event['title']}"
    )

    digest = hashlib.sha1(
        raw.encode("utf-8")
    ).hexdigest()

    return (
        f"{digest}@congreso-jalisco-calendar"
    )


def build_calendar(
    events: list[dict],
) -> Calendar:

    calendar = Calendar()

    calendar.add(
        "prodid",
        "-//Congreso Jalisco Calendar//"
        "ES//",
    )

    calendar.add(
        "version",
        "2.0",
    )

    calendar.add(
        "calscale",
        "GREGORIAN",
    )

    calendar.add(
        "X-WR-CALNAME",
        "Congreso de Jalisco",
    )

    calendar.add(
        "X-WR-TIMEZONE",
        TIMEZONE,
    )

    for item in events:

        event = Event()

        event.add(
            "uid",
            event_uid(item),
        )

        event.add(
            "dtstart",
            item["start"],
        )

        event.add(
            "dtend",
            item["end"],
        )

        event.add(
            "summary",
            item["title"],
        )

        event.add(
            "categories",
            item["category"],
        )

        if item.get("location"):
            event.add(
                "location",
                item["location"],
            )

        description_parts = []

        if item.get("description"):
            description_parts.append(
                item["description"]
            )

        description_parts.append(
            f"Fuente: {item.get('source', '')}"
        )

        if item.get("url"):
            description_parts.append(
                f"Enlace: {item['url']}"
            )

        event.add(
            "description",
            "\n".join(
                description_parts
            ),
        )

        if item.get("url"):
            event.add(
                "url",
                item["url"],
            )

        calendar.add_component(event)

    return calendar


def save_calendar(
    events: list[dict],
):

    calendar = build_calendar(events)

    output = Path(OUTPUT_FILE)

    output.write_bytes(
        calendar.to_ical()
    )

    print()
    print(
        f"Calendario generado: {output}"
    )
    print(
        f"Total de eventos: {len(events)}"
    )


# ============================================================
# RANGO DE MESES
# ============================================================

def generate_months():

    current = date(
        START_YEAR,
        START_MONTH,
        1,
    )

    for _ in range(
        LOOK_AHEAD_MONTHS
    ):
        yield (
            current.year,
            current.month,
        )

        current += relativedelta(
            months=1
        )


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "=========================================="
    )
    print(
        " Congreso de Jalisco - Calendario"
    )
    print(
        " Fuente oficial: Gaceta Parlamentaria"
    )
    print(
        "=========================================="
    )
    print()

    session = get_session()

    # --------------------------------------------------------
    # 1. Comprobar Gaceta.
    # --------------------------------------------------------

    gaceta_ok = verify_gaceta_calendar(
        session
    )

    if not gaceta_ok:

        print(
            "La Gaceta no está disponible."
        )

        print(
            "Se continuará con la Agenda "
            "Parlamentaria como fuente "
            "complementaria."
        )

    # --------------------------------------------------------
    # 2. Recopilar eventos.
    # --------------------------------------------------------

    all_events = []

    for year, month in generate_months():

        events = extract_events_from_month(
            session,
            year,
            month,
        )

        all_events.extend(events)

        extraordinary = (
            extract_extraordinary_from_site(
                session,
                year,
                month,
            )
        )

        all_events.extend(
            extraordinary
        )

    # --------------------------------------------------------
    # 3. Eliminar duplicados.
    # --------------------------------------------------------

    all_events = deduplicate_events(
        all_events
    )

    # --------------------------------------------------------
    # 4. Mostrar resumen.
    # --------------------------------------------------------

    pleno_count = sum(
        event["category"] == "Pleno"
        for event in all_events
    )

    commission_count = sum(
        event["category"]
        ==
        "Comisión de Higiene, Salud y "
        "Prevención de las Adicciones"
        for event in all_events
    )

    print()
    print(
        "=========================================="
    )
    print("RESUMEN")
    print(
        "=========================================="
    )

    print(
        f"Pleno: {pleno_count}"
    )

    print(
        f"Comisión: {commission_count}"
    )

    print(
        f"TOTAL: {len(all_events)}"
    )

    print()

    for event in all_events:

        print(
            event["start"].strftime(
                "%Y-%m-%d %H:%M"
            ),
            "|",
            event["category"],
            "|",
            event["title"],
        )

    # --------------------------------------------------------
    # 5. Generar ICS.
    # --------------------------------------------------------

    save_calendar(
        all_events
    )


if __name__ == "__main__":
    main()