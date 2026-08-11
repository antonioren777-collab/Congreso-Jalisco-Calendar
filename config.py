BASE_URL = "https://www.congresojal.gob.mx"
MONTH_URL = BASE_URL + "/agenda-parlamentaria/mes/{year:04d}-{month:02d}"
OUTPUT_FILE = "calendario.ics"
TIMEZONE = "America/Mexico_City"

# El Congreso ha utilizado distintas variantes de nombre.
COMMISSION_PATTERNS = (
    "comisión de higiene y salud",
    "comisión de salud e higiene",
    "comisión de higiene, salud",
    "comisión de higiene salud",
    "comisión de salud e higiene",
)

PLENO_PATTERNS = (
    "sesión ordinaria",
    "sesión extraordinaria",
    "sesión solemne",
)

LOOK_AHEAD_MONTHS = 12
REQUEST_TIMEOUT = 30
USER_AGENT = (
    "Mozilla/5.0 (compatible; Congreso-Jalisco-Calendar/1.0; "
    "+https://github.com/)"
)
