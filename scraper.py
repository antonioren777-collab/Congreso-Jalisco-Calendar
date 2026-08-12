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

    # Respaldo: cualquier texto que contenga "pleno" es Pleno.
    if "pleno" in text:
        return "Pleno"

    # Respaldo: cualquier texto que contenga "salud" es Comisión.
    if "salud" in text:
        return (
            "Comisión de Higiene, Salud Pública "
            "y Prevención de las Adicciones"
        )

    if any(p.casefold() in text for p in PLENO_PATTERNS):
        return "Pleno"

    if any(p.casefold() in text for p in COMMISSION_PATTERNS):
        return (
            "Comisión de Higiene, Salud Pública "
            "y Prevención de las Adicciones"
        )

    if "higiene" in text or "adicciones" in text:
        return (
            "Comisión de Higiene, Salud Pública "
            "y Prevención de las Adicciones"
        )

    return None


def parse_time(text: str):
    m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", text or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def parse_detail(body: str, category_hint: str | None = None):
    """Obtiene el nombre real de la sesión, evitando leyendas genéricas."""

    lines = [normalize(x) for x in body.splitlines() if normalize(x)]

    generic = {
        "SESIÓN DE PLENO DEL CONGRESO",
        "SESION DE PLENO DEL CONGRESO",
        "REANUDACIÓN SESIÓN DE PLENO DEL CONGRESO",
        "REANUDACION SESION DE PLENO DEL CONGRESO",
        "SESIÓN DE COMISIÓN/COMITÉ",
        "SESION DE COMISION/COMITE",
        "EVENTO DE COMISIÓN/COMITÉ",
        "EVENTO DE COMISION/COMITE",
    }

    # 1) Primero buscar una sesión numerada real.
    numbered = []
    for line in lines:
        upper = line.upper()
        if not ("SESIÓN" in upper or "SESION" in upper):
            continue
        if not re.search(r"\b(?:N[ÚU]M\.?|NUM\.?)\s*\d+", upper):
            continue
        if upper.strip() in generic:
            continue
        numbered.append(line)

    if numbered:
        if category_hint == "Pleno":
            for line in numbered:
                if "PLENO" in line.upper():
                    return line, parse_time(line)
        if category_hint == "Comisión":
            for line in numbered:
                upper = line.upper()
                if (
                    "COMISIÓN" in upper
                    or "COMISION" in upper
                    or "SALUD" in upper
                    or "HIGIENE" in upper
                    or "ADICCIONES" in upper
                ):
                    return line, parse_time(line)
        return numbered[0], parse_time(numbered[0])

    # 2) Si no hay número, buscar título específico según la clase.
    for line in lines:
        upper = line.upper()
        if upper.strip() in generic:
            continue
        if "SESIÓN" not in upper and "SESION" not in upper:
            continue

        if category_hint == "Pleno" and "PLENO" in upper:
            return line, parse_time(line)

        if category_hint == "Comisión" and (
            "COMISIÓN" in upper
            or "COMISION" in upper
            or "SALUD" in upper
            or "HIGIENE" in upper
            or "ADICCIONES" in upper
        ):
            return line, parse_time(line)

    # 3) Último respaldo: cualquier sesión no genérica.
    for line in lines:
        upper = line.upper()
        if upper.strip() in generic:
            continue
        if "SESIÓN" in upper or "SESION" in upper:
            return line, parse_time(line)

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

                # La clase del día es la fuente principal de la categoría.
                if "sesple" in cls or "rsesple" in cls:
                    category_hint = "Pleno"
                elif "sescom" in cls:
                    category_hint = "Comisión"
                else:
                    category_hint = None

                title, time_value = parse_detail(
                    body,
                    category_hint,
                )

                if not title:
                    print("    -> sin detalle de sesión")
                    continue

                if category_hint == "Pleno":
                    category = "Pleno"
                elif category_hint == "Comisión":
                    category = (
                        "Comisión de Higiene, Salud Pública "
                        "y Prevención de las Adicciones"
                    )
                else:
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