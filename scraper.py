from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta
from pathlib import Path

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
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "octubre": 10, "noviembre": 11,
    "diciembre": 12,
}

TIME_RE = re.compile(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b")


def normalize(text: str) -> str:
    return re.sub(
        r"\s+",
        " ",
        (text or "").replace("\xa0", " "),
    ).strip()


def target_commission(text: str) -> bool:
    upper = normalize(text).upper()
    return (
        "SALUD" in upper
        or "HIGIENE" in upper
        or "ADICCIONES" in upper
    )


def parse_hhmm(text: str):
    match = TIME_RE.search(text or "")
    if not match:
        return None
    h, m = match.group().split(":")
    return int(h), int(m)


def extract_session_lines(text: str) -> list[str]:
    """
    Extrae únicamente líneas que representan sesiones reales.
    No analiza la leyenda global del calendario.
    """
    lines = []

    for raw in (text or "").splitlines():
        line = normalize(raw)

        if not line:
            continue

        if re.match(
            r"^(?:[01]?\d|2[0-3]):[0-5]\d\s*-\s*SESI[ÓO]N\b",
            line,
            re.IGNORECASE,
        ):
            lines.append(line)
            continue

        if re.match(
            r"^SESI[ÓO]N\s+(?:NUM\.?\s*)?\d+\b",
            line,
            re.IGNORECASE,
        ):
            lines.append(line)

    return lines


def classify_session(title: str):
    upper = normalize(title).upper()

    if "PLENO" in upper:
        return PLENO_NAME

    if target_commission(upper):
        return COMMISSION_NAME

    return None


def extract_sessions_from_detail(detail_text: str):
    candidates = extract_session_lines(detail_text)
    results = []

    for line in candidates:
        match = re.match(
            r"^(?P<time>(?:[01]?\d|2[0-3]):[0-5]\d)\s*-\s*"
            r"(?P<title>.+)$",
            line,
            re.IGNORECASE,
        )

        if match:
            time_text = match.group("time")
            title = normalize(match.group("title"))
        else:
            time_text = None
            title = normalize(line)

        category = classify_session(title)

        # Solo Pleno o la comisión de Higiene/Salud.
        if category is None:
            continue

        # Nunca aceptar las leyendas genéricas.
        if title.upper().startswith(
            (
                "SESIÓN DE COMISIÓN",
                "SESION DE COMISION",
                "EVENTO DE COMISIÓN",
                "EVENTO DE COMISION",
            )
        ):
            continue

        results.append(
            {
                "title": title,
                "category": category,
                "time": time_text,
            }
        )

    return results


def detail_text_after_click(page):
    """
    Busca primero contenedores de detalle visibles.
    Si no existe un selector estable, usa body como último
    recurso, pero extract_session_lines() solo aceptará líneas
    que tengan formato real de sesión.
    """
    selectors = [
        "#eventos",
        "#evento",
        "#detalle",
        "#detalles",
        ".detalle",
        ".detalles",
        ".event-detail",
        ".evento",
        ".session-detail",
        ".ui-dialog",
        ".modal",
    ]

    chunks = []

    for selector in selectors:
        try:
            loc = page.locator(selector)

            for i in range(min(loc.count(), 10)):
                node = loc.nth(i)

                if not node.is_visible():
                    continue

                txt = node.inner_text(timeout=1500)

                if txt and (
                    "SESIÓN" in txt.upper()
                    or "SESION" in txt.upper()
                ):
                    chunks.append(txt)
        except Exception:
            pass

    if chunks:
        return min(
            (normalize(c) for c in chunks if c),
            key=len,
            default="",
        )

    return page.locator("body").inner_text(
        timeout=10000
    )


def get_gaceta_events():
    events = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

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

        page.wait_for_timeout(2500)

        # IMPORTANTE:
        # Ya no dependemos de sesple/sescom para decidir qué fechas
        # revisar. Recorremos todas las fechas disponibles desde
        # julio de 2026.
        cells = page.locator(
            "#datepicker td[data-handler='selectDay']"
        )

        raw_dates = []

        for i in range(cells.count()):
            cell = cells.nth(i)

            try:
                year_raw = cell.get_attribute("data-year")
                month_raw = cell.get_attribute("data-month")

                if year_raw is None or month_raw is None:
                    continue

                year = int(year_raw)
                month = int(month_raw) + 1

                if year < START_YEAR:
                    continue

                if (
                    year == START_YEAR
                    and month < START_MONTH
                ):
                    continue

                link = cell.locator("a").first

                if link.count() == 0:
                    continue

                day_text = normalize(link.inner_text())

                if not day_text.isdigit():
                    continue

                raw_dates.append(
                    (
                        year,
                        month,
                        int(day_text),
                    )
                )

            except Exception:
                continue

        dates = sorted(set(raw_dates))

        print(
            f"Fechas del calendario desde "
            f"{START_YEAR}-{START_MONTH:02d}: "
            f"{len(dates)}"
        )

        for year, month, day in dates:
            print(
                f"Consultando Gaceta: "
                f"{year}-{month:02d}-{day:02d}"
            )

            try:
                selector = (
                    "#datepicker td[data-handler='selectDay']"
                    f"[data-year='{year}']"
                    f"[data-month='{month - 1}']"
                    " a"
                )

                links = page.locator(selector)
                clicked = False

                for j in range(links.count()):
                    link = links.nth(j)

                    if normalize(link.inner_text()) != str(day):
                        continue

                    link.scroll_into_view_if_needed()
                    link.click(timeout=8000)
                    clicked = True
                    break

                if not clicked:
                    print("  -> no se pudo seleccionar la fecha")
                    continue

                page.wait_for_timeout(450)

                detail = detail_text_after_click(page)
                sessions = extract_sessions_from_detail(detail)

                if not sessions:
                    print("  -> sin sesiones objetivo")
                    continue

                for session in sessions:
                    time_value = parse_hhmm(
                        session["time"] or ""
                    )

                    # Para casos como la sesión del 13 de agosto,
                    # la Gaceta muestra el nombre en una línea y la
                    # hora en otra. Si solo hay una hora en el detalle,
                    # la asociamos a esa sesión.
                    if time_value is None:
                        nearby_times = TIME_RE.findall(detail)

                        if len(nearby_times) == 1:
                            time_value = parse_hhmm(
                                nearby_times[0]
                            )

                    h, m = time_value or (0, 0)

                    start = datetime(
                        year,
                        month,
                        day,
                        h,
                        m,
                        tzinfo=TZ,
                    )

                    all_day = time_value is None

                    end = (
                        start + timedelta(days=1)
                        if all_day
                        else start + timedelta(hours=1)
                    )

                    events.append(
                        {
                            "title": session["title"],
                            "category": session["category"],
                            "start": start,
                            "end": end,
                            "url": GACETA_BASE_URL,
                            "location": "",
                            "description": (
                                "Fuente: Gaceta Parlamentaria "
                                "del Congreso del Estado de Jalisco."
                            ),
                            "source": "Gaceta Parlamentaria",
                            "all_day": all_day,
                        }
                    )

                    print(
                        "  ->",
                        session["category"],
                        "|",
                        start.strftime("%H:%M"),
                        "|",
                        session["title"],
                    )

            except Exception as exc:
                print(
                    f"  -> error en "
                    f"{year}-{month:02d}-{day:02d}: {exc}"
                )

        browser.close()

    events = deduplicate(events)

    if not events:
        raise RuntimeError(
            "La Gaceta no produjo ningún evento objetivo. "
            "No se generará un calendario vacío."
        )

    return events


def get_agenda_events():
    """
    Agenda secundaria. Solo complementa información faltante
    y nunca crea una comisión que no sea la de Higiene/Salud.
    """
    session = requests.Session()

    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept-Language": "es-MX,es;q=0.9",
        }
    )

    results = []

    current = date(
        START_YEAR,
        START_MONTH,
        1,
    )

    for _ in range(LOOK_AHEAD_MONTHS):
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

            upper = title.upper()

            if "PLENO" in upper:
                category = PLENO_NAME

            elif target_commission(upper):
                category = COMMISSION_NAME

            else:
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
                r".{0,100}?"
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
                    "end": start + timedelta(hours=1),
                    "url": requests.compat.urljoin(
                        AGENDA_BASE_URL,
                        link["href"],
                    ),
                    "location": "",
                    "description": (
                        "Fuente complementaria: "
                        "Agenda Parlamentaria."
                    ),
                    "source": "Agenda Parlamentaria",
                    "all_day": False,
                }
            )

        current += relativedelta(
            months=1
        )

    return deduplicate(results)


def merge_events(gaceta_events, agenda_events):
    """
    La Gaceta es la fuente principal.
    La Agenda solo rellena la hora si Gaceta no la dio.
    """
    result = []

    for gaceta in gaceta_events:
        same_day = [
            a
            for a in agenda_events
            if (
                a["category"] == gaceta["category"]
                and a["start"].date()
                == gaceta["start"].date()
            )
        ]

        matching = [
            a
            for a in same_day
            if (
                normalize(gaceta["title"]).casefold()
                in normalize(a["title"]).casefold()
                or normalize(a["title"]).casefold()
                in normalize(gaceta["title"]).casefold()
            )
        ]

        candidate = (
            matching[0]
            if matching
            else (
                same_day[0]
                if len(same_day) == 1
                else None
            )
        )

        if candidate and gaceta["all_day"]:
            gaceta["start"] = candidate["start"]
            gaceta["end"] = candidate["end"]
            gaceta["all_day"] = False

        result.append(gaceta)

    return deduplicate(result)


def deduplicate(events):
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
            bool(old.get(k))
            for k in (
                "url",
                "location",
                "description",
            )
        )

        new_score = sum(
            bool(event.get(k))
            for k in (
                "url",
                "location",
                "description",
            )
        )

        if new_score > old_score:
            unique[key] = event

    return sorted(
        unique.values(),
        key=lambda e: e["start"],
    )


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

    for data in events:
        event = Event()

        event.add(
            "uid",
            uid_for(data),
        )

        if data["all_day"]:
            event.add(
                "dtstart",
                data["start"].date(),
            )

            event.add(
                "dtend",
                data["end"].date(),
            )

        else:
            event.add(
                "dtstart",
                data["start"],
            )

            event.add(
                "dtend",
                data["end"],
            )

        event.add(
            "summary",
            data["title"],
        )

        event.add(
            "categories",
            data["category"],
        )

        description = data.get(
            "description",
            "",
        )

        if data.get("url"):
            event.add(
                "url",
                data["url"],
            )

            description += (
                "\nFuente: "
                + data["url"]
            )

        event.add(
            "description",
            description,
        )

        if data.get("location"):
            event.add(
                "location",
                data["location"],
            )

        calendar.add_component(event)

    return calendar


def main():
    print("=" * 70)
    print("CONGRESO DE JALISCO - CALENDARIO")
    print("FUENTE PRINCIPAL: GACETA PARLAMENTARIA")
    print("=" * 70)

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
        e["category"] == PLENO_NAME
        for e in events
    )

    commission = sum(
        e["category"] == COMMISSION_NAME
        for e in events
    )

    print("\nRESUMEN")
    print("=" * 50)
    print(f"Pleno: {pleno}")
    print(f"Comisión: {commission}")
    print(f"TOTAL: {len(events)}")
    print("=" * 50)

    for e in events:
        print(
            e["start"].strftime(
                "%Y-%m-%d %H:%M"
            ),
            "|",
            e["category"],
            "|",
            e["title"],
        )

    calendar = build_calendar(events)

    Path(OUTPUT_FILE).write_bytes(
        calendar.to_ical()
    )

    print(
        f"\nCalendario generado: "
        f"{OUTPUT_FILE}"
    )

    print(
        f"Total de eventos: "
        f"{len(events)}"
    )


if __name__ == "__main__":
    main()
