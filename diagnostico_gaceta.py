from __future__ import annotations

import json
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

from config import GACETA_BASE_URL


def normalizar(texto):
    return re.sub(r"\s+", " ", texto or "").strip()


def es_fecha_valida(year, month_zero, day):
    """
    El calendario de jQuery UI utiliza:
      data-month = 0 para enero, 1 para febrero, etc.
    """
    month = month_zero + 1

    if year < 2026:
        return False

    if year == 2026 and month < 7:
        return False

    return True


with sync_playwright() as p:

    browser = p.chromium.launch(headless=True)

    page = browser.new_page(
        viewport={
            "width": 1600,
            "height": 1400,
        },
        locale="es-MX",
    )

    print("=" * 70)
    print("DIAGNÓSTICO GACETA PARLAMENTARIA")
    print("=" * 70)

    print("\nAbriendo Gaceta...")

    page.goto(
        GACETA_BASE_URL,
        wait_until="networkidle",
        timeout=60000,
    )

    page.wait_for_timeout(5000)

    # ------------------------------------------------------------
    # Captura inicial
    # ------------------------------------------------------------

    page.screenshot(
        path="gaceta_debug.png",
        full_page=True,
    )

    Path(
        "gaceta_debug.html"
    ).write_text(
        page.content(),
        encoding="utf-8",
    )

    # ------------------------------------------------------------
    # Encontrar TODAS las fechas del calendario
    # ------------------------------------------------------------

    cells = page.locator(
        "td[data-handler='selectDay']"
    )

    total = cells.count()

    print(
        f"\nCeldas de fechas encontradas: {total}"
    )

    fechas = []

    for i in range(total):

        cell = cells.nth(i)

        try:

            year_raw = cell.get_attribute(
                "data-year"
            )

            month_raw = cell.get_attribute(
                "data-month"
            )

            if (
                year_raw is None
                or month_raw is None
            ):
                continue

            year = int(year_raw)
            month_zero = int(month_raw)

            if not es_fecha_valida(
                year,
                month_zero,
                1,
            ):
                continue

            # Obtener el número del día desde el enlace.
            link = cell.locator("a").first

            if link.count() == 0:
                continue

            day_text = normalizar(
                link.inner_text()
            )

            if not day_text.isdigit():
                continue

            day = int(day_text)

            classes = (
                cell.get_attribute("class")
                or ""
            )

            title = (
                cell.get_attribute("title")
                or ""
            )

            fechas.append(
                {
                    "index": i,
                    "year": year,
                    "month": month_zero + 1,
                    "day": day,
                    "classes": classes,
                    "title": title,
                }
            )

        except Exception as exc:

            print(
                f"Error leyendo celda {i}: {exc}"
            )

    # Quitar duplicados.
    unicas = {}

    for fecha in fechas:

        key = (
            fecha["year"],
            fecha["month"],
            fecha["day"],
        )

        unicas[key] = fecha

    fechas = sorted(
        unicas.values(),
        key=lambda x: (
            x["year"],
            x["month"],
            x["day"],
        ),
    )

    print(
        f"Fechas válidas desde julio de 2026: "
        f"{len(fechas)}"
    )

    # ------------------------------------------------------------
    # Abrir cada fecha y guardar el texto real
    # ------------------------------------------------------------

    resultados = []

    for numero, fecha in enumerate(
        fechas,
        start=1,
    ):

        year = fecha["year"]
        month = fecha["month"]
        day = fecha["day"]

        print(
            f"\n[{numero}/{len(fechas)}] "
            f"{year}-{month:02d}-{day:02d}"
        )

        try:

            # Volver a localizar la celda por sus
            # atributos para evitar problemas después
            # de que la página cambie dinámicamente.

            selector = (
                "td[data-handler='selectDay']"
                f"[data-year='{year}']"
                f"[data-month='{month - 1}']"
            )

            candidate_cells = page.locator(
                selector
            )

            clicked = False

            for j in range(
                candidate_cells.count()
            ):

                candidate = (
                    candidate_cells.nth(j)
                )

                link = candidate.locator(
                    "a"
                ).first

                if link.count() == 0:
                    continue

                text_day = normalizar(
                    link.inner_text()
                )

                if text_day != str(day):
                    continue

                candidate.scroll_into_view_if_needed()

                link.click(
                    timeout=10000
                )

                clicked = True

                break

            if not clicked:

                print(
                    "  No se pudo hacer clic."
                )

                resultados.append(
                    {
                        **fecha,
                        "clicked": False,
                        "body": "",
                    }
                )

                continue

            # Esperar a que aparezca el detalle.
            page.wait_for_timeout(700)

            body = normalizar(
                page.locator("body").inner_text(
                    timeout=10000
                )
            )

            # Buscar palabras relevantes.
            upper = body.upper()

            contiene_pleno = (
                "PLENO" in upper
            )

            contiene_salud = (
                "SALUD" in upper
            )

            contiene_higiene = (
                "HIGIENE" in upper
            )

            contiene_adicciones = (
                "ADICCIONES" in upper
            )

            contiene_sesion = (
                "SESIÓN" in upper
                or "SESION" in upper
            )

            # Extraer todas las líneas que parecen
            # contener sesiones.
            lineas = [
                normalizar(line)
                for line in body.splitlines()
                if normalizar(line)
            ]

            sesiones = []

            for line in lineas:

                line_upper = line.upper()

                if (
                    "SESIÓN" in line_upper
                    or "SESION" in line_upper
                ):
                    sesiones.append(line)

            # Buscar horas.
            horas = re.findall(
                r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b",
                body,
            )

            resultado = {
                **fecha,
                "clicked": True,
                "contiene_pleno": contiene_pleno,
                "contiene_salud": contiene_salud,
                "contiene_higiene": contiene_higiene,
                "contiene_adicciones": (
                    contiene_adicciones
                ),
                "contiene_sesion": contiene_sesion,
                "horas": sorted(set(horas)),
                "sesiones": sesiones,
                "body": body,
            }

            resultados.append(resultado)

            # Mostrar en pantalla solamente lo importante.
            if (
                contiene_pleno
                or contiene_salud
                or contiene_higiene
                or contiene_adicciones
            ):

                print(
                    "  >>> EVENTO RELEVANTE"
                )

                print(
                    f"  Pleno: {contiene_pleno}"
                )

                print(
                    f"  Salud: {contiene_salud}"
                )

                print(
                    f"  Higiene: {contiene_higiene}"
                )

                print(
                    f"  Adicciones: "
                    f"{contiene_adicciones}"
                )

                print(
                    f"  Horas: "
                    f"{sorted(set(horas))}"
                )

                for sesion in sesiones:
                    print(
                        f"  SESIÓN: {sesion}"
                    )

            else:

                print(
                    "  Sin palabras objetivo."
                )

        except Exception as exc:

            print(
                f"  ERROR: {exc}"
            )

            resultados.append(
                {
                    **fecha,
                    "clicked": False,
                    "error": str(exc),
                }
            )

    # ------------------------------------------------------------
    # Guardar diagnóstico completo
    # ------------------------------------------------------------

    Path(
        "gaceta_diagnostico_completo.json"
    ).write_text(
        json.dumps(
            resultados,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # ------------------------------------------------------------
    # Archivo de texto fácil de revisar
    # ------------------------------------------------------------

    with open(
        "gaceta_diagnostico_sesiones.txt",
        "w",
        encoding="utf-8",
    ) as f:

        f.write(
            "DIAGNÓSTICO COMPLETO DE GACETA\n"
        )

        f.write(
            "=" * 80 + "\n\n"
        )

        for resultado in resultados:

            f.write(
                f"{resultado['year']}-"
                f"{resultado['month']:02d}-"
                f"{resultado['day']:02d}\n"
            )

            f.write(
                f"CLASES: "
                f"{resultado.get('classes', '')}\n"
            )

            f.write(
                f"TITLE: "
                f"{resultado.get('title', '')}\n"
            )

            f.write(
                f"PLENO: "
                f"{resultado.get('contiene_pleno', False)}\n"
            )

            f.write(
                f"SALUD: "
                f"{resultado.get('contiene_salud', False)}\n"
            )

            f.write(
                f"HIGIENE: "
                f"{resultado.get('contiene_higiene', False)}\n"
            )

            f.write(
                f"ADICCIONES: "
                f"{resultado.get('contiene_adicciones', False)}\n"
            )

            f.write(
                f"HORAS: "
                f"{resultado.get('horas', [])}\n"
            )

            f.write(
                "SESIONES:\n"
            )

            for sesion in resultado.get(
                "sesiones",
                [],
            ):
                f.write(
                    f"  - {sesion}\n"
                )

            f.write(
                "\n"
            )

            f.write(
                "-" * 80
                + "\n\n"
            )

    # ------------------------------------------------------------
    # Resumen
    # ------------------------------------------------------------

    relevantes = [
        r
        for r in resultados
        if (
            r.get("contiene_pleno")
            or r.get("contiene_salud")
            or r.get("contiene_higiene")
            or r.get("contiene_adicciones")
        )
    ]

    print("\n")
    print("=" * 70)
    print("RESUMEN DEL DIAGNÓSTICO")
    print("=" * 70)

    print(
        f"Fechas revisadas: "
        f"{len(resultados)}"
    )

    print(
        f"Fechas relevantes: "
        f"{len(relevantes)}"
    )

    print(
        "\nArchivos generados:"
    )

    print(
        "gaceta_debug.png"
    )

    print(
        "gaceta_debug.html"
    )

    print(
        "gaceta_diagnostico_completo.json"
    )

    print(
        "gaceta_diagnostico_sesiones.txt"
    )

    print("=" * 70)

    browser.close()