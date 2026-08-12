from __future__ import annotations

from pathlib import Path
from playwright.sync_api import sync_playwright

from config import GACETA_BASE_URL


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(
        viewport={"width": 1600, "height": 1400},
        locale="es-MX",
    )

    print("Abriendo Gaceta para diagnóstico...")
    page.goto(
        GACETA_BASE_URL,
        wait_until="networkidle",
        timeout=60000,
    )
    page.wait_for_timeout(5000)

    page.screenshot(
        path="gaceta_debug.png",
        full_page=True,
    )

    Path("gaceta_debug.html").write_text(
        page.content(),
        encoding="utf-8",
    )

    rows = page.evaluate("""
    () => {
      const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
      return [...document.querySelectorAll('*')].map((el, i) => {
        const r = el.getBoundingClientRect();
        const cs = getComputedStyle(el);
        const attrs = {};
        for (const a of el.attributes || []) attrs[a.name] = a.value;

        return {
          i,
          tag: el.tagName,
          text: norm(el.innerText || el.textContent),
          attrs,
          background: cs.backgroundColor,
          color: cs.color,
          x: Math.round(r.left),
          y: Math.round(r.top),
          w: Math.round(r.width),
          h: Math.round(r.height)
        };
      }).filter(x => x.w > 0 && x.h > 0);
    }
    """)

    with open(
        "gaceta_dom_diagnostico.txt",
        "w",
        encoding="utf-8",
    ) as f:
        for row in rows:
            attrs = " ".join(
                f"{k}={v!r}"
                for k, v in row["attrs"].items()
            )[:700]

            f.write(
                f"INDEX={row['i']} "
                f"TAG={row['tag']} "
                f"RECT={row['x']},{row['y']},"
                f"{row['w']},{row['h']} "
                f"BG={row['background']} "
                f"COLOR={row['color']} "
                f"TEXT={row['text'][:180]!r} "
                f"ATTRS={attrs}\n"
            )

    print("Diagnóstico generado:")
    print("gaceta_debug.png")
    print("gaceta_debug.html")
    print("gaceta_dom_diagnostico.txt")
    print(f"Elementos visibles: {len(rows)}")

    browser.close()
