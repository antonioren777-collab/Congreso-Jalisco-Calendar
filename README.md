# Calendario Congreso de Jalisco → Apple Calendar

Automatización para publicar un calendario `.ics` con **solo**:
- Sesiones del Pleno: Ordinaria, Extraordinaria y Solemne.
- Sesiones de la Comisión de Higiene, Salud y Prevención de las Adicciones, incluyendo las denominaciones que el Congreso ha usado en su agenda (por ejemplo, "Comisión de Higiene y Salud", "Comisión de Salud e Higiene" y variantes con "TELEMATICA").

La agenda se consulta diariamente a las **08:00, hora de Ciudad de México (UTC-6)** mediante GitHub Actions. GitHub puede iniciar un workflow algunos minutos después de la hora programada; por eso 08:00 debe entenderse como la hora objetivo.

## 1. Crear el repositorio

1. En GitHub crea un repositorio público, por ejemplo `Congreso-Jalisco-Calendar`.
2. Sube todos los archivos de este proyecto.
3. En **Settings → Actions → General**, permite que GitHub Actions tenga permiso para escribir en el repositorio si tu cuenta/repo lo requiere.
4. En **Settings → Pages**, selecciona **GitHub Actions** como fuente de publicación (si aparece esa opción).

## 2. Ejecutar la primera actualización

Ve a **Actions → Actualizar calendario Congreso Jalisco → Run workflow**.

El workflow:
- consulta la agenda mensual del Congreso;
- extrae los eventos;
- aplica el filtro;
- genera `calendario.ics`;
- lo publica mediante GitHub Pages.

## 3. Suscribirlo en el iPhone

Cuando GitHub Pages esté publicado, la dirección será normalmente:

`https://TU_USUARIO.github.io/TU_REPOSITORIO/calendario.ics`

En iPhone:
**Calendario → Calendarios → Añadir calendario → Añadir calendario suscrito**

Pega esa dirección y guarda.

## 4. Comportamiento

El archivo se regenera todos los días. Al estar suscrito, Apple Calendar consultará el calendario periódicamente; la actualización de Apple no necesariamente ocurre exactamente a las 08:00.

## Notas

- El scraper revisa el mes actual y los siguientes 12 meses.
- Se generan identificadores estables para evitar duplicados.
- Si el Congreso cambia la estructura de su web, puede ser necesario ajustar el scraper.
- La zona horaria de los eventos es `America/Mexico_City`.


## Corrección de agosto de 2026

La versión 2 reconoce abreviaturas de meses usadas por la agenda del Congreso (por ejemplo, `Jul`) y también la denominación abreviada `Comisión de Salud (TELEMATICA)`, que corresponde a la comisión de salud solicitada.
