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
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "octubre": 10, "noviembre": 11,
    "diciembre": 12,
}


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def classify(title: str) -> str | None:
    text = normalize(title).casefold()

    if any(p.casefold() in text for p in PLENO_PATTERNS):
        return "Pleno"

    if any(p.casefold() in text for p in COMMISSION_PATTERNS):
        return "Comisión de Higiene, Salud y Prevención de las Adicciones"

    if "higiene" in text and "salud" in text and "adicciones" in text:
        return "Comisión de Higiene, Salud y Prevención de las Adicciones"

    return None


def parse_time(text: str):
    m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", text or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def parse_detail(body: str):
    """
    Extrae el nombre REAL de la sesión.

    Prioridad:
    1. SESIÓN NUM. # ...
    2. SESION NUM. # ... (sin acento)
    3. Títulos específicos de sesión.
    
    Ignora las leyendas genéricas del calendario.
    """

    lines = [
        normalize(x)
        for x in body.splitlines()
        if normalize(x)
    ]

    # ---------------------------------------------------------
    # 1. PRIORIDAD ABSOLUTA: SESIÓN NUM. #
    # ---------------------------------------------------------
    session_patterns = [
        re.compile(
            r"^.*?SESI[ÓO]N\s+NUM\.?\s*#?\s*\d+.*$",
            re.IGNORECASE
        ),
        re.compile(
            r"^.*?SESI[ÓO]N\s+N[ÚU]M\.?\s*#?\s*\d+.*$",
            re.IGNORECASE
        ),
    ]

    for line in lines:
        for pattern in session_patterns:
            if pattern.search(line):
                # Evitar textos que sean solamente una leyenda
                upper = line.upper()

                if (
                    "SESIÓN DE PLENO DEL CONGRESO" == upper
                    or "SESION DE PLENO DEL CONGRESO" == upper
                    or "SESIÓN DE COMISIÓN/COMITÉ" == upper
                    or "SESION DE COMISION/COMITE" == upper
                ):
                    continue

                return line, parse_time(line)

    # ---------------------------------------------------------
    # 2. Buscar títulos específicos de PLENO
    # ---------------------------------------------------------
    for line in lines:
        upper = line.upper()

        if "PLENO" not in upper:
            continue

        if any(generic in upper for generic in (
            "SESIÓN DE PLENO DEL CONGRESO",
            "SESION DE PLENO DEL CONGRESO",
            "REANUDACIÓN SESIÓN DE PLENO",
            "REANUDACION SESION DE PLENO",
        )):
            continue

        if "SESIÓN" in upper or "SESION" in upper:
            return line, parse_time(line)

    # ---------------------------------------------------------
    # 3. Buscar títulos específicos de COMISIÓN
    # ---------------------------------------------------------
    for line in lines:
        upper = line.upper()

        if "COMISIÓN" not in upper and "COMISION" not in upper:
            continue

        if any(generic in upper for generic in (
            "SESIÓN DE COMISIÓN/COMITÉ",
            "SESION DE COMISION/COMITE",
            "EVENTO DE COMISIÓN/COMITÉ",
            "EVENTO DE COMISION/COMITE",
        )):
            continue

        if "SALUD" in upper or "HIGIENE" in upper or "ADICCIONES" in upper:
            return line, parse_time(line)

    # ---------------------------------------------------------
    # 4. Si no encontró título específico, no usar la leyenda
    # ---------------------------------------------------------
    return None, None

def get_gaceta_events():
    events = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 1600, "height": 1200},
            locale="es-MX",
        )

        print("Abriendo Gaceta Parlamentaria...")
        page.goto(
            GACETA_BASE_URL,
            wait_until="networkidle",
            timeout=60000,
        )
        page.wait_for_timeout(3000)

        # La Gaceta usa jQuery UI Datepicker.
        # Cada día con información es un TD con:
        # data-handler="selectDay"
        # data-month="0..11"
        # data-year="2026"
        # y clases: sesple, rsesple, sescom o varios.
        cells = page.locator(
            "#datepicker td[data-handler='selectDay'][data-year='2026']"
        )

        count = cells.count()
        print(f"Días 2026 interactivos encontrados: {count}")

        for i in range(count):
            cell = cells.nth(i)

            try:
                month = int(cell.get_attribute("data-month"))
                year = int(cell.get_attribute("data-year"))
                day_text = normalize(cell.inner_text())
                day = int(re.search(r"\b\d{1,2}\b", day_text).group())
                cls = cell.get_attribute("class") or ""
                title_attr = cell.get_attribute("title") or ""

                # Solo días que realmente tienen información.
                if "ui-datepicker-unselectable" in cls:
                    continue

                if month + 1 < START_MONTH and year == START_YEAR:
                    continue

                # El calendario usa:
                # sesple = Pleno
                # rsesple = Reanudación del Pleno
                # sescom = Comisión/Comité
                # varios = actividades mixtas; puede contener Pleno.
                if not any(
                    x in cls for x in ("sesple", "rsesple", "sescom", "varios")
                ):
                    continue

                print(
                    f"  Gaceta: {year}-{month + 1:02d}-{day:02d} "
                    f"class={cls.strip()} title={title_attr}"
                )

                cell.scroll_into_view_if_needed()
                cell.click(timeout=5000)
                page.wait_for_timeout(350)

                body = page.locator("body").inner_text(timeout=10000)
                title, time_value = parse_detail(body)

                if not title:
                    print("    -> sin detalle de sesión")
                    continue

                category = classify(title)
                if category is None:
                    print(f"    -> ignorado: {title}")
                    continue

                start = datetime(
                    year,
                    month + 1,
                    day,
                    *(time_value or (0, 0)),
                    tzinfo=TZ,
                )

                end = start + (
                    timedelta(hours=1)
                    if time_value
                    else timedelta(days=1)
                )

                events.append({
                    "title": title,
                    "category": category,
                    "start": start,
                    "end": end,
                    "url": GACETA_BASE_URL,
                    "location": "",
                    "description": (
                        "Detectado directamente en el "
                        "Calendario de la Gaceta Parlamentaria."
                    ),
                    "source": "Gaceta Parlamentaria",
                    "all_day": time_value is None,
                })

                print(f"    -> {category}: {title}")

            except Exception as exc:
                print(f"    -> error en celda {i}: {exc}")

        browser.close()

    if not events:
        raise RuntimeError(
            "La Gaceta no produjo ningún evento objetivo. "
            "No se generará un calendario vacío."
        )

    return deduplicate(events)


def get_agenda_events():
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "es-MX,es;q=0.9",
    })

    results = []
    current = date(START_YEAR, START_MONTH, 1)

    for _ in range(LOOK_AHEAD_MONTHS):
        year, month = current.year, current.month
        url = MONTH_URL.format(year=year, month=month)

        print(f"Consultando agenda: {year}-{month:02d}")

        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
        except requests.RequestException as exc:
            print(f"  Agenda no disponible: {exc}")
            current += relativedelta(months=1)
            continue

        soup = BeautifulSoup(response.text, "html.parser")

        for link in soup.find_all("a", href=True):
            title = normalize(link.get_text(" ", strip=True))
            category = classify(title)
            if not category:
                continue

            parent = link
            context = title
            for _ in range(8):
                if parent is None:
                    break
                txt = normalize(parent.get_text(" ", strip=True))
                if len(txt) > len(context):
                    context = txt
                parent = parent.parent

            m = re.search(
                r"(\d{1,2})\s+([A-Za-zÁÉÍÓÚáéíóúñÑ]+)\s+(20\d{2}).{0,60}?(\d{1,2}):(\d{2})",
                context,
                re.I,
            )
            if not m:
                continue

            mo = MONTHS.get(m.group(2).casefold())
            if not mo:
                continue

            try:
                start = datetime(
                    int(m.group(3)), mo, int(m.group(1)),
                    int(m.group(4)), int(m.group(5)),
                    tzinfo=TZ,
                )
            except ValueError:
                continue

            results.append({
                "title": title,
                "category": category,
                "start": start,
                "end": start + timedelta(hours=1),
                "url": urljoin(AGENDA_BASE_URL, link["href"]),
                "location": "",
                "description": "",
                "source": "Agenda Parlamentaria",
                "all_day": False,
            })

        current += relativedelta(months=1)

    return results


def deduplicate(events):
    unique = {}

    for event in events:
        key = (event["category"], event["start"].date().isoformat())

        if key not in unique:
            unique[key] = event
            continue

        old = unique[key]
        old_score = sum(bool(old.get(x)) for x in ("url", "location", "description"))
        new_score = sum(bool(event.get(x)) for x in ("url", "location", "description"))

        if new_score > old_score:
            unique[key] = event

    return sorted(unique.values(), key=lambda x: x["start"])


def merge_events(gaceta_events, agenda_events):
    result = []

    for g in gaceta_events:
        candidates = [
            a for a in agenda_events
            if a["category"] == g["category"]
            and a["start"].date() == g["start"].date()
        ]

        if candidates:
            a = candidates[0]

            if g["all_day"]:
                g["start"] = a["start"]
                g["end"] = a["end"]
                g["all_day"] = False

            if a.get("url"):
                g["url"] = a["url"]

            if a.get("location"):
                g["location"] = a["location"]

            g["description"] += "\nComplementado con la Agenda Parlamentaria."

        result.append(g)

    return deduplicate(result)


def uid_for(event):
    raw = f"{event['category']}|{event['start'].date()}|{event['title']}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest() + "@congreso-jalisco"


def build_calendar(events):
    cal = Calendar()
    cal.add("prodid", "-//Congreso Jalisco Calendar//ES//")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("X-WR-CALNAME", "Congreso de Jalisco")
    cal.add("X-WR-TIMEZONE", TIMEZONE)

    for e in events:
        item = Event()
        item.add("uid", uid_for(e))

        if e["all_day"]:
            item.add("dtstart", e["start"].date())
            item.add("dtend", e["end"].date())
        else:
            item.add("dtstart", e["start"])
            item.add("dtend", e["end"])

        item.add("summary", e["title"])
        item.add("categories", e["category"])

        if e.get("location"):
            item.add("location", e["location"])

        description = e.get("description", "")
        if e.get("url"):
            description += f"\nFuente: {e['url']}"
            item.add("url", e["url"])

        item.add("description", description)
        cal.add_component(item)

    return cal


def main():
    print("=" * 60)
    print("CONGRESO DE JALISCO - CALENDARIO")
    print("FUENTE PRINCIPAL: GACETA PARLAMENTARIA")
    print("=" * 60)

    gaceta = get_gaceta_events()
    print(f"\nGaceta: {len(gaceta)} eventos objetivo")

    agenda = get_agenda_events()
    print(f"Agenda complementaria: {len(agenda)} eventos")

    events = merge_events(gaceta, agenda)

    pleno = sum(e["category"] == "Pleno" for e in events)
    commission = sum(
        e["category"]
        == "Comisión de Higiene, Salud y Prevención de las Adicciones"
        for e in events
    )

    print("\nRESUMEN")
    print(f"Pleno: {pleno}")
    print(f"Comisión: {commission}")
    print(f"TOTAL: {len(events)}")

    for e in events:
        print(
            e["start"].strftime("%Y-%m-%d %H:%M"),
            "|",
            e["category"],
            "|",
            e["title"],
        )

    Path(OUTPUT_FILE).write_bytes(
        build_calendar(events).to_ical()
    )

    print(f"\nCalendario generado: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()