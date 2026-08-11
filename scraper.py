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
    BASE_URL,
    MONTH_URL,
    OUTPUT_FILE,
    TIMEZONE,
    COMMISSION_PATTERNS,
    PLENO_PATTERNS,
    LOOK_AHEAD_MONTHS,
    REQUEST_TIMEOUT,
    USER_AGENT,
)

TZ = ZoneInfo(TIMEZONE)

# ------------------------------------------------------------
# CONFIGURACIÓN
# ------------------------------------------------------------

# El calendario comenzará en julio de 2026.
START_YEAR = 2026
START_MONTH = 7

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
    "septiembre": 9,
    "sep": 9,
    "setiembre": 9,
    "octubre": 10,
    "oct": 10,
    "noviembre": 11,
    "nov": 11,
    "diciembre": 12,
    "dic": 12,
}


# ------------------------------------------------------------
# UTILIDADES
# ------------------------------------------------------------

def normalize(text: str) -> str:
    text = html.unescape(text or "")
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def get_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "es-MX,es;q=0.9,en;q=0.8",
    })
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


# ------------------------------------------------------------
# CLASIFICACIÓN
# ------------------------------------------------------------

def is_pleno(title: str) -> bool:
    t = normalize(title).casefold()

    patrones = (
        "sesión ordinaria",
        "sesion ordinaria",
        "sesión extraordinaria",
        "sesion extraordinaria",
        "sesión solemne",
        "sesion solemne",
    )

    return any(pattern in t for pattern in patrones)


def is_commission(title: str) -> bool:
    t = normalize(title).casefold()

    # Denominaciones que han aparecido en la agenda.
    patrones = (
        "comisión de salud",
        "comision de salud",
        "comisión de higiene y salud",
        "comision de higiene y salud",
        "comisión de higiene, salud",
        "comision de higiene, salud",
        "comisión de higiene salud",
        "comision de higiene salud",
        "comisión de salud e higiene",
        "comision de salud e higiene",
        "comisión de higiene, salud pública",
        "comision de higiene, salud publica",
        "prevención de las adicciones",
        "prevencion de las adicciones",
    )

    return any(pattern in t for pattern in patrones)


def is_target(title: str) -> bool:
    return is_pleno(title) or is_commission(title)


def classify(title: str) -> str | None:
    if is_pleno(title):
        return "Pleno"

    if is_commission(title):
        return "Comisión de Higiene, Salud y Prevención de las Adicciones"

    return None


# ------------------------------------------------------------
# FECHAS Y HORAS
# ------------------------------------------------------------

def parse_date_time(
    text: str,
    default_year: int,
    default_month: int,
):
    text = normalize(text)

    # Ejemplos:
    # 9 Jul 2026 - 09:00
    # 28 Mayo 2026 - 09:00 a 09:45
    # 15 Julio 2026 - 10:00
    pattern_month = re.compile(
        r"(\d{1,2})\s+"
        r"([A-Za-zÁÉÍÓÚáéíóúñÑ]+)\s+"
        r"(\d{4})"
        r"(?:\s*-\s*|\s+)"
        r"(\d{1,2}):(\d{2})",
        flags=re.I,
    )

    match = pattern_month.search(text)

    if match:
        day = int(match.group(1))
        month_text = match.group(2).casefold()
        year = int(match.group(3))
        hour = int(match.group(4))
        minute = int(match.group(5))

        month = MONTHS.get(month_text)

        if month is None:
            month = default_month

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

    # Formato 15/07/2026 - 10:00
    pattern_numeric = re.compile(
        r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})"
        r"\s*(?:-|a las)?\s*"
        r"(\d{1,2}):(\d{2})",
        flags=re.I,
    )

    match = pattern_numeric.search(text)

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


def parse_end_time(text: str, start: datetime):
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


# ------------------------------------------------------------
# EVENTOS DE LA AGENDA PARLAMENTARIA
# ------------------------------------------------------------

def extract_events_from_month(
    session: requests.Session,
    year: int,
    month: int,
) -> list[dict]:

    url = MONTH_URL.format(
        year=year,
        month=month,
    )

    response = session.get(
        url,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    results = []
    seen = set()

    # Primero buscamos enlaces de agenda.
    candidates = soup.select(
        'a[href*="/agenda/"], '
        'a[href*="/agenda-parlamentaria/"]'
    )

    # Si la estructura del sitio cambia, usamos todos los enlaces
    # cuyo texto sea un evento objetivo.
    if not candidates:
        candidates = soup.find_all("a")

    for link in candidates:

        title = normalize(
            link.get_text(" ", strip=True)
        )

        if not title:
            continue

        if not is_target(title):
            continue

        href = link.get("href")

        event_url = (
            urljoin(BASE_URL, href)
            if href
            else url
        )

        # Buscamos fecha/hora en el enlace y sus padres.
        container = link
        candidate_texts = []

        for _ in range(8):

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

        key = (
            title.casefold(),
            start.isoformat(),
            event_url,
        )

        if key in seen:
            continue

        seen.add(key)

        location = ""
        description = ""

        # Intentamos consultar la ficha individual.
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

        results.append({
            "title": title,
            "category": category,
            "start": start,
            "end": end,
            "url": event_url,
            "location": location,
            "description": description,
        })

    return results


# ------------------------------------------------------------
# BOLETINES OFICIALES
# ------------------------------------------------------------

def extract_extraordinary_sessions_from_bulletins(
    session: requests.Session,
    year: int,
    month: int,
) -> list[dict]:
    """
    Busca sesiones extraordinarias en los boletines oficiales.

    Esto sirve como segunda fuente porque algunas sesiones del Pleno
    pueden no aparecer en la Agenda Parlamentaria.
    """

    results = []
    seen = set()

    # Revisamos varias páginas de boletines.
    for page in range(0, 8):

        if page == 0:
            url = f"{BASE_URL}/boletines"
        else:
            url = f"{BASE_URL}/boletines?page={page}"

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

        # Buscamos enlaces a notas individuales.
        links = soup.find_all("a", href=True)

        page_found = False

        for link in links:

            href = link.get("href", "")
            text = normalize(
                link.get_text(" ", strip=True)
            )

            if not href:
                continue

            if "/boletines/" not in href:
                continue

            if href.rstrip("/") == "/boletines":
                continue

            article_url = urljoin(
                BASE_URL,
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

            lower_body = body.casefold()

            if (
                "sesión extraordinaria"
                not in lower_body
                and "sesion extraordinaria"
                not in lower_body
            ):
                continue

            # Buscamos la fecha del boletín.
            published_date = parse_date_only(
                body,
                year,
                month,
            )

            if published_date is None:
                continue

            if (
                published_date.year != year
                or published_date.month != month
            ):
                continue

            page_found = True

            # Intentamos obtener hora si aparece.
            start = parse_date_time(
                body,
                year,
                month,
            )

            if start is None:
                # Si no hay hora publicada, evento de día completo.
                start = datetime(
                    published_date.year,
                    published_date.month,
                    published_date.day,
                    tzinfo=TZ,
                )

                end = start + timedelta(days=1)

            else:
                end = parse_end_time(
                    body,
                    start,
                )

            title = "Sesión Extraordinaria"

            key = (
                title.casefold(),
                start.date().isoformat(),
            )

            if key in seen:
                continue

            seen.add(key)

            description = (
                "Sesión extraordinaria del Pleno "
                "identificada en información oficial "
                "del Congreso de Jalisco."
            )

            results.append({
                "title": title,
                "category": "Pleno",
                "start": start,
                "end": end,
                "url": article_url,
                "location": "",
                "description": description,
            })

        # Si ya no encontramos ningún boletín en la página,
        # seguimos algunas páginas más por seguridad.
        if not page_found and page >= 3:
            break

    return results


# ------------------------------------------------------------
# DEDUPLICACIÓN
# ------------------------------------------------------------

def event_key(item: dict):
    return (
        item["title"].casefold(),
        item["start"].date().isoformat(),
        item["category"],
    )


def deduplicate_events(
    items: list[dict],
) -> list[dict]:

    unique = {}

    for item in items:

        key = event_key(item)

        # Si ya existe, preferimos el que tenga hora concreta
        # frente a uno de día completo.
        if key not in unique:
            unique[key] = item
            continue

        current = unique[key]

        current_has_time = (
            current["start"].hour != 0
            or current["start"].minute != 0
        )

        new_has_time = (
            item["start"].hour != 0
            or item["start"].minute != 0
        )

        if new_has_time and not current_has_time:
            unique[key] = item

    return sorted(
        unique.values(),
        key=lambda item: item["start"],
    )


# ------------------------------------------------------------
# CALENDARIO ICS
# ------------------------------------------------------------

def event_uid(item: dict) -> str:

    raw = (
        f'{item["title"]}|'
        f'{item["start"].isoformat()}|'
        f'{item["url"]}'
    )

    digest = hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:24]

    return (
        f"{digest}@"
        "congreso-jalisco-calendar"
    )


def build_calendar(
    items: list[dict],
) -> Calendar:

    calendar = Calendar()

    calendar.add(
        "prodid",
        "-//Congreso de Jalisco Calendar//ES//",
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
        "method",
        "PUBLISH",
    )

    calendar.add(
        "x-wr-calname",
        "Congreso de Jalisco",
    )

    calendar.add(
        "x-wr-timezone",
        TIMEZONE,
    )

    calendar.add(
        "refresh-interval;value=duration",
        "PT12H",
    )

    calendar.add(
        "x-published-ttl",
        "PT12H",
    )

    for item in items:

        event = Event()

        event.add(
            "uid",
            event_uid(item),
        )

        event.add(
            "summary",
            item["title"],
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
            "dtstamp",
            datetime.now(TZ),
        )

        event.add(
            "categories",
            item["category"],
        )

        if item["url"]:
            event.add(
                "url",
                item["url"],
            )

        description = item["description"]

        if item["url"]:
            description = (
                f"{description}\n\n"
                f"Fuente oficial:\n"
                f"{item['url']}"
            ).strip()

        if description:
            event.add(
                "description",
                description,
            )

        if item["location"]:
            event.add(
                "location",
                item["location"],
            )

        calendar.add_component(event)

    return calendar


# ------------------------------------------------------------
# MESES A REVISAR
# ------------------------------------------------------------

def months_to_scan():

    start = date(
        START_YEAR,
        START_MONTH,
        1,
    )

    for i in range(
        LOOK_AHEAD_MONTHS + 1
    ):

        current = start + relativedelta(
            months=i
        )

        yield (
            current.year,
            current.month,
        )


# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------

def main():

    session = get_session()

    all_events = []
    failures = []

    print(
        "Inicio de revisión:",
        f"{START_YEAR}-{START_MONTH:02d}",
    )

    print(
        "Meses a revisar:",
        LOOK_AHEAD_MONTHS + 1,
    )

    for year, month in months_to_scan():

        print(
            f"\nConsultando "
            f"{year}-{month:02d}..."
        )

        # ----------------------------------------------------
        # FUENTE 1: AGENDA PARLAMENTARIA
        # ----------------------------------------------------

        try:

            agenda_events = (
                extract_events_from_month(
                    session,
                    year,
                    month,
                )
            )

            print(
                f"{year}-{month:02d}: "
                f"{len(agenda_events)} "
                f"eventos de agenda"
            )

            all_events.extend(
                agenda_events
            )

        except Exception as exc:

            message = (
                f"Agenda "
                f"{year}-{month:02d}: "
                f"{exc}"
            )

            failures.append(message)

            print(
                "ERROR:",
                message,
            )

        # ----------------------------------------------------
        # FUENTE 2: BOLETINES
        # ----------------------------------------------------

        try:

            bulletin_events = (
                extract_extraordinary_sessions_from_bulletins(
                    session,
                    year,
                    month,
                )
            )

            if bulletin_events:

                print(
                    f"{year}-{month:02d}: "
                    f"{len(bulletin_events)} "
                    "sesiones extraordinarias "
                    "adicionales"
                )

                all_events.extend(
                    bulletin_events
                )

            else:

                print(
                    f"{year}-{month:02d}: "
                    "0 sesiones extraordinarias "
                    "adicionales"
                )

        except Exception as exc:

            message = (
                f"Boletines "
                f"{year}-{month:02d}: "
                f"{exc}"
            )

            failures.append(message)

            print(
                "ERROR:",
                message,
            )

    # --------------------------------------------------------
    # DEDUPLICAR
    # --------------------------------------------------------

    unique = deduplicate_events(
        all_events
    )

    print(
        "\n================================"
    )

    print(
        f"Eventos finales: {len(unique)}"
    )

    print(
        "================================"
    )

    for item in unique:

        print(
            item["start"].strftime(
                "%Y-%m-%d %H:%M"
            ),
            "|",
            item["category"],
            "|",
            item["title"],
        )

    # --------------------------------------------------------
    # AVISOS
    # --------------------------------------------------------

    if failures:

        print("\nAvisos:")

        for failure in failures:
            print(
                " -",
                failure,
            )

    # --------------------------------------------------------
    # NO PUBLICAR CALENDARIO VACÍO
    # --------------------------------------------------------

    if not unique:

        print(
            "\nSin eventos publicados "
            "en el periodo consultado."
        )

        print(
            "No se modifica calendario.ics."
        )

        return 0

    # --------------------------------------------------------
    # GENERAR ICS
    # --------------------------------------------------------

    calendar = build_calendar(
        unique
    )

    Path(OUTPUT_FILE).write_bytes(
        calendar.to_ical()
    )

    print(
        f"\nCalendario generado: "
        f"{OUTPUT_FILE}"
    )

    print(
        f"Eventos: {len(unique)}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )