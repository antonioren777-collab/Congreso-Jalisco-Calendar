# ============================================================
# FUENTES OFICIALES
# ============================================================

# Gaceta Parlamentaria del Congreso de Jalisco
GACETA_BASE_URL = "https://gaceta.congresojal.gob.mx"

# Calendario oficial de sesiones 2026 LXIV
GACETA_CALENDAR_URL = (
    GACETA_BASE_URL
    + "/documentos/Calendario%20Sesiones%202026%20LXIV.pdf"
)

# Agenda de Actividades del Congreso.
# Se utiliza como fuente complementaria para obtener:
# - hora
# - nombre exacto de la sesión
# - comisión
# - ubicación
# - enlace al evento
AGENDA_BASE_URL = "https://www.congresojal.gob.mx"

MONTH_URL = (
    AGENDA_BASE_URL
    + "/agenda-parlamentaria/mes/{year:04d}-{month:02d}"
)

OUTPUT_FILE = "calendario.ics"

TIMEZONE = "America/Mexico_City"

# ============================================================
# EVENTOS QUE NOS INTERESAN
# ============================================================

PLENO_PATTERNS = (
    "pleno",
)

COMMISSION_PATTERNS = (
    "comisión de higiene, salud pública y prevención de las adicciones",
    "comision de higiene, salud publica y prevencion de las adicciones",

    "comisión de higiene, salud pública",
    "comision de higiene, salud publica",

    "comisión de higiene y salud",
    "comision de higiene y salud",

    "comisión de higiene, salud",
    "comision de higiene, salud",

    "comisión de salud",
    "comision de salud",

    "prevención de las adicciones",
    "prevencion de las adicciones",
)

# ============================================================
# CONFIGURACIÓN DEL SCRAPER
# ============================================================

# Comenzamos desde julio de 2026, que es donde quedó
# establecido nuestro calendario.
START_YEAR = 2026
START_MONTH = 7

# Revisar 12 meses hacia adelante.
LOOK_AHEAD_MONTHS = 12

REQUEST_TIMEOUT = 45

USER_AGENT = (
    "Mozilla/5.0 "
    "(compatible; Congreso-Jalisco-Calendar/3.0; "
    "+https://github.com/antonioren777-collab/"
    "Congreso-Jalisco-Calendar)"
)