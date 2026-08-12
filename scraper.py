from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dateutil.relativedelta import relativedelta
from icalendar import Calendar, Event
from playwright.sync_api import sync_playwright
from zoneinfo import ZoneInfo

from config import (
    GACETA_BASE_URL,
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

PLENO_NAME = "Pleno"
COMMISSION_NAME = (
    "Comisión de Higiene, Salud Pública "
    "y Prevención de las Adicciones"
)

MONTHS = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}


def normalize(text: str) -> str:
    return re.sub(
        r"\s+",
        " ",
        (text or "").replace("\xa0", " "),
    ).strip()


def is_target_commission(text: str) -> bool:
    """
    Devuelve True únicamente para la Comisión de Higiene,
    Salud Pública y Prevención de las Adicciones.

    Se aceptan las palabras distintivas que utiliza la Gaceta:
    SALUD, HIGIENE o ADICCIONES.
    """
    upper = normalize(text).upper()

    return (
        "SALUD" in upper
        or "HIGIENE" in upper
        or "ADICCIONES" in upper
    )


def classify(title: str) -> str | None:
    """
    Clasificación de respaldo cuando no existe una clase
    estructural suficiente en el calendario de la Gaceta.
    """

    text = normalize(title).casefold()

    if any(
        pattern.casefold() in text
        for pattern in PLENO_PATTERNS
    ) or "pleno" in text:
        return PLENO_NAME

    if any(
        pattern.casefold() in text
        for pattern in COMMISSION_PATTERNS
    ) or is_target_commission(text):
        return COMMISSION_NAME

    return None


def parse_time(text: str):
    match = re.search(
        r"\b([01]?\d|2[0-3]):([0-5]\d)\b",
        text or "",
    )

    if not match:
        return None

    return (
        int(match.group(1)),
        int(match.group(2)),
    )


def is_generic_calendar_label(line: str) -> bool:
    upper = normalize(line).upper()

    generic_labels = (
        "SESIÓN DE PLENO DEL CONGRESO",
        "SESION DE PLENO DEL CONGRESO",
        "REANUDACIÓN SESIÓN DE PLENO DEL CONGRESO",
        "REANUDACION SESION DE PLENO DEL CONGRESO",
        "SESIÓN DE COMISIÓN/COMITÉ",
        "SESION DE COMISION/COMITE",
        "EVENTO DE COMISIÓN/COMITÉ",
        "EVENTO DE COMISION/COMITE",
        "PLENO DEL CONGRESO Y COMISIÓN/COMITÉ",
        "PLENO DEL CONGRESO Y COMISION/COMITE",
    )

    return (
        upper in generic_labels
        or upper.startswith("SESIÓN DE COMISIÓN/COMITÉ")
        or upper.startswith("SESION DE COMISION/COMITE")
        or upper.startswith("EVENTO DE COMISIÓN/COMITÉ")
        or upper.startswith("EVENTO DE COMISION/COMITE")
    )


def is_numbered_session(line: str) -> bool:
    upper = normalize(line).upper()

    return bool(
        (
            "SESIÓN" in upper
            or "SESION" in upper
        )
        and re.search(
            r"\b(?:N[ÚU]M\.?|NUM\.?)\s*\d+",
            upper,
        )
    )


def parse_detail(
    body: str,
    category_hint: str | None = None,
):
    """
    Obtiene el nombre real de la sesión.

    Para Pleno:
      busca primero una sesión numerada y/o texto de Pleno.

    Para Comisión:
      SOLO acepta una sesión cuyo texto contenga SALUD,
      HIGIENE o ADICCIONES.

    Nunca devuelve una comisión distinta a la solicitada.
    """

    lines = [
        normalize(line)
        for line in body.splitlines()
        if normalize(line)
    ]

    # --------------------------------------------------------
    # SESIONES NUMERADAS
    # --------------------------------------------------------

    numbered = [
        line
        for line in lines
        if is_numbered_session(line)
        and not is_generic_calendar_label(line)
    ]

    if category_hint == PLENO_NAME:
        for line in numbered:
            upper = line.upper()

            if "PLENO" in upper:
                return line, parse_time(line)

        # Algunas sesiones de Pleno pueden aparecer con hora
        # y título específico sin repetir la palabra Pleno.
        # Si no hay otra comisión objetivo en la línea, aceptar
        # una sesión numerada que no sea de comisión.
        for line in numbered:
            upper = line.upper()

            if (
                "COMISIÓN" not in upper
                and "COMISION" not in upper
            ):
                return line, parse_time(line)

        return None, None

    if category_hint == COMMISSION_NAME:
        for line in numbered:
            if is_target_commission(line):
                return line, parse_time(line)

        # MUY IMPORTANTE:
        # No devolver otra comisión.
        return None, None

    # --------------------------------------------------------
    # SIN CATEGORÍA ESTRUCTURAL
    # --------------------------------------------------------

    for line in numbered:
        if is_target_commission(line):
            return line, parse_time(line)

    for line in numbered:
        if "PLENO" in line.upper():
            return line, parse_time(line)

    # --------------------------------------------------------
    # SESIONES NO NUMERADAS
    # --------------------------------------------------------

    candidates = [
        line
        for line in lines
        if (
            "SESIÓN" in line.upper()
            or "SESION" in line.upper()
        )
        and not is_generic_calendar_label(line)
    ]

    if category_hint == COMMISSION_NAME:
        for line in candidates:
            if is_target_commission(line):
                return line, parse_time(line)

        return None, None

    if category_hint == PLENO_NAME:
        for line in candidates:
            if "PLENO" in line.upper():
                return line, parse_time(line)

        return None, None

    for line in candidates:
        if is_target_commission(line):
            return line, parse_time(line)

    for line in candidates:
        if "PLENO" in line.upper():
            return line, parse_time(line)

    return None, None


def get_gaceta_events():
    events = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
        )

        page = browser.new_page(
            viewport={
                "width": 1600,
                "height": 1200,
            },
            locale="es-MX",
        )

        print("Abriendo Gaceta Parlamentaria...")

        page.goto(
            GACETA_BASE_URL,
            wait_until="networkidle",
            timeout=60000,
        )

        page.wait_for_timeout(3000)

        cells = page.locator(
            "#datepicker "
            "td[data-handler='selectDay']"
            "[data-year='2026']"
        )

        count = cells.count()

        print(
            "Días 2026 interactivos encontrados:",
            count,
        )

        for i in range(count):
            cell = cells.nth(i)

            try:
                month_raw = cell.get_attribute(
                    "data-month"
                )
                year_raw = cell.get_attribute(
                    "data-year"
                )

                if (
                    month_raw is None
                    or year_raw is None
                ):
                    continue

                month = int(month_raw)
                year = int(year_raw)

                day_match = re.search(
                    r"\b\d{1,2}\b",
                    normalize(cell.inner_text()),
                )

                if not day_match:
                    continue

                day = int(day_match.group())

                cls = (
                    cell.get_attribute("class")
                    or ""
                )

                title_attr = (
                    cell.get_attribute("title")
                    or ""
                )

                if (
                    "ui-datepicker-unselectable"
                    in cls
                ):
                    continue

                if year < START_YEAR:
                    continue

                if (
                    year == START_YEAR
                    and month + 1 < START_MONTH
                ):
                    continue

                if not any(
                    marker in cls
                    for marker in (
                        "sesple",
                        "rsesple",
                        "sescom",
                        "varios",
                    )
                ):
                    continue

                # La clase estructural indica qué tipo de
                # actividad existe ese día.
                if (
                    "sesple" in cls
                    or "rsesple" in cls
                ):
                    category_hint = PLENO_NAME

                elif "sescom" in cls:
                    category_hint = COMMISSION_NAME

                else:
                    category_hint = None

                print(
                    f"  Gaceta: "
                    f"{year}-{month + 1:02d}-{day:02d} "
                    f"class={cls.strip()} "
                    f"title={title_attr}"
                )

                cell.scroll_into_view_if_needed()

                cell.click(timeout=5000)

                page.wait_for_timeout(350)

                body = page.locator(
                    "body"
                ).inner_text(
                    timeout=10000
                )

                title, time_value = parse_detail(
                    body,
                    category_hint,
                )

                if not title:
                    print(
                        "    -> no corresponde a "
                        "Pleno o Comisión de Higiene/Salud"
                    )
                    continue

                # ------------------------------------------------
                # Categoría final.
                # ------------------------------------------------

                if category_hint == PLENO_NAME:
                    category = PLENO_NAME

                elif category_hint == COMMISSION_NAME:
                    # SOLO conservar la comisión objetivo.
                    if not is_target_commission(title):
                        print(
                            "    -> otra comisión ignorada:",
                            title,
                        )
                        continue

                    category = COMMISSION_NAME

                else:
                    category = classify(title)

                    if category is None:
                        print(
                            "    -> ignorado:",
                            title,
                        )
                        continue

                start = datetime(
                    year,
                    month + 1,
                    day,
                    *(time_value or (0, 0)),
                    tzinfo=TZ,
                )

                if time_value:
                    end = start + timedelta(
                        hours=1
                    )
                    all_day = False
                else:
                    end = start + timedelta(
                        days=1
                    )
                    all_day = True

                events.append(
                    {
                        "title": title,
                        "category": category,
                        "start": start,
                        "end": end,
                        "url": GACETA_BASE_URL,
                        "location": "",
                        "description": (
                            "Detectado directamente "
                            "en el Calendario de la "
                            "Gaceta Parlamentaria."
                        ),
                        "source": (
                            "Gaceta Parlamentaria"
                        ),
                        "all_day": all_day,
                    }
                )

                print(
                    f"    -> {category}: {title}"
                )

            except Exception as exc:
                print(
                    f"    -> error en celda {i}: {exc}"
                )

        browser.close()

    if not events:
        raise RuntimeError(
            "La Gaceta no produjo ningún "
            "evento objetivo. "
            "No se generará un calendario vacío."
        )

    return deduplicate(events)


def get_agenda_events():
    """
    La Agenda es únicamente complementaria.

    Si no encuentra información, no afecta la extracción
    principal de la Gaceta.
    """

    session = requests.Session()

    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept-Language": (
                "es-MX,es;q=0.9"
            ),
        }
    )

    results = []

    current = date(
        START_YEAR,
        START_MONTH,
        1,
    )

    for _ in range(
        LOOK_AHEAD_MONTHS
    ):
        year = current.year
        month = current.month

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
                f"  Agenda no disponible: {exc}"
            )

            current += relativedelta(
                months=1
            )

            continue

        soup = BeautifulSoup(
            response.text,
            "html.parser",
        )

        for link in soup.find_all(
            "a",
            href=True,
        ):
            title = normalize(
                link.get_text(
                    " ",
                    strip=True,
                )
            )

            category = classify(title)

            if not category:
                continue

            parent = link
            context = title

            for _ in range(8):
                if parent is None:
                    break

                text = normalize(
                    parent.get_text(
                        " ",
                        strip=True,
                    )
                )

                if len(text) > len(context):
                    context = text

                parent = parent.parent

            match = re.search(
                r"(\d{1,2})\s+"
                r"([A-Za-zÁÉÍÓÚáéíóúñÑ]+)\s+"
                r"(20\d{2})"
                r".{0,60}?"
                r"(\d{1,2}):(\d{2})",
                context,
                flags=re.I,
            )

            if not match:
                continue

            month_number = MONTHS.get(
                match.group(2).casefold()
            )

            if not month_number:
                continue

            try:
                start = datetime(
                    int(match.group(3)),
                    month_number,
                    int(match.group(1)),
                    int(match.group(4)),
                    int(match.group(5)),
                    tzinfo=TZ,
                )
            except ValueError:
                continue

            results.append(
                {
                    "title": title,
                    "category": category,
                    "start": start,
                    "end": start + timedelta(
                        hours=1
                    ),
                    "url": urljoin(
                        AGENDA_BASE_URL,
                        link["href"],
                    ),
                    "location": "",
                    "description": "",
                    "source": (
                        "Agenda Parlamentaria"
                    ),
                    "all_day": False,
                }
            )

        current += relativedelta(
            months=1
        )

    return results


def deduplicate(events):
    """
    No elimina dos eventos distintos de la misma categoría
    en la misma fecha.

    La clave incluye fecha, hora y título.
    """

    unique = {}

    for event in events:
        key = (
            event["category"],
            event["start"].isoformat(),
            normalize(event["title"]).casefold(),
        )

        if key not in unique:
            unique[key] = event
            continue

        old = unique[key]

        old_score = sum(
            bool(old.get(field))
            for field in (
                "url",
                "location",
                "description",
            )
        )

        new_score = sum(
            bool(event.get(field))
            for field in (
                "url",
                "location",
                "description",
            )
        )

        if new_score > old_score:
            unique[key] = event

    return sorted(
        unique.values(),
        key=lambda item: item["start"],
    )


def merge_events(
    gaceta_events,
    agenda_events,
):
    """
    La Gaceta manda.

    La Agenda únicamente complementa hora/enlace cuando
    la Gaceta no proporcionó hora.
    """

    result = []

    for gaceta in gaceta_events:
        candidates = [
            agenda
            for agenda in agenda_events
            if (
                agenda["category"]
                == gaceta["category"]
                and agenda["start"].date()
                == gaceta["start"].date()
            )
        ]

        if candidates:
            agenda = candidates[0]

            if gaceta["all_day"]:
                gaceta["start"] = agenda["start"]
                gaceta["end"] = agenda["end"]
                gaceta["all_day"] = False

            if agenda.get("url"):
                gaceta["url"] = agenda["url"]

            if agenda.get("location"):
                gaceta["location"] = agenda["location"]

            gaceta["description"] += (
                "\nComplementado con la "
                "Agenda Parlamentaria."
            )

        result.append(gaceta)

    return deduplicate(result)


def uid_for(event):
    raw = (
        f"{event['category']}|"
        f"{event['start'].isoformat()}|"
        f"{event['title']}"
    )

    return (
        hashlib.sha1(
            raw.encode("utf-8")
        ).hexdigest()
        + "@congreso-jalisco"
    )


def build_calendar(events):
    calendar = Calendar()

    calendar.add(
        "prodid",
        "-//Congreso Jalisco Calendar//ES//",
    )
    calendar.add("version", "2.0")
    calendar.add("calscale", "GREGORIAN")
    calendar.add(
        "X-WR-CALNAME",
        "Congreso de Jalisco",
    )
    calendar.add(
        "X-WR-TIMEZONE",
        TIMEZONE,
    )

    for event_data in events:
        event = Event()

        event.add(
            "uid",
            uid_for(event_data),
        )

        if event_data["all_day"]:
            event.add(
                "dtstart",
                event_data["start"].date(),
            )
            event.add(
                "dtend",
                event_data["end"].date(),
            )
        else:
            event.add(
                "dtstart",
                event_data["start"],
            )
            event.add(
                "dtend",
                event_data["end"],
            )

        event.add(
            "summary",
            event_data["title"],
        )

        event.add(
            "categories",
            event_data["category"],
        )

        description = event_data.get(
            "description",
            "",
        )

        if event_data.get("url"):
            description += (
                "\nFuente: "
                + event_data["url"]
            )

            event.add(
                "url",
                event_data["url"],
            )

        event.add(
            "description",
            description,
        )

        if event_data.get("location"):
            event.add(
                "location",
                event_data["location"],
            )

        calendar.add_component(event)

    return calendar


def main():
    print("=" * 60)
    print(
        "CONGRESO DE JALISCO - CALENDARIO"
    )
    print(
        "FUENTE PRINCIPAL: GACETA PARLAMENTARIA"
    )
    print("=" * 60)

    gaceta_events = get_gaceta_events()

    print(
        f"\nGaceta: {len(gaceta_events)} "
        "eventos objetivo"
    )

    agenda_events = get_agenda_events()

    print(
        f"Agenda complementaria: "
        f"{len(agenda_events)} eventos"
    )

    events = merge_events(
        gaceta_events,
        agenda_events,
    )

    pleno = sum(
        event["category"] == PLENO_NAME
        for event in events
    )

    commission = sum(
        event["category"] == COMMISSION_NAME
        for event in events
    )

    print("\nRESUMEN")
    print("=" * 40)
    print(f"Pleno: {pleno}")
    print(f"Comisión: {commission}")
    print(f"TOTAL: {len(events)}")
    print("=" * 40)

    for event in events:
        print(
            event["start"].strftime(
                "%Y-%m-%d %H:%M"
            ),
            "|",
            event["category"],
            "|",
            event["title"],
        )

    calendar = build_calendar(events)

    Path(
        OUTPUT_FILE
    ).write_bytes(
        calendar.to_ical()
    )

    print(
        f"\nCalendario generado: "
        f"{OUTPUT_FILE}"
    )


if __name__ == "__main__":
    main()