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
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
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
    "enero": 1, "ene": 1, "febrero": 2, "feb": 2,
    "marzo": 3, "mar": 3, "abril": 4, "abr": 4,
    "mayo": 5, "may": 5, "junio": 6, "jun": 6,
    "julio": 7, "jul": 7, "agosto": 8, "ago": 8,
    "septiembre": 9, "sept": 9, "sep": 9, "setiembre": 9,
    "octubre": 10, "oct": 10, "noviembre": 11, "nov": 11,
    "diciembre": 12, "dic": 12,
}


def normalize(text: str) -> str:
    text = (text or "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def classify(title: str) -> str | None:
    t = normalize(title).casefold()

    if any(p.casefold() in t for p in PLENO_PATTERNS):
        return "Pleno"

    if any(p.casefold() in t for p in COMMISSION_PATTERNS):
        return "Comisión de Higiene, Salud y Prevención de las Adicciones"

    # La Gaceta puede mostrar "HIGIENE, SALUD..." sin "comisión".
    if "higiene" in t and "adicciones" in t:
        return "Comisión de Higiene, Salud y Prevención de las Adicciones"

    return None


def parse_time(text: str) -> tuple[int, int] | None:
    m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", text)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def make_datetime(y: int, m: int, d: int, time_text: str | None = None) -> datetime:
    if time_text:
        hm = parse_time(time_text)
    else:
        hm = None
    hour, minute = hm if hm else (0, 0)
    return datetime(y, m, d, hour, minute, tzinfo=TZ)


def month_from_label(label: str) -> tuple[int, int] | None:
    m = re.search(
        r"(enero|febrero|marzo|abril|mayo|junio|julio|agosto|"
        r"septiembre|octubre|noviembre|diciembre)\s+(\d{4})",
        label.casefold(),
    )
    if not m:
        return None
    return MONTHS[m.group(1)], int(m.group(2))


def is_colored(rgb: str) -> bool:
    """Detecta celdas marcadas del calendario sin depender de clases CSS."""
    m = re.match(r"rgba?\((\d+),\s*(\d+),\s*(\d+)", rgb or "")
    if not m:
        return False
    r, g, b = map(int, m.groups())
    # Blanco/gris muy claro = día sin evento.
    luminance = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255
    return luminance < 0.88


def extract_calendar_cells(page, month_label: str) -> list[dict]:
    """
    Encuentra el bloque visual del mes y devuelve los días coloreados.
    No depende de una clase CSS concreta: la Gaceta puede cambiar su HTML.
    """
    return page.evaluate(
        """
        (label) => {
          const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
          const heading = [...document.querySelectorAll('*')]
            .find(el => norm(el.textContent) === label);

          if (!heading) return [];

          let best = null;
          let node = heading;

          for (let level = 0; level < 10 && node; level++, node = node.parentElement) {
            const all = [...node.querySelectorAll('*')];
            const nums = all.filter(el => {
              const t = norm(el.textContent);
              return /^([1-9]|[12]\\d|3[01])$/.test(t);
            });

            if (nums.length >= 15) {
              if (!best || nums.length < best.count || best.count < 15) {
                best = { node, count: nums.length };
              }
            }
          }

          if (!best) return [];

          const candidates = [...best.node.querySelectorAll('*')]
            .filter(el => {
              const t = norm(el.textContent);
              if (!/^([1-9]|[12]\\d|3[01])$/.test(t)) return false;

              // Preferir hojas o elementos que no contengan otro elemento
              // con exactamente el mismo texto.
              const same = [...el.children].some(c => norm(c.textContent) === t);
              if (same) return false;

              const r = el.getBoundingClientRect();
              return r.width >= 8 && r.height >= 8;
            });

          return candidates.map(el => {
            const r = el.getBoundingClientRect();
            const cs = getComputedStyle(el);
            return {
              day: Number(norm(el.textContent)),
              x: r.left + r.width / 2,
              y: r.top + r.height / 2,
              background: cs.backgroundColor,
              color: cs.color,
              cls: String(el.className || ''),
              title: el.getAttribute('title') || '',
              aria: el.getAttribute('aria-label') || ''
            };
          });
        }
        """,
        month_label,
    )


def extract_detail_text(page) -> str:
    return page.locator("body").inner_text(timeout=10000)


def extract_event_title(body: str) -> str | None:
    lines = [normalize(x) for x in body.splitlines() if normalize(x)]
    candidates = []

    for line in lines:
        upper = line.upper()
        if "SESIÓN" not in upper and "SESION" not in upper:
            continue
        if "PLENO" in upper or "COMISIÓN" in upper or "COMISION" in upper:
            if "SESIÓN DE PLENO DEL CONGRESO" == upper:
                continue
            if "SESIÓN DE COMISIÓN/COMITÉ" == upper:
                continue
            if "SESIÓN DE COMISION/COMITE" == upper:
                continue
            candidates.append(line)

    if not candidates:
        # Segunda forma: la Gaceta puede usar "SESIÓN NUM. 91..."
        for line in lines:
            upper = line.upper()
            if ("SESION NUM" in upper or "SESIÓN NUM" in upper) and (
                "PLENO" in upper or "COMIS" in upper
            ):
                candidates.append(line)

    if not candidates:
        return None

    return max(candidates, key=len)


def get_gaceta_events() -> list[dict]:
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
            wait_until="domcontentloaded",
            timeout=60000,
        )
        page.wait_for_timeout(2500)

        # Intentamos mantener LXIV / Segundo Año Lectivo si la página
        # presenta selects. Si ya vienen seleccionados, no hacemos nada.
        for selector, wanted in [
            ("select", "LXIV"),
            ("select", "Segundo Año Lectivo"),
        ]:
            try:
                selects = page.locator(selector)
                count = selects.count()
                for i in range(count):
                    options = selects.nth(i).locator("option")
                    labels = [
                        normalize(options.nth(j).inner_text())
                        for j in range(options.count())
                    ]
                    match = next(
                        (j for j, label in enumerate(labels)
                         if wanted.casefold() in label.casefold()),
                        None,
                    )
                    if match is not None:
                        value = options.nth(match).get_attribute("value")
                        if value:
                            selects.nth(i).select_option(value)
                            page.wait_for_timeout(1200)
                        break
            except Exception:
                pass

        # La página muestra varios meses simultáneamente. Escaneamos todos
        # los encabezados "Mes Año" visibles, no una lista fija de fechas.
        labels = page.evaluate(
            """
            () => {
              const months = [
                'enero','febrero','marzo','abril','mayo','junio',
                'julio','agosto','septiembre','octubre','noviembre','diciembre'
              ];
              const re = new RegExp(
                '^(' + months.join('|') + ')\\\\s+20\\\\d{2}$',
                'i'
              );
              const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
              return [...document.querySelectorAll('*')]
                .map(el => norm(el.textContent))
                .filter(t => re.test(t))
                .filter((v, i, a) => a.indexOf(v) === i);
            }
            """
        )

        print("Meses detectados:", ", ".join(labels))

        for label in labels:
            parsed = month_from_label(label)
            if not parsed:
                continue

            month, year = parsed
            if year < START_YEAR:
                continue

            cells = extract_calendar_cells(page, label)

            # Deduplicar candidatos que provienen de spans/elementos anidados.
            unique = {}
            for c in cells:
                if not is_colored(c["background"]):
                    continue
                key = (c["day"], round(c["x"]), round(c["y"]))
                unique[key] = c

            marked = sorted(unique.values(), key=lambda x: x["day"])

            if not marked:
                continue

            print(f"{label}: {len(marked)} días marcados")

            for cell in marked:
                try:
                    page.mouse.click(cell["x"], cell["y"])
                    page.wait_for_timeout(450)
                    body = extract_detail_text(page)
                except Exception:
                    continue

                title = extract_event_title(body)
                if not title:
                    continue

                category = classify(title)
                if category is None:
                    continue

                # Evita confundir el número del día con un número de sesión.
                day = cell["day"]

                # Buscar hora cerca del título.
                pos = body.upper().find(title.upper())
                nearby = body[max(0, pos - 300): pos + len(title) + 800] if pos >= 0 else body
                hm = parse_time(nearby)

                start = make_datetime(year, month, day, f"{hm[0]:02d}:{hm[1]:02d}" if hm else None)
                end = start + (timedelta(hours=1) if hm else timedelta(days=1))

                events.append({
                    "title": title,
                    "category": category,
                    "start": start,
                    "end": end,
                    "url": GACETA_BASE_URL,
                    "location": "",
                    "description": (
                        "Evento detectado directamente en el "
                        "Calendario de la Gaceta Parlamentaria."
                    ),
                    "source": "Gaceta Parlamentaria",
                    "all_day": hm is None,
                })

        browser.close()

    return events


def get_agenda_events() -> list[dict]:
    """
    Fuente complementaria. Solo aporta hora/lugar/enlace cuando la Agenda
    ya publicó el evento. Nunca crea un Pleno por sí sola.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "es-MX,es;q=0.9",
    })

    results = []

    current = date(START_YEAR, START_MONTH, 1)

    for _ in range(LOOK_AHEAD_MONTHS):
        y, m = current.year, current.month
        url = MONTH_URL.format(year=y, month=m)

        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
        except requests.RequestException as exc:
            print(f"{y}-{m:02d}: no se pudo consultar Agenda ({exc})")
            current += relativedelta(months=1)
            continue

        soup = BeautifulSoup(response.text, "html.parser")
        text = soup.get_text(" ", strip=True)

        # Solo guardamos enlaces/títulos que correspondan a nuestros dos
        # grupos. La Gaceta sigue siendo la fuente que decide si existe.
        for a in soup.find_all("a", href=True):
            title = normalize(a.get_text(" ", strip=True))
            category = classify(title)
            if not category:
                continue

            href = urljoin(AGENDA_BASE_URL, a["href"])
            parent = a
            context = title

            for _ in range(7):
                if parent is None:
                    break
                t = normalize(parent.get_text(" ", strip=True))
                if len(t) > len(context):
                    context = t
                if re.search(r"\b\d{1,2}\s+(?:de\s+)?[A-Za-zÁÉÍÓÚáéíóúñÑ]+\s+20\d{2}\b", t):
                    break
                parent = parent.parent

            dt = None
            mdt = re.search(
                r"(\d{1,2})\s+([A-Za-zÁÉÍÓÚáéíóúñÑ]+)\s+(20\d{2})\D{0,30}(\d{1,2}):(\d{2})",
                context,
                re.I,
            )
            if mdt:
                mo = MONTHS.get(mdt.group(2).casefold())
                if mo:
                    try:
                        dt = datetime(
                            int(mdt.group(3)), mo, int(mdt.group(1)),
                            int(mdt.group(4)), int(mdt.group(5)),
                            tzinfo=TZ,
                        )
                    except ValueError:
                        pass

            if dt:
                results.append({
                    "title": title,
                    "category": category,
                    "start": dt,
                    "end": dt + timedelta(hours=1),
                    "url": href,
                    "location": "",
                    "description": "",
                    "source": "Agenda Parlamentaria",
                    "all_day": False,
                })

        current += relativedelta(months=1)

    return results


def merge_events(gaceta: list[dict], agenda: list[dict]) -> list[dict]:
    """
    La Gaceta manda. La Agenda solo complementa.
    """
    agenda_by_date = {}

    for a in agenda:
        key = (a["category"], a["start"].date())
        agenda_by_date.setdefault(key, []).append(a)

    merged = []

    for g in gaceta:
        key = (g["category"], g["start"].date())
        candidates = agenda_by_date.get(key, [])

        best = None
        if candidates:
            # Preferir título que comparta palabras relevantes.
            best = max(
                candidates,
                key=lambda a: len(set(g["title"].casefold().split()) &
                                  set(a["title"].casefold().split()))
            )

        item = dict(g)

        if best:
            if not g["all_day"]:
                item["start"] = best["start"]
                item["end"] = best["end"]
                item["all_day"] = False
            item["url"] = best.get("url") or g["url"]
            item["location"] = best.get("location", "")
            item["description"] = (
                g["description"] +
                "\nComplementado con la Agenda Parlamentaria."
            )

        merged.append(item)

    # Deduplicación final.
    unique = {}
    for e in merged:
        key = (
            e["category"],
            e["start"].date().isoformat(),
        )

        # Si hay dos eventos el mismo día de la misma categoría,
        # conservar el que tenga más información.
        if key not in unique:
            unique[key] = e
        else:
            old = unique[key]
            old_score = sum(bool(old.get(k)) for k in ("url", "location", "description"))
            new_score = sum(bool(e.get(k)) for k in ("url", "location", "description"))
            if new_score > old_score:
                unique[key] = e

    return sorted(unique.values(), key=lambda e: e["start"])


def uid_for(event: dict) -> str:
    raw = f"{event['category']}|{event['start'].date()}|{event['title']}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest() + "@congreso-jalisco"


def build_calendar(events: list[dict]) -> Calendar:
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
        item.add("description", description)

        if e.get("url"):
            item.add("url", e["url"])

        cal.add_component(item)

    return cal


def main():
    print("=" * 60)
    print(" Congreso de Jalisco - Calendario")
    print(" FUENTE PRINCIPAL: Gaceta Parlamentaria")
    print("=" * 60)

    gaceta_events = get_gaceta_events()
    print(f"\nGaceta: {len(gaceta_events)} eventos objetivo")

    agenda_events = get_agenda_events()
    print(f"Agenda complementaria: {len(agenda_events)} eventos")

    events = merge_events(gaceta_events, agenda_events)

    print("\nRESUMEN")
    print(f"Pleno: {sum(e['category'] == 'Pleno' for e in events)}")
    print("Comisión:", sum(
        e["category"] == "Comisión de Higiene, Salud y Prevención de las Adicciones"
        for e in events
    ))
    print(f"TOTAL: {len(events)}")

    for e in events:
        print(
            e["start"].strftime("%Y-%m-%d %H:%M"),
            "|",
            e["category"],
            "|",
            e["title"],
            "|",
            e["source"],
        )

    cal = build_calendar(events)
    Path(OUTPUT_FILE).write_bytes(cal.to_ical())

    print(f"\nCalendario generado: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
