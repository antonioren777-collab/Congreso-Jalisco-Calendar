from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, date
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

# Meses que aparecen en la web del Congreso en español.
MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}


def normalize(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def is_target(title: str) -> bool:
    t = normalize(title).casefold()
    if any(p in t for p in PLENO_PATTERNS):
        return True
    if any(p in t for p in COMMISSION_PATTERNS):
        return True
    return False


def classify(title: str) -> str | None:
    t = normalize(title).casefold()
    if any(p in t for p in PLENO_PATTERNS):
        return "Pleno"
    if any(p in t for p in COMMISSION_PATTERNS):
        return "Comisión de Higiene, Salud y Prevención de las Adicciones"
    return None


def parse_date_time(text: str, default_year: int, default_month: int):
    text = normalize(text)

    # Ejemplos esperados: "27 Mar 2026 - 09:00" o
    # "27 Marzo 2026 - 09:00 a 09:45".
    pattern = re.compile(
        r"(\d{1,2})\s+([A-Za-zÁÉÍÓÚáéíóúñÑ]+)\s+(\d{4})\s*-\s*"
        r"(\d{1,2}):(\d{2})"
    )
    m = pattern.search(text)
    if m:
        day = int(m.group(1))
        month_name = m.group(2).casefold()
        year = int(m.group(3))
        hour = int(m.group(4))
        minute = int(m.group(5))
        month = MONTHS.get(month_name, default_month)
        return datetime(year, month, day, hour, minute, tzinfo=TZ)

    # Variante numérica ocasional: 27/03/2026 - 09:00
    m = re.search(
        r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})\s*-\s*(\d{1,2}):(\d{2})",
        text,
    )
    if m:
        return datetime(
            int(m.group(3)), int(m.group(2)), int(m.group(1)),
            int(m.group(4)), int(m.group(5)), tzinfo=TZ
        )

    return None


def extract_events_from_month(year: int, month: int) -> list[dict]:
    url = MONTH_URL.format(year=year, month=month)
    r = requests.get(
        url,
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    results = []
    seen = set()

    # Primero buscamos enlaces que apunten a fichas /agenda/... .
    for a in soup.select('a[href*="/agenda/"]'):
        title = normalize(a.get_text(" ", strip=True))
        href = a.get("href")
        if not href or not title or not is_target(title):
            continue

        event_url = urljoin(BASE_URL, href)

        # El texto útil suele estar en el contenedor inmediato del enlace.
        container = a
        for _ in range(5):
            if container.parent:
                container = container.parent
            txt = normalize(container.get_text(" ", strip=True))
            if len(txt) > len(title) + 10:
                break

        start = parse_date_time(txt, year, month)
        if start is None:
            # Buscar en elementos hermanos/ancestros por si el HTML cambia.
            for candidate in [a.parent, a.parent.parent if a.parent else None]:
                if candidate:
                    start = parse_date_time(
                        candidate.get_text(" ", strip=True), year, month
                    )
                    if start:
                        break

        if start is None:
            continue

        key = (title.casefold(), start.isoformat(), event_url)
        if key in seen:
            continue
        seen.add(key)

        # Intentamos obtener detalles de la ficha individual.
        location = ""
        description = ""
        try:
            detail = requests.get(
                event_url,
                timeout=REQUEST_TIMEOUT,
                headers={"User-Agent": USER_AGENT},
            )
            detail.raise_for_status()
            ds = BeautifulSoup(detail.text, "html.parser")
            body_text = normalize(ds.get_text(" ", strip=True))

            # No dependemos de una estructura fija: guardamos el texto de la ficha
            # como descripción si no podemos aislar campos.
            location_match = re.search(
                r"Lugar:\s*(.+?)(?:Descripción:|Tipo de agenda:|$)",
                body_text,
                flags=re.I,
            )
            if location_match:
                location = normalize(location_match.group(1))

            description_match = re.search(
                r"Descripción:\s*(.+?)(?:Tipo de agenda:|$)",
                body_text,
                flags=re.I,
            )
            if description_match:
                description = normalize(description_match.group(1))
        except requests.RequestException:
            pass

        results.append({
            "title": title,
            "category": classify(title),
            "start": start,
            "url": event_url,
            "location": location,
            "description": description,
        })

    return results


def event_uid(item: dict) -> str:
    raw = f'{item["title"]}|{item["start"].isoformat()}|{item["url"]}'
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"{digest}@congreso-jalisco-calendar"


def build_calendar(items: list[dict]) -> Calendar:
    cal = Calendar()
    cal.add("prodid", "-//Congreso de Jalisco Calendar//ES//")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", "Congreso de Jalisco")
    cal.add("x-wr-timezone", TIMEZONE)
    cal.add("refresh-interval;value=duration", "PT12H")
    cal.add("x-published-ttl", "PT12H")

    for item in sorted(items, key=lambda x: x["start"]):
        e = Event()
        e.add("uid", event_uid(item))
        e.add("summary", item["title"])
        e.add("dtstart", item["start"])
        e.add("dtstamp", datetime.now(TZ))

        # Duración por defecto de 1 hora; si el sitio publica un rango,
        # el parser puede ampliarse posteriormente sin cambiar el formato.
        e.add("dtend", item["start"].replace(second=0) + relativedelta(hours=1))

        e.add("categories", item["category"])
        e.add("url", item["url"])

        desc = item["description"]
        if item["url"]:
            desc = (desc + "\n\nFicha del Congreso: " + item["url"]).strip()
        if desc:
            e.add("description", desc)
        if item["location"]:
            e.add("location", item["location"])

        cal.add_component(e)

    return cal


def months_to_scan():
    today = date.today().replace(day=1)
    for i in range(LOOK_AHEAD_MONTHS + 1):
        d = today + relativedelta(months=i)
        yield d.year, d.month


def main():
    all_events = []
    failures = []

    for year, month in months_to_scan():
        try:
            events = extract_events_from_month(year, month)
            print(f"{year}-{month:02d}: {len(events)} eventos filtrados")
            all_events.extend(events)
        except Exception as exc:
            failures.append(f"{year}-{month:02d}: {exc}")
            print(f"ERROR {year}-{month:02d}: {exc}")

    # Elimina duplicados entre páginas mensuales.
    unique = {}
    for item in all_events:
        unique[(item["title"].casefold(), item["start"].isoformat(), item["url"])] = item

    if failures:
        print("Avisos:")
        for f in failures:
            print(" -", f)

    cal = build_calendar(list(unique.values()))
    Path(OUTPUT_FILE).write_bytes(cal.to_ical())

    print(f"Calendario generado: {OUTPUT_FILE}")
    print(f"Eventos: {len(unique)}")

    # Si no se pudo consultar ninguna página, no sobrescribimos con un
    # calendario vacío: esto protege la suscripción ante una caída temporal.
    if not unique and not failures:
        raise RuntimeError("No se encontraron eventos; se evita publicar un calendario vacío.")


if __name__ == "__main__":
    main()
