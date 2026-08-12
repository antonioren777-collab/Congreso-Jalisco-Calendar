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


# ============================================================
# UTILIDADES
# ============================================================

def normalize(text: str) -> str:
    text = (text or "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def normalize_key(text: str) -> str:
    return normalize(text).casefold()


def classify(title: str) -> str | None:
    text = normalize_key(title)

    if any(
        pattern.casefold() in text
        for pattern in PLENO_PATTERNS
    ):
        return "Pleno"

    if any(
        pattern.casefold() in text
        for pattern in COMMISSION_PATTERNS
    ):
        return (
            "Comisión de Higiene, Salud y "
            "Prevención de las Adicciones"
        )

    # Variante que puede aparecer en la Gaceta
    # sin la palabra "comisión".
    if (
        "higiene" in text
        and "salud" in text
        and "adicciones" in text
    ):
        return (
            "Comisión de Higiene, Salud y "
            "Prevención de las Adicciones"
        )

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


def event_datetime(
    year: int,
    month: int,
    day: int,
    time_value=None,
):
    if time_value:
        hour, minute = time_value
    else:
        hour, minute = 0, 0

    return datetime(
        year,
        month,
        day,
        hour,
        minute,
        tzinfo=TZ,
    )


# ============================================================
# DETECCIÓN DEL CALENDARIO DE LA GACETA
# ============================================================

def inspect_interactive_calendar(page):
    """
    Inspecciona TODOS los elementos interactivos de la página.

    No presupone:
      - nombres de meses en HTML
      - clases CSS concretas
      - estructura concreta del calendario

    Devuelve elementos que pueden representar días del calendario.
    """

    return page.evaluate(
        """
        () => {
            const norm = s =>
                (s || '').replace(/\\s+/g, ' ').trim();

            const elements = [
                ...document.querySelectorAll(
                    'a, area, button, [role="button"], ' +
                    '[onclick], [data-date], [data-day], ' +
                    '[data-fecha], [data-id]'
                )
            ];

            return elements.map((el, index) => {

                const r = el.getBoundingClientRect();
                const cs = getComputedStyle(el);

                return {
                    index,
                    tag: el.tagName,
                    text: norm(el.innerText || el.textContent),
                    title: el.getAttribute('title') || '',
                    alt: el.getAttribute('alt') || '',
                    aria: el.getAttribute('aria-label') || '',
                    href: el.getAttribute('href') || '',
                    onclick: el.getAttribute('onclick') || '',
                    dataDate: el.getAttribute('data-date') || '',
                    dataDay: el.getAttribute('data-day') || '',
                    dataFecha: el.getAttribute('data-fecha') || '',
                    dataId: el.getAttribute('data-id') || '',
                    className: String(el.className || ''),
                    background: cs.backgroundColor,
                    color: cs.color,
                    x: r.left + r.width / 2,
                    y: r.top + r.height / 2,
                    width: r.width,
                    height: r.height
                };
            });
        }
        """
    )


def is_session_color(background: str) -> bool:
    """
    El calendario de la Gaceta utiliza colores para distinguir
    tipos de sesión.

    Se excluyen blancos/grises muy claros.
    """

    match = re.search(
        r"rgba?\(\s*(\d+),\s*(\d+),\s*(\d+)",
        background or "",
    )

    if not match:
        return False

    r, g, b = map(
        int,
        match.groups(),
    )

    # Blanco / gris claro.
    if r > 225 and g > 225 and b > 225:
        return False

    # Negro del texto, si aparece como fondo de un elemento
    # muy pequeño, no se considera suficiente por sí solo.
    if (
        r < 30
        and g < 30
        and b < 30
    ):
        return False

    return True


def extract_date_from_attributes(item: dict):
    """
    Intenta obtener una fecha de href, onclick, data-*,
    title, aria-label, etc.
    """

    fields = [
        item.get("dataDate", ""),
        item.get("dataFecha", ""),
        item.get("href", ""),
        item.get("onclick", ""),
        item.get("title", ""),
        item.get("aria", ""),
        item.get("alt", ""),
        item.get("text", ""),
    ]

    combined = " ".join(
        x for x in fields if x
    )

    # YYYY-MM-DD
    match = re.search(
        r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})",
        combined,
    )

    if match:
        try:
            return date(
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
            )
        except ValueError:
            pass

    # DD/MM/YYYY
    match = re.search(
        r"\b(\d{1,2})[-/](\d{1,2})[-/](20\d{2})\b",
        combined,
    )

    if match:
        try:
            return date(
                int(match.group(3)),
                int(match.group(2)),
                int(match.group(1)),
            )
        except ValueError:
            pass

    # Fecha en español
    month_pattern = (
        "enero|febrero|marzo|abril|mayo|junio|"
        "julio|agosto|septiembre|octubre|"
        "noviembre|diciembre"
    )

    match = re.search(
        rf"\b(\d{{1,2}})\s+de?\s*"
        rf"({month_pattern})"
        rf"(?:\s+de?\s*(20\d{{2}}))?\b",
        combined,
        flags=re.I,
    )

    if match:
        day = int(match.group(1))
        month = MONTHS[
            match.group(2).casefold()
        ]
        year = (
            int(match.group(3))
            if match.group(3)
            else START_YEAR
        )

        try:
            return date(
                year,
                month,
                day,
            )
        except ValueError:
            pass

    return None


# ============================================================
# TEXTO DEL DETALLE DE LA GACETA
# ============================================================

def find_session_title(text: str):
    lines = [
        normalize(line)
        for line in text.splitlines()
        if normalize(line)
    ]

    candidates = []

    for line in lines:

        upper = line.upper()

        if (
            "SESIÓN" not in upper
            and "SESION" not in upper
        ):
            continue

        if (
            "PLENO" not in upper
            and "COMISIÓN" not in upper
            and "COMISION" not in upper
        ):
            continue

        # Ignorar solamente las leyendas genéricas.
        generic = (
            "SESIÓN DE PLENO DEL CONGRESO",
            "SESION DE PLENO DEL CONGRESO",
            "SESIÓN DE COMISIÓN/COMITÉ",
            "SESION DE COMISION/COMITE",
        )

        if upper in generic:
            continue

        candidates.append(line)

    if not candidates:
        return None

    # Preferimos la línea que contenga "NUM."
    numbered = [
        x for x in candidates
        if (
            "NUM." in x.upper()
            or "NÚM." in x.upper()
            or "NUM " in x.upper()
        )
    ]

    if numbered:
        return max(
            numbered,
            key=len,
        )

    return max(
        candidates,
        key=len,
    )


def get_current_page_text(page):
    try:
        return page.locator(
            "body"
        ).inner_text(
            timeout=10000
        )
    except Exception:
        return ""


# ============================================================
# GACETA
# ============================================================

def get_gaceta_events():

    events = []

    with sync_playwright() as playwright:

        browser = playwright.chromium.launch(
            headless=True
        )

        page = browser.new_page(
            viewport={
                "width": 1600,
                "height": 1200,
            },
            locale="es-MX",
        )

        print(
            "Abriendo Gaceta Parlamentaria..."
        )

        page.goto(
            GACETA_BASE_URL,
            wait_until="networkidle",
            timeout=60000,
        )

        page.wait_for_timeout(
            5000
        )

        # ----------------------------------------------------
        # Guardar HTML para diagnóstico si algo cambia.
        # ----------------------------------------------------

        html = page.content()

        Path(
            "gaceta_debug.html"
        ).write_text(
            html,
            encoding="utf-8",
        )

        # ----------------------------------------------------
        # Selección de Legislatura.
        # ----------------------------------------------------

        try:

            selects = page.locator(
                "select"
            )

            for i in range(
                selects.count()
            ):

                options = selects.nth(i).locator(
                    "option"
                )

                for j in range(
                    options.count()
                ):

                    text = normalize(
                        options.nth(j).inner_text()
                    )

                    if text == "LXIV":

                        value = (
                            options.nth(j)
                            .get_attribute(
                                "value"
                            )
                        )

                        if value:

                            selects.nth(i).select_option(
                                value
                            )

                            page.wait_for_timeout(
                                2000
                            )

                        break

        except Exception as exc:

            print(
                "Aviso selección LXIV:",
                exc,
            )

        # ----------------------------------------------------
        # Selección de periodo.
        # ----------------------------------------------------

        try:

            selects = page.locator(
                "select"
            )

            for i in range(
                selects.count()
            ):

                options = selects.nth(i).locator(
                    "option"
                )

                for j in range(
                    options.count()
                ):

                    text = normalize(
                        options.nth(j).inner_text()
                    )

                    if (
                        "Segundo Año Lectivo"
                        in text
                    ):

                        value = (
                            options.nth(j)
                            .get_attribute(
                                "value"
                            )
                        )

                        if value:

                            selects.nth(i).select_option(
                                value
                            )

                            page.wait_for_timeout(
                                2500
                            )

                        break

        except Exception as exc:

            print(
                "Aviso selección periodo:",
                exc,
            )

        # ----------------------------------------------------
        # INSPECCIÓN REAL DEL CALENDARIO
        # ----------------------------------------------------

        elements = (
            inspect_interactive_calendar(
                page
            )
        )

        print(
            "Elementos interactivos encontrados:",
            len(elements),
        )

        # Mostrar diagnóstico útil en Actions.
        date_candidates = []

        for item in elements:

            if not is_session_color(
                item.get(
                    "background",
                    "",
                )
            ):
                continue

            parsed_date = (
                extract_date_from_attributes(
                    item
                )
            )

            if parsed_date:

                date_candidates.append(
                    (
                        parsed_date,
                        item,
                    )
                )

        # Eliminar duplicados.
        unique_dates = {}

        for parsed_date, item in date_candidates:

            key = (
                parsed_date,
                round(item["x"]),
                round(item["y"]),
            )

            unique_dates[key] = item

        print(
            "Fechas coloreadas con fecha identificable:",
            len(unique_dates),
        )

        # ----------------------------------------------------
        # CLIC EN CADA FECHA
        # ----------------------------------------------------

        for (
            key,
            item,
        ) in unique_dates.items():

            parsed_date = key[0]

            if parsed_date.year < START_YEAR:
                continue

            try:

                # Localizar nuevamente el elemento.
                locator = page.locator(
                    "a, area, button, "
                    "[role='button'], "
                    "[onclick], "
                    "[data-date], "
                    "[data-day], "
                    "[data-fecha], "
                    "[data-id]"
                ).nth(
                    item["index"]
                )

                if locator.count() == 0:
                    continue

                locator.scroll_into_view_if_needed()

                locator.click(
                    timeout=5000
                )

            except Exception:

                # Segundo intento mediante coordenadas.
                try:

                    page.mouse.click(
                        item["x"],
                        item["y"],
                    )

                except Exception:

                    continue

            page.wait_for_timeout(
                500
            )

            body = get_current_page_text(
                page
            )

            title = find_session_title(
                body
            )

            if not title:
                continue

            category = classify(
                title
            )

            if category is None:
                continue

            # Buscar hora cerca del detalle.
            position = body.upper().find(
                title.upper()
            )

            if position >= 0:

                nearby = body[
                    max(
                        0,
                        position - 500,
                    ):
                    position
                    + len(title)
                    + 1000
                ]

            else:

                nearby = body

            time_value = parse_time(
                nearby
            )

            start = event_datetime(
                parsed_date.year,
                parsed_date.month,
                parsed_date.day,
                time_value,
            )

            if time_value:

                end = (
                    start
                    + timedelta(
                        hours=1
                    )
                )

                all_day = False

            else:

                end = (
                    start
                    + timedelta(
                        days=1
                    )
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
                        "en la Gaceta Parlamentaria."
                    ),
                    "source": (
                        "Gaceta Parlamentaria"
                    ),
                    "all_day": all_day,
                }
            )

            print(
                "Gaceta:",
                parsed_date.isoformat(),
                "|",
                category,
                "|",
                title,
            )

        browser.close()

    # --------------------------------------------------------
    # NO PERMITIR RESULTADO SILENCIOSAMENTE VACÍO
    # --------------------------------------------------------

    if not events:

        raise RuntimeError(
            "\n"
            "ERROR: La Gaceta no produjo ningún "
            "evento objetivo.\n"
            "No se generará un calendario vacío.\n"
            "Se creó gaceta_debug.html para diagnóstico."
        )

    return deduplicate_events(
        events
    )


# ============================================================
# AGENDA COMPLEMENTARIA
# ============================================================

def get_agenda_events():

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
            "Consultando agenda:",
            f"{year}-{month:02d}",
        )

        try:

            response = session.get(
                url,
                timeout=REQUEST_TIMEOUT,
            )

            response.raise_for_status()

        except requests.RequestException as exc:

            print(
                "Error agenda:",
                exc,
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

            category = classify(
                title
            )

            if category is None:
                continue

            # Contexto alrededor del enlace.
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

            # Buscar fecha + hora.
            match = re.search(
                r"(\d{1,2})\s+"
                r"([A-Za-zÁÉÍÓÚáéíóúñÑ]+)\s+"
                r"(20\d{2})"
                r".{0,50}?"
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
                    "end": (
                        start
                        + timedelta(
                            hours=1
                        )
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


# ============================================================
# DEDUPLICACIÓN
# ============================================================

def deduplicate_events(
    events: list[dict],
):

    unique = {}

    for event in events:

        key = (
            event["category"],
            event["start"].date().isoformat(),
        )

        if key not in unique:

            unique[key] = event

            continue

        old = unique[key]

        old_score = sum(
            bool(
                old.get(field)
            )
            for field in (
                "url",
                "location",
                "description",
            )
        )

        new_score = sum(
            bool(
                event.get(field)
            )
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
        key=lambda x: x["start"],
    )


# ============================================================
# COMPLEMENTAR GACETA CON AGENDA
# ============================================================

def merge_events(
    gaceta_events,
    agenda_events,
):

    result = []

    for gaceta in gaceta_events:

        candidates = [
            agenda
            for agenda in agenda_events
            if (
                agenda["category"]
                == gaceta["category"]
                and
                agenda["start"].date()
                == gaceta["start"].date()
            )
        ]

        if candidates:

            agenda = candidates[0]

            # Si la Gaceta no proporciona hora,
            # usamos la hora publicada en Agenda.
            if gaceta["all_day"]:

                gaceta["start"] = (
                    agenda["start"]
                )

                gaceta["end"] = (
                    agenda["end"]
                )

                gaceta["all_day"] = False

            if agenda.get("url"):

                gaceta["url"] = (
                    agenda["url"]
                )

            if agenda.get("location"):

                gaceta["location"] = (
                    agenda["location"]
                )

        result.append(
            gaceta
        )

    return deduplicate_events(
        result
    )


# ============================================================
# ICS
# ============================================================

def make_uid(
    event: dict,
):

    raw = (
        f"{event['category']}|"
        f"{event['start'].date()}|"
        f"{event['title']}"
    )

    digest = hashlib.sha1(
        raw.encode("utf-8")
    ).hexdigest()

    return (
        digest
        + "@congreso-jalisco-calendar"
    )


def build_calendar(
    events,
):

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

    for event_data in events:

        event = Event()

        event.add(
            "uid",
            make_uid(
                event_data
            ),
        )

        if event_data[
            "all_day"
        ]:

            event.add(
                "dtstart",
                event_data[
                    "start"
                ].date(),
            )

            event.add(
                "dtend",
                event_data[
                    "end"
                ].date(),
            )

        else:

            event.add(
                "dtstart",
                event_data[
                    "start"
                ],
            )

            event.add(
                "dtend",
                event_data[
                    "end"
                ],
            )

        event.add(
            "summary",
            event_data[
                "title"
            ],
        )

        event.add(
            "categories",
            event_data[
                "category"
            ],
        )

        description = (
            event_data.get(
                "description",
                "",
            )
        )

        if event_data.get(
            "url"
        ):

            description += (
                "\nFuente: "
                + event_data[
                    "url"
                ]
            )

            event.add(
                "url",
                event_data[
                    "url"
                ],
            )

        event.add(
            "description",
            description,
        )

        if event_data.get(
            "location"
        ):

            event.add(
                "location",
                event_data[
                    "location"
                ],
            )

        calendar.add_component(
            event
        )

    return calendar


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 60)

    print(
        "CONGRESO DE JALISCO - CALENDARIO"
    )

    print(
        "FUENTE PRINCIPAL: GACETA PARLAMENTARIA"
    )

    print("=" * 60)

    # 1. Gaceta = fuente principal.
    gaceta_events = (
        get_gaceta_events()
    )

    print()
    print(
        "Gaceta:",
        len(gaceta_events),
        "eventos objetivo",
    )

    # 2. Agenda = complemento.
    agenda_events = (
        get_agenda_events()
    )

    print(
        "Agenda complementaria:",
        len(agenda_events),
        "eventos",
    )

    # 3. Combinar.
    events = merge_events(
        gaceta_events,
        agenda_events,
    )

    print()
    print(
        "=============================="
    )

    print("RESUMEN")

    print(
        "=============================="
    )

    pleno = sum(
        event["category"]
        == "Pleno"
        for event in events
    )

    commission = sum(
        event["category"]
        ==
        "Comisión de Higiene, Salud y "
        "Prevención de las Adicciones"
        for event in events
    )

    print(
        "Pleno:",
        pleno,
    )

    print(
        "Comisión:",
        commission,
    )

    print(
        "TOTAL:",
        len(events),
    )

    print()

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

    # 4. Generar ICS.
    calendar = build_calendar(
        events
    )

    Path(
        OUTPUT_FILE
    ).write_bytes(
        calendar.to_ical()
    )

    print()
    print(
        "Calendario generado:",
        OUTPUT_FILE,
    )


if __name__ == "__main__":
    main()