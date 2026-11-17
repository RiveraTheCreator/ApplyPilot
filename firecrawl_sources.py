#!/usr/bin/env python3
"""Fuentes Firecrawl del pipeline de empleo (Fase 1): career pages nativas.

Complementa a ``scraper.py`` (JobSpy) con vacantes de Wellfound, Get on Board,
Torre, Remote OK, We Work Remotely y las páginas de carrera listadas en
``TARGET_COMPANY_PAGES``. Cada vacante se normaliza al esquema de ``vacantes``
con las mismas funciones que ``scraper.map_job`` y se guarda con
``db.insert_vacante`` (INSERT OR IGNORE: re-ejecutar no duplica filas). Después
pasa por ``filter_rank.py`` igual que cualquier otra, sin código especial.

Estrategia por fuente (se prefiere buscar a scrapear):
  1. ``firecrawl_search`` con queries ``site:`` (máximo 2 por fuente) para
     descubrir URLs de vacantes individuales.
  2. ``firecrawl_scrape`` de 1 página de listado por fuente (si la fuente tiene
     una parseable) para sacar varias vacantes de una sola llamada.
  3. ``firecrawl_scrape`` de vacantes individuales hasta ``limit`` en total por
     corrida, para la descripción completa. Solo URLs que aún no están en la
     base; las que no entran en el tope se guardan con el extracto de la
     búsqueda o del listado.
Las URLs de categoría/listado se descartan: nunca se crea una vacante a partir
de una página que no sea de vacante individual.

Firecrawl se invoca SOLO vía el helper ``custom.firecrawl/bin/client.py``
(subprocess). El helper gestiona la credencial: este módulo no la lee, no la
recibe y no la escribe en ningún sitio.

Presupuesto: ``max_credits`` por corrida y una reserva mínima de la cuenta. El
gasto se contabiliza con el ``creditsUsed`` que cada respuesta reporta y se
contrasta con ``firecrawl_credit_usage`` antes y después (el contador de la
cuenta puede tardar en reflejar el gasto; se loguea el delta tal cual).

Degradación elegante: un fallo (timeout, 429, 401/403, JSON inválido, página
sin vacantes parseables) queda en el log con su causa y la corrida sigue.
``run_firecrawl_sources`` nunca lanza excepción por un fallo de fuente.

Uso:
    python3 firecrawl_sources.py --db vacantes.db --limit 5
    python3 firecrawl_sources.py --db /tmp/prueba.db --limit 3 --sources wellfound,remoteok --dry-run

Nada se inventa: lo que la página no trae (sueldo, empresa, fecha) queda en
None / vacío.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import db  # noqa: E402
from scraper import (  # noqa: E402 - jobspy se importa perezosamente dentro de scrape_site
    detect_modalidad,
    match_keywords,
    parse_salary,
    salary_from_description,
)
from textutil import load_keywords  # noqa: E402

log = logging.getLogger("empleo.firecrawl")

LOG_DIR = BASE_DIR / "logs"
HELPER = Path.home() / "workspace" / "custom-connectors" / "custom.firecrawl" / "bin" / "client.py"
ALLOWED_TOOLS = ("firecrawl_search", "firecrawl_scrape", "firecrawl_map", "firecrawl_credit_usage")
CALL_TIMEOUT = 120  # segundos por llamada al helper
MAX_RETRIES = 1  # un reintento como mucho, y nunca tras 429 / créditos / 401 / 403

DEFAULT_LIMIT = 5  # scrapes de detalle por corrida (total, no por fuente)
DEFAULT_MAX_CREDITS = 100
DEFAULT_MIN_RESERVE = 100  # no se gasta si a la cuenta le quedan menos créditos
MAX_SEARCHES_PER_SOURCE = 2
MAX_LISTING_SCRAPES_PER_SOURCE = 1
MAX_COMPANY_PAGES_PER_RUN = 5
MAX_DESCRIPCION = db.MAX_DESCRIPCION

# Coste esperado por llamada, solo para decidir si una llamada cabe en el
# presupuesto ANTES de hacerla. Son los valores de ``creditsUsed`` observados en
# las respuestas el 2026-11-16; el gasto real se toma siempre de la respuesta.
EXPECTED_COST = {"firecrawl_search": 2, "firecrawl_scrape": 1, "firecrawl_map": 1}

# Filtro de antigüedad de las búsquedas (sintaxis tbs): "qdr:m" = último mes.
# Sin él, las búsquedas devuelven sobre todo vacantes viejas ya cerradas.
SEARCH_TBS = "qdr:m"

# Aristas 1 y 3 en la primera query; 5 y 10 en la segunda (keywords.json).
_QUERY_TERMS = (
    '("AI automation" OR "workflow automation" OR "solutions engineer" OR "implementation consultant")',
    '("implementation specialist" OR "customer onboarding" OR "low-code" OR "internal tools")',
)

# Páginas de carrera de empresas/agencias objetivo. EDITABLE: una entrada por
# página de listado. Cada entrada que falle (404, bloqueo, sin vacantes) queda
# en el log y no tumba la corrida. Sin credenciales ni URLs con token.
# Solo se reconocen como vacante los enlaces a ATS conocidos (ver _ATS_JOB_RES).
TARGET_COMPANY_PAGES: list[dict] = [
    # Verificadas el 2026-11-16 (responden 200 y listan vacantes vía Firecrawl):
    {"empresa": "n8n", "url": "https://jobs.ashbyhq.com/n8n"},  # arista 1
    {"empresa": "Deliverect", "url": "https://jobs.lever.co/deliverect"},  # arista 6
    {"empresa": "Cloudbeds", "url": "https://job-boards.greenhouse.io/cloudbeds"},  # arista 6
    # Ejemplos sin verificar con Firecrawl (descomentar tras probarlos):
    # {"empresa": "Zapier", "url": "https://jobs.ashbyhq.com/zapier"},
    # {"empresa": "Mews", "url": "https://www.mews.com/en/careers"},
]

COMPANY_SOURCE = "empresas"

# key -> configuración de la fuente. ``job_re`` se aplica a la ruta de la URL y
# define qué es una vacante individual; todo lo demás se descarta.
SOURCES: dict[str, dict] = {
    "wellfound": {
        "label": "Wellfound",
        "hosts": ("wellfound.com", "www.wellfound.com"),
        "job_re": re.compile(r"^/jobs/\d{5,}-[a-z0-9-]+$"),
        "queries": tuple(f"site:wellfound.com/jobs remote {t}" for t in _QUERY_TERMS),
        "listing_url": None,  # los listados /role/ no traen título y empresa separables
        "remote_only": False,
        "site_suffixes": ("Wellfound",),
    },
    "getonboard": {
        "label": "Get on Board",
        "hosts": ("www.getonbrd.com", "getonbrd.com", "www.getonboard.com", "getonboard.com"),
        "job_re": re.compile(r"^/(?:jobs|empleos)/[a-z0-9-]+/[a-z0-9-]+$"),
        "queries": tuple(f"site:getonbrd.com/jobs remote {t}" for t in _QUERY_TERMS),
        "listing_url": "https://www.getonbrd.com/jobs/customer-support",
        "remote_only": False,
        "site_suffixes": ("Get on Board",),
    },
    "torre": {
        "label": "Torre",
        "hosts": ("torre.ai", "www.torre.ai", "torre.co", "www.torre.co"),
        "job_re": re.compile(r"^/(?:jobs|post)/[A-Za-z0-9]{6,12}(?:-[A-Za-z0-9-]+)?$"),
        "queries": tuple(f"site:torre.ai remote {t}" for t in _QUERY_TERMS),
        "listing_url": "https://torre.ai/sub/remote-latam-saas/jobs",
        "remote_only": False,
        "site_suffixes": ("Torre", "Torre.ai"),
    },
    "remoteok": {
        "label": "Remote OK",
        "hosts": ("remoteok.com", "www.remoteok.com", "remoteok.io"),
        "job_re": re.compile(r"^/remote-jobs/[a-z0-9-]+-\d{5,}$"),
        "queries": tuple(f"site:remoteok.com/remote-jobs {t}" for t in _QUERY_TERMS),
        "listing_url": None,  # el listado llega sin enlaces a vacantes (se pinta con JS)
        "remote_only": True,  # el portal solo publica empleo remoto
        "site_suffixes": ("Remote OK",),
        # En la vacante probada coincide con la antigüedad que muestra la página.
        "date_meta_keys": ("og:updated_time",),
    },
    "weworkremotely": {
        "label": "We Work Remotely",
        "hosts": ("weworkremotely.com", "www.weworkremotely.com"),
        "job_re": re.compile(r"^/remote-jobs/(?!search$|new$)[a-z0-9][a-z0-9-]*$"),
        "queries": tuple(f"site:weworkremotely.com/remote-jobs {t}" for t in _QUERY_TERMS),
        "listing_url": "https://weworkremotely.com/remote-jobs/search?term=automation",
        "remote_only": True,
        "site_suffixes": ("We Work Remotely",),
    },
}
SOURCE_ALIASES = {
    "getonbrd": "getonboard", "get-on-board": "getonboard", "wwr": "weworkremotely",
    "remote-ok": "remoteok", "torre.ai": "torre", "empresa": COMPANY_SOURCE,
    "companies": COMPANY_SOURCE,
}
ALL_SOURCES = (*SOURCES, COMPANY_SOURCE)

# Enlaces a vacantes individuales en ATS conocidos (para TARGET_COMPANY_PAGES).
_ATS_JOB_RES = (
    re.compile(r"^https://jobs\.ashbyhq\.com/[^/]+/[0-9a-f]{8}-[0-9a-f-]{27}$"),
    re.compile(r"^https://jobs\.(?:eu\.)?lever\.co/[^/]+/[0-9a-f]{8}-[0-9a-f-]{27}$"),
    re.compile(r"^https://(?:job-)?boards\.(?:eu\.)?greenhouse\.io/[^/]+/jobs/\d+$"),
    re.compile(r"^https://apply\.workable\.com/[^/]+/j/[A-Z0-9]+$"),
)

# --------------------------------------------------------------------------
# Helper de Firecrawl (subprocess)
# --------------------------------------------------------------------------

_SECRET_RE = re.compile(r"(?i)\b(?:fc-[a-z0-9]{8,}|hsurr:\S+|bearer\s+\S+)")
_HTTP_RE = re.compile(r"HTTP (\d{3})")
_RATE_RE = re.compile(r"(?i)\b429\b|rate.?limit|too many requests")
_CREDITS_RE = re.compile(r"(?i)insufficient credits|out of credits|credits? (?:exhausted|ran out)"
                         r"|payment required|\b402\b")

# Tipos de fallo que cortan la corrida entera (reintentar no sirve de nada).
FATAL_KINDS = ("rate_limit", "credits", "auth", "helper")


def _safe(text: str | None, limit: int = 300) -> str:
    """Texto apto para log: una línea, recortado y sin nada con pinta de credencial."""
    flat = " ".join((text or "").split())
    return _SECRET_RE.sub("<redactado>", flat)[:limit]


def _call_once(name: str, arguments: dict, timeout: int) -> tuple[dict | None, str | None, str]:
    """Una llamada al helper. Devuelve (payload, error, tipo de fallo)."""
    fd, tmp_path = tempfile.mkstemp(prefix="fc-args-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(arguments, handle, ensure_ascii=False)
        proc = subprocess.run(
            ["python3", str(HELPER), "call-tool", "--name", name, "--arguments-file", tmp_path],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, f"timeout de {timeout} s", "timeout"
    except OSError as exc:
        return None, f"no se pudo ejecutar el helper ({type(exc).__name__})", "helper"
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    result = None
    if proc.stdout.strip():
        try:
            result = json.loads(proc.stdout)
        except ValueError:
            result = None
    if isinstance(result, dict) and result.get("isError"):
        content = result.get("content") or [{}]
        text = _safe(content[0].get("text") if isinstance(content[0], dict) else "")
        if _RATE_RE.search(text):
            return None, f"429 / rate limit: {text}", "rate_limit"
        if _CREDITS_RE.search(text):
            return None, f"créditos agotados: {text}", "credits"
        return None, f"error de la herramienta: {text}", "tool_error"
    if proc.returncode != 0:
        stderr = _safe(proc.stderr) or f"código de salida {proc.returncode}"
        status = _HTTP_RE.search(stderr)
        code = int(status.group(1)) if status else None
        if code == 429:
            return None, stderr, "rate_limit"
        if code == 402:
            return None, stderr, "credits"
        if code in (401, 403):
            return None, stderr, "auth"
        return None, stderr, "transport"
    if not isinstance(result, dict):
        return None, "el helper no devolvió JSON válido", "bad_json"
    payload = result.get("structuredContent")
    if not isinstance(payload, dict):
        try:
            payload = json.loads(result["content"][0]["text"])
        except (KeyError, IndexError, TypeError, ValueError):
            return None, "respuesta sin structuredContent ni JSON en content[0].text", "bad_json"
    if not isinstance(payload, dict):
        return None, "la respuesta no es un objeto JSON", "bad_json"
    return payload, None, "ok"


def call_tool(name: str, arguments: dict, timeout: int = CALL_TIMEOUT):
    """Llama a una herramienta de Firecrawl vía el helper; nunca lanza excepción.

    Devuelve ``(payload, error, tipo)``. Reintenta UNA vez solo los fallos
    transitorios (timeout, transporte, JSON inválido). Un 429, créditos
    agotados, 401/403 o un error de la propia herramienta no se reintentan.
    """
    if name not in ALLOWED_TOOLS:
        return None, f"herramienta no permitida: {name}", "helper"
    if not HELPER.is_file():
        return None, f"no existe el helper de Firecrawl ({HELPER.name})", "helper"
    payload, error, kind = _call_once(name, arguments, timeout)
    retries = 0
    while error and kind in ("timeout", "transport", "bad_json") and retries < MAX_RETRIES:
        retries += 1
        log.warning("FALLO %s (%s): %s | reintento %d/%d", name, kind, error, retries, MAX_RETRIES)
        payload, error, kind = _call_once(name, arguments, timeout)
    return payload, error, kind


def get_credits() -> tuple[int | None, str | None]:
    """Créditos restantes de la cuenta según ``firecrawl_credit_usage``."""
    payload, error, _kind = call_tool("firecrawl_credit_usage", {})
    if error:
        return None, error
    remaining = payload.get("remainingCredits")
    if isinstance(remaining, bool) or not isinstance(remaining, (int, float)):
        return None, "firecrawl_credit_usage no trae remainingCredits"
    return int(remaining), None


# --------------------------------------------------------------------------
# Texto / markdown
# --------------------------------------------------------------------------

_IMG_RE = re.compile(r"!\[([^\]]*)\]\((?:[^()\s]|\([^)]*\))*(?:\s+\"[^\"]*\")?\)")
_LINK_RE = re.compile(r"\[([^\[\]]*)\]\((?:[^()\s]|\([^)]*\))*(?:\s+\"[^\"]*\")?\)")
_MD_ESCAPE_RE = re.compile(r"\\([\\|_*#\-.()\[\]&~>`!+])")
_ALT_OPEN, _ALT_CLOSE = "\x00", "\x01"
_CARD_LINK_RE = re.compile(r"\]\((https?://[^)\s]+)(?:\s+\"[^\"]*\")?\)")
_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.M)
_END_MARKERS_RE = re.compile(
    r"^(?:#{1,4}\s*)?(?:similar jobs|more jobs at\b.*|related jobs|view all jobs\b.*"
    r"|other jobs\b.*|remote jobs you might like|empleos similares|apply for this job)\s*$",
    re.I | re.M,
)
_CLOSED_RE = re.compile(
    r"no longer (?:available|accepting|open|active)|(?:position|role|job) has been filled"
    r"|this job (?:post |posting |listing )?(?:is|has been) (?:closed|expired|removed)"
    r"|job (?:post|posting|listing) (?:is|has) (?:closed|expired)"
    r"|(?:vacante|oferta|empleo) (?:cerrad[ao]|ya no esta disponible|no esta disponible)",
    re.I,
)
_JUNK_SNIPPET_RE = re.compile(r"^\s*\|\s*!\[|Sort by.{0,40}Latest jobs", re.S)
_TRUNCATED_RE = re.compile(r"\s*(?:\.\.\.|…)\s*$")
_GENERIC_ANCHORS = {"apply", "apply now", "view job", "view details", "learn more", "details",
                    "ver vacante", "postular", "aplicar"}


def strip_symbols(text: str | None) -> str:
    """Quita emojis y símbolos decorativos y colapsa espacios."""
    if not text:
        return ""
    kept = [
        c for c in html.unescape(str(text))
        if unicodedata.category(c) not in ("So", "Sk", "Cs", "Co", "Cn", "Cf")
        and not 0xFE00 <= ord(c) <= 0xFE0F
    ]
    return re.sub(r"\s+", " ", "".join(kept)).strip()


def clean_markdown(text: str | None) -> str:
    """Markdown -> texto legible: sin imágenes, sin URLs de enlaces, sin escapes."""
    if not text:
        return ""
    text = _IMG_RE.sub("", str(text))
    for _ in range(2):  # enlaces anidados tras quitar la imagen
        text = _LINK_RE.sub(r"\1", text)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"\\+[ \t]*$", "", text, flags=re.M)
    text = _MD_ESCAPE_RE.sub(r"\1", text)
    text = re.sub(r"\*\*|__", "", text)  # negritas
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def normalize_url(url: str | None) -> str:
    """URL canónica para deduplicar: https, host en minúsculas, sin query ni fragmento."""
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    path = parts.path.rstrip("/")
    host = parts.hostname.lower()
    if host.endswith("getonbrd.com") and path.startswith("/empleos/"):
        path = "/jobs/" + path[len("/empleos/"):]  # misma vacante en español e inglés
    return f"https://{host}{path}"


def classify_url(source_key: str, url: str) -> str:
    """'vacante' (individual), 'listado' (categoría/búsqueda) o 'ajena' (otro sitio)."""
    canonical = normalize_url(url)
    if not canonical:
        return "ajena"
    if source_key == COMPANY_SOURCE:
        return "vacante" if any(p.match(canonical) for p in _ATS_JOB_RES) else "listado"
    source = SOURCES[source_key]
    parts = urlsplit(canonical)
    if parts.hostname not in source["hosts"]:
        return "ajena"
    return "vacante" if source["job_re"].match(parts.path) else "listado"


# --------------------------------------------------------------------------
# Fechas
# --------------------------------------------------------------------------

_MONTHS = {
    "jan": 1, "ene": 1, "feb": 2, "mar": 3, "apr": 4, "abr": 4, "may": 5, "jun": 6, "jul": 7,
    "aug": 8, "ago": 8, "sep": 9, "set": 9, "oct": 10, "nov": 11, "dec": 12, "dic": 12,
}
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_FULL_DATE_LINE_RE = re.compile(r"^\s*([A-Za-z]{3,9})\.? (\d{1,2}), (\d{4})\s*$", re.M)
_RELATIVE_RE = re.compile(
    r"(?:posted|published|publicad[ao])\s*:?\s*(?:hace\s+)?(\d+|an?|un[ao]?)\s+"
    r"(hour|hora|day|dia|week|semana|month|mes)(?:e?s)?(?:\s+ago)?", re.I)
_UNIT_DAYS = {"hour": 0, "hora": 0, "day": 1, "dia": 1, "week": 7, "semana": 7, "month": 30, "mes": 30}


def _valid_date(year: int, month: int, day: int, today: date) -> str | None:
    try:
        value = date(year, month, day)
    except ValueError:
        return None
    return value.isoformat() if date(2015, 1, 1) <= value <= today else None


def parse_short_date(text: str, today: date) -> str | None:
    """'oct 21' -> la fecha más reciente no futura con ese mes y día."""
    match = re.fullmatch(r"\s*([A-Za-z]{3,4})\.? (\d{1,2})\s*", text or "")
    if not match or match.group(1).lower()[:3] not in _MONTHS:
        return None
    month, day = _MONTHS[match.group(1).lower()[:3]], int(match.group(2))
    return _valid_date(today.year, month, day, today) or _valid_date(today.year - 1, month, day, today)


def extract_date(markdown: str, metadata: dict, meta_keys: tuple[str, ...], today: date) -> str | None:
    """Fecha de publicación YYYY-MM-DD, o None si la página no la da.

    Orden: metadatos de publicación, fecha completa en una línea propia y
    antigüedad relativa ("Posted: 4 weeks ago"; semanas y meses son aproximados).
    """
    for key in ("article:published_time", "publishedTime", "datePosted", *meta_keys):
        value = metadata.get(key)
        if isinstance(value, str) and (m := _ISO_DATE_RE.match(value)):
            parsed = _valid_date(int(m.group(1)), int(m.group(2)), int(m.group(3)), today)
            if parsed:
                return parsed
    for m in _FULL_DATE_LINE_RE.finditer(markdown):
        month = _MONTHS.get(m.group(1).lower()[:3])
        if month and (parsed := _valid_date(int(m.group(3)), month, int(m.group(2)), today)):
            return parsed
    plain = unicodedata.normalize("NFKD", markdown).encode("ascii", "ignore").decode()
    if m := _RELATIVE_RE.search(plain):
        amount = int(m.group(1)) if m.group(1).isdigit() else 1
        return (today - timedelta(days=amount * _UNIT_DAYS[m.group(2).lower()])).isoformat()
    return None


# --------------------------------------------------------------------------
# Candidatas: resultados de búsqueda, tarjetas de listado y detalle
# --------------------------------------------------------------------------

_SALARY_LINE_RE = re.compile(r"(?i)(?:usd|mxn|us\$|\$|eur|€)")
_CODE_DIGIT_RE = re.compile(r"(?i)\b(usd|mxn|eur)(?=\d)")


def _new_candidate(source_key: str, url: str, origen: str) -> dict:
    return {
        "source": source_key, "url": normalize_url(url), "origen": origen,
        "titulo": "", "empresa": "", "ubicacion": None, "sueldo": None,
        "fecha_publicacion": None, "descripcion": "", "detalle": False,
    }


def _strip_site_suffix(title: str, suffixes: tuple[str, ...]) -> str:
    for suffix in suffixes:
        title = re.sub(rf"\s*[|\-–]\s*{re.escape(suffix)}\s*$", "", title, flags=re.I)
    return title.strip()


def split_title_company(raw_title: str, source_key: str) -> tuple[str, str]:
    """'Rol at Empresa • Ciudad | Sitio' -> (rol, empresa). Empresa '' si no viene."""
    source = SOURCES.get(source_key, {})
    title = _strip_site_suffix(strip_symbols(raw_title), source.get("site_suffixes", ()))
    truncated = bool(_TRUNCATED_RE.search(title))
    title = _TRUNCATED_RE.sub("", title)
    title = re.sub(r"\s+at$", "", title.split(" • ")[0].strip())
    empresa = ""
    if " at " in title:
        head, tail = title.rsplit(" at ", 1)
        tail = re.split(r"\s+[-–]\s+Remote\b", tail)[0].strip(" .-–")
        # Un título cortado ("... at Apollo ...") puede traer la empresa a medias: se omite.
        if head.strip() and not truncated:
            empresa = tail
        title = head.strip() or title
    if source.get("remote_only"):
        title = re.sub(r"^Remote\s+", "", title)
    title = re.sub(r"\s*\(\s*~?\$[^)]*\)\s*$", "", title)  # "(~$84k, USA)" de Remote OK
    return title.strip(" -–|"), empresa


def candidate_from_search(source_key: str, item: dict) -> dict | None:
    """Convierte un resultado de ``firecrawl_search`` en candidata (o None si no hay título)."""
    cand = _new_candidate(source_key, item.get("url", ""), "busqueda")
    raw_desc = item.get("description") or ""
    if _JUNK_SNIPPET_RE.search(raw_desc):
        raw_desc = ""  # extracto que solo trae la navegación del sitio
    titulo, empresa = split_title_company(item.get("title") or "", source_key)
    heading = _H1_RE.search(raw_desc)
    if heading:
        h1 = strip_symbols(clean_markdown(heading.group(1)))
        if source_key == "getonboard" and (m := re.match(r"^(.+?)\s+in\s+(.+)$", h1)):
            titulo, empresa = m.group(1).strip(), empresa or m.group(2).strip()
        elif h1 and (not titulo or _TRUNCATED_RE.search(item.get("title") or "")):
            titulo = h1
    if source_key == "remoteok" and not empresa:
        if m := re.match(r"^([^.|\n]{2,80}?) is hiring an? ", raw_desc):
            empresa = strip_symbols(m.group(1))
    if empresa and titulo.lower().endswith(f" - {empresa.lower()}"):
        titulo = titulo[: -len(empresa) - 3].strip()  # "Rol - Empresa" de Remote OK
    if not titulo:
        return None
    cand["titulo"], cand["empresa"] = titulo, empresa
    cand["descripcion"] = strip_symbols_keep_lines(clean_markdown(raw_desc))
    flat = " ".join(cand["descripcion"].split())
    if m := re.search(r"(?i:remote) \(\s*([A-Z][\w ,/&-]{1,60}?)\s*\)", flat):
        cand["ubicacion"] = f"Remote ({m.group(1).strip()})"
    return cand


def strip_symbols_keep_lines(text: str) -> str:
    """Como :func:`strip_symbols` pero conservando los saltos de línea."""
    lines = [strip_symbols(line) for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _card_lines(raw: str) -> list[str]:
    text = re.sub(rf"{_ALT_OPEN}[^{_ALT_CLOSE}]*{_ALT_CLOSE}", "", raw)
    lines = []
    for line in text.splitlines():
        line = strip_symbols(_MD_ESCAPE_RE.sub(r"\1", line.replace("\\\\", " ")).replace("\\", " "))
        line = line.replace("*", "").strip(" #·")
        if line:
            lines.append(line)
    return lines


def candidate_from_card(source_key: str, url: str, raw: str, today: date,
                        empresa_fija: str = "") -> dict | None:
    """Convierte el texto de un enlace de listado en candidata.

    Si de la tarjeta no sale un título fiable devuelve None: nunca se rellena.
    """
    cand = _new_candidate(source_key, url, "listado")
    alts = [strip_symbols(a) for a in re.findall(rf"{_ALT_OPEN}([^{_ALT_CLOSE}]*){_ALT_CLOSE}", raw)]
    alts = [a for a in alts if a and a.lower() not in ("image", "logo", "avatar")]
    bold = re.search(r"\*\*+\s*([^*\n]+?)\s*\*\*", raw)
    lines = _card_lines(raw)
    if not lines:
        return None
    titulo = strip_symbols(_MD_ESCAPE_RE.sub(r"\1", bold.group(1))) if bold else lines[0]
    if not titulo or titulo.lower() in _GENERIC_ANCHORS or len(titulo) > 160:
        return None
    rest = [line for line in lines if line != titulo and not line.startswith(titulo)]
    empresa = empresa_fija or (alts[0] if alts else "")
    ubicacion = None

    salary_idx = next((i for i, line in enumerate(rest)
                       if _SALARY_LINE_RE.search(line) and re.search(r"\d", line)), None)
    if salary_idx is not None:
        cand["sueldo"] = _CODE_DIGIT_RE.sub(r"\1 ", rest[salary_idx].lstrip("~"))

    if source_key == "weworkremotely":
        hq_idx = next((i for i, line in enumerate(rest) if line.startswith("HQ:")), None)
        if hq_idx:
            empresa = rest[hq_idx - 1]
        region = next((line for line in rest if re.search(r"(?i)\bonly\b|anywhere|worldwide", line)), "")
        ubicacion = f"Remote ({region})" if region else "Remote"
        for line in rest:
            if m := re.fullmatch(r"(\d{1,3})d", line):
                cand["fecha_publicacion"] = (today - timedelta(days=int(m.group(1)))).isoformat()
                break
    elif source_key == "getonboard":
        comp_idx = next((i for i, line in enumerate(rest) if empresa and line == empresa), None)
        if comp_idx is not None:
            end = salary_idx if salary_idx is not None and salary_idx > comp_idx else len(rest)
            where = [line for line in rest[comp_idx + 1:end] if not parse_short_date(line, today)]
            ubicacion = " ".join(where)[:200] or None
        if rest:
            cand["fecha_publicacion"] = parse_short_date(rest[-1], today)
    elif source_key == "torre":
        if rest and len(rest[-1]) <= 60 and not _SALARY_LINE_RE.search(rest[-1]):
            ubicacion = rest[-1]
    else:
        ubicacion = " | ".join(rest[:3])[:200] or None

    cand["titulo"], cand["empresa"], cand["ubicacion"] = titulo, empresa, ubicacion
    cand["descripcion"] = "\n".join(lines)
    return cand


def extract_cards(markdown: str, source_key: str, today: date, empresa_fija: str = ""):
    """Vacantes individuales enlazadas en una página de listado.

    Devuelve (candidatas, enlaces a vacante sin título utilizable).
    """
    text = _IMG_RE.sub(lambda m: f"{_ALT_OPEN}{m.group(1)}{_ALT_CLOSE}", markdown)
    candidates: dict[str, dict] = {}
    unusable = 0
    prev_end = 0
    for match in _CARD_LINK_RE.finditer(text):
        segment = text[prev_end:match.start()]
        prev_end = match.end()
        if classify_url(source_key, match.group(1)) != "vacante":
            continue
        url = normalize_url(match.group(1))
        blank = list(re.finditer(r"\n[ \t]*\n", segment))
        if blank:
            segment = segment[blank[-1].end():]
        start = segment.find("[")
        raw = segment[start + 1:] if start >= 0 else segment
        cand = candidate_from_card(source_key, url, raw, today, empresa_fija)
        if cand is None:
            if url not in candidates:
                unusable += 1
            continue
        if url not in candidates:
            candidates[url] = cand
    return list(candidates.values()), unusable


def merge_candidate(base: dict, extra: dict) -> None:
    """Completa ``base`` con lo que traiga ``extra`` (misma URL); no pisa datos."""
    for field in ("empresa", "ubicacion", "sueldo", "fecha_publicacion"):
        if not base.get(field) and extra.get(field):
            base[field] = extra[field]
    if len(extra.get("descripcion") or "") > len(base.get("descripcion") or ""):
        base["descripcion"] = extra["descripcion"]


def apply_detail(cand: dict, payload: dict, today: date) -> str | None:
    """Enriquece la candidata con el scrape de su página. Devuelve motivo de descarte o None."""
    markdown = payload.get("markdown") or ""
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    source = SOURCES.get(cand["source"], {})
    if not markdown.strip():
        return "página sin contenido"
    if _CLOSED_RE.search(unicodedata.normalize("NFKD", markdown).encode("ascii", "ignore").decode()):
        return "vacante cerrada según la propia página"

    meta_title = metadata.get("ogTitle") or metadata.get("og:title") or metadata.get("title") or ""
    if isinstance(meta_title, list):
        meta_title = meta_title[0] if meta_title else ""
    titulo, empresa = split_title_company(str(meta_title), cand["source"])
    heading = _H1_RE.search(markdown)
    body = markdown
    if heading:
        h1 = strip_symbols(clean_markdown(heading.group(1)))
        if cand["source"] == "getonboard" and (m := re.match(r"^(.+?)\s+in\s+(.+)$", h1)):
            h1, empresa = m.group(1).strip(), empresa or m.group(2).strip()
        titulo = h1 or titulo
        body = markdown[heading.start():]
    if end := _END_MARKERS_RE.search(body, 1):
        body = body[:end.start()]
    body = strip_symbols_keep_lines(clean_markdown(body))
    if not heading or len(body) < 200:
        # Página sin cuerpo reconocible: se usa solo la descripción de sus metadatos.
        meta_desc = metadata.get("ogDescription") or metadata.get("description") or ""
        body = strip_symbols(meta_desc if isinstance(meta_desc, str) else "")
        if len(body) < len(cand["descripcion"]):
            body = cand["descripcion"]
    if titulo and not cand.get("titulo_fijo"):
        cand["titulo"] = titulo
    if cand["source"] != COMPANY_SOURCE and empresa:
        cand["empresa"] = empresa
    cand["descripcion"] = body
    cand["detalle"] = True

    flat = " ".join(body.split())
    if m := re.search(r"Hires remotely in ([A-Z][\w ,/&-]{1,60}?)(?= Visa| Relocation| Skills|$)", flat):
        cand["ubicacion"] = f"Remote ({m.group(1).strip()})"
    elif cand["source"] == "getonboard" and (m := re.search(r"^##\s+(.+?)\s*\\?\|", markdown, re.M)):
        cand["ubicacion"] = strip_symbols(clean_markdown(m.group(1)))[:200] or cand["ubicacion"]
    fecha = extract_date(markdown, metadata, source.get("date_meta_keys", ()), today)
    if fecha:
        cand["fecha_publicacion"] = fecha
    return None


def to_vacante(cand: dict, aristas: list[dict], now: str) -> dict:
    """Candidata -> dict con las columnas de ``vacantes`` (igual que ``scraper.map_job``)."""
    descripcion = (cand.get("descripcion") or "")[:MAX_DESCRIPCION] or None
    ubicacion = cand.get("ubicacion")
    if not ubicacion and SOURCES.get(cand["source"], {}).get("remote_only"):
        ubicacion = "Remote"  # portal exclusivamente remoto
    sueldo = cand.get("sueldo") or salary_from_description(descripcion)
    sueldo_min, sueldo_max = parse_salary(sueldo)
    matched_ids, matched_keywords = match_keywords(cand["titulo"], descripcion, aristas)
    return {
        "titulo": cand["titulo"],
        "empresa": cand.get("empresa") or "",
        "ubicacion": ubicacion,
        "modalidad": detect_modalidad(cand["titulo"], descripcion, ubicacion),
        "sueldo": sueldo,
        "sueldo_min_mxn": sueldo_min,
        "sueldo_max_mxn": sueldo_max,
        "url": cand["url"],
        "fecha_publicacion": cand.get("fecha_publicacion"),
        "fuente": f"firecrawl:{cand['source']}",
        "aristas": json.dumps(sorted(matched_ids)),
        "keywords_matched": json.dumps(matched_keywords, ensure_ascii=False),
        "descripcion": descripcion,
        "fecha_scrapeo": now,
    }


def _merge_aristas(conn, vacante: dict) -> None:
    """Si la vacante ya existía, añade a su lista las aristas de esta corrida.

    Mismo patrón que ``scraper._merge_aristas``; se busca por URL entre las
    filas de Firecrawl porque título y empresa pueden variar entre una búsqueda
    y el detalle de la misma vacante.
    """
    row = conn.execute(
        "SELECT id, aristas FROM vacantes WHERE url = ? AND fuente LIKE 'firecrawl:%'",
        (vacante["url"],),
    ).fetchone()
    if not row:
        return
    current = set(json.loads(row["aristas"] or "[]"))
    merged = current | set(json.loads(vacante["aristas"]))
    if merged != current:
        conn.execute(
            "UPDATE vacantes SET aristas = ? WHERE id = ?", (json.dumps(sorted(merged)), row["id"])
        )


# --------------------------------------------------------------------------
# Corrida
# --------------------------------------------------------------------------


def parse_sources_arg(value: str | None) -> list[str] | None:
    """'wellfound,remoteok' -> ['wellfound', 'remoteok']; None o vacío -> None (todas)."""
    names = [part.strip().lower() for part in (value or "").split(",") if part.strip()]
    return [SOURCE_ALIASES.get(name, name) for name in names] or None


def _resolve_keywords_file(keywords_file: str) -> Path:
    path = Path(keywords_file)
    if not path.is_file() and not path.is_absolute() and (BASE_DIR / path).is_file():
        return BASE_DIR / path
    return path


class _Run:
    """Estado de una corrida: presupuesto, estadísticas y corte por fallo fatal."""

    def __init__(self, sources: list[str], max_credits: int, min_reserve: int) -> None:
        self.max_credits = max_credits
        self.min_reserve = min_reserve
        self.remaining: int | None = None
        self.spent = 0
        self.stopped: str | None = None
        self.stats = {
            "per_source": {
                s: {"searches_ok": 0, "searches_failed": 0, "scrapes_ok": 0, "scrapes_failed": 0,
                    "found": 0, "inserted": 0}
                for s in sources
            },
            "failures": [],
            "inserted": 0,
            "duplicates": 0,
            "discarded_urls": 0,
            "credits_before": None,
            "credits_after": None,
            "credits_used": None,
            "credits_reported": 0,
            "stopped": None,
            "dry_run": False,
            "total_failure": False,
        }

    def fail(self, source: str, step: str, target: str, error: str) -> None:
        self.stats["failures"].append(
            {"source": source, "step": step, "target": target, "error": _safe(error)})
        log.warning("FALLO %s | %s | %s | %s", source, step, target, _safe(error))

    def stop(self, reason: str) -> None:
        if not self.stopped:
            self.stopped = reason
            self.stats["stopped"] = reason
            log.error("Firecrawl se detiene: %s", reason)

    def can_spend(self, tool: str) -> bool:
        """True si la llamada cabe en el presupuesto de la corrida y en la reserva."""
        if self.stopped:
            return False
        cost = EXPECTED_COST.get(tool, 1)
        if self.spent + cost > self.max_credits:
            self.stop(f"presupuesto de la corrida agotado ({self.spent}/{self.max_credits} créditos)")
            return False
        if self.remaining is not None and self.remaining - self.spent - cost < self.min_reserve:
            self.stop(f"reserva mínima alcanzada (quedan ~{self.remaining - self.spent} créditos, "
                      f"reserva {self.min_reserve})")
            return False
        return True

    def call(self, source: str, step: str, tool: str, arguments: dict, target: str):
        """Llamada con presupuesto y registro de fallos. Devuelve el payload o None."""
        if not self.can_spend(tool):
            return None
        payload, error, kind = call_tool(tool, arguments)
        counter = "searches" if tool == "firecrawl_search" else "scrapes"
        site = self.stats["per_source"][source]
        if error:
            site[f"{counter}_failed"] += 1
            self.fail(source, step, target, f"{kind}: {error}")
            if kind in FATAL_KINDS:
                self.stop(f"{kind}: {_safe(error, 160)}")
            return None
        used = payload.get("creditsUsed")
        if used is None and isinstance(payload.get("metadata"), dict):
            used = payload["metadata"].get("creditsUsed")
        if isinstance(used, bool) or not isinstance(used, (int, float)):
            used = EXPECTED_COST.get(tool, 1)
            log.info("%s no reportó creditsUsed; se cuenta el coste esperado (%d)", tool, used)
        self.spent += int(used)
        self.stats["credits_reported"] = self.spent
        if tool == "firecrawl_scrape":
            status = (payload.get("metadata") or {}).get("statusCode")
            if isinstance(status, int) and status >= 400:
                site["scrapes_failed"] += 1
                self.fail(source, step, target, f"la página respondió HTTP {status}")
                return None
        site[f"{counter}_ok"] += 1
        return payload


def _discover(run: _Run, source_key: str, today: date) -> dict[str, dict]:
    """Búsquedas + listado de una fuente. Devuelve candidatas por URL."""
    source = SOURCES[source_key]
    found: dict[str, dict] = {}

    def add(cand: dict) -> None:
        if cand["url"] in found:
            merge_candidate(found[cand["url"]], cand)
        else:
            found[cand["url"]] = cand

    for query in source["queries"][:MAX_SEARCHES_PER_SOURCE]:
        arguments = {
            "query": query,
            "objective": f"Encontrar vacantes individuales de empleo remoto publicadas en {source['label']}",
        }
        if SEARCH_TBS:
            arguments["tbs"] = SEARCH_TBS
        payload = run.call(source_key, "busqueda", "firecrawl_search", arguments, query)
        if run.stopped:
            return found
        if payload is None:
            continue
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        results = data.get("web") if isinstance(data.get("web"), list) else []
        kept = 0
        for item in results:
            if not isinstance(item, dict) or not item.get("url"):
                continue
            kind = classify_url(source_key, item["url"])
            if kind != "vacante":
                run.stats["discarded_urls"] += 1
                log.info("DESCARTE %s | %s | %s", source_key,
                         "página de categoría/listado" if kind == "listado" else "otro sitio",
                         _safe(item["url"], 200))
                continue
            cand = candidate_from_search(source_key, item)
            if cand is None:
                run.stats["discarded_urls"] += 1
                log.info("DESCARTE %s | sin título utilizable | %s", source_key, _safe(item["url"], 200))
                continue
            add(cand)
            kept += 1
        log.info("%s | búsqueda %r | %d resultados, %d vacantes individuales",
                 source_key, query, len(results), kept)
        if not results:
            run.fail(source_key, "busqueda", query, "la búsqueda no devolvió resultados")

    listing = source.get("listing_url")
    if listing and MAX_LISTING_SCRAPES_PER_SOURCE > 0:
        payload = run.call(
            source_key, "listado", "firecrawl_scrape",
            {"url": listing, "objective": f"Listar las vacantes remotas publicadas en {source['label']}"},
            listing,
        )
        if payload is not None:
            cards, unusable = extract_cards(payload.get("markdown") or "", source_key, today)
            for cand in cards:
                add(cand)
            log.info("%s | listado %s | %d vacantes individuales, %d enlaces sin título utilizable",
                     source_key, listing, len(cards), unusable)
            if not cards:
                run.fail(source_key, "listado", listing, "listado sin vacantes parseables")
    return found


def _discover_companies(run: _Run, today: date) -> dict[str, dict]:
    """Un scrape de listado por cada página de ``TARGET_COMPANY_PAGES``."""
    found: dict[str, dict] = {}
    for entry in TARGET_COMPANY_PAGES[:MAX_COMPANY_PAGES_PER_RUN]:
        if run.stopped:
            break
        empresa, url = (entry.get("empresa") or "").strip(), (entry.get("url") or "").strip()
        if not empresa or not normalize_url(url):
            run.fail(COMPANY_SOURCE, "listado", _safe(url, 200) or "(sin url)", "entrada inválida")
            continue
        payload = run.call(
            COMPANY_SOURCE, "listado", "firecrawl_scrape",
            {"url": url, "objective": f"Listar las vacantes abiertas de {empresa}"}, url,
        )
        if payload is None:
            continue
        cards, unusable = extract_cards(payload.get("markdown") or "", COMPANY_SOURCE, today, empresa)
        for cand in cards:
            cand["titulo_fijo"] = True  # el título del listado es el del ATS
            found.setdefault(cand["url"], cand)
        log.info("%s | %s | %s | %d vacantes individuales, %d enlaces sin título utilizable",
                 COMPANY_SOURCE, empresa, url, len(cards), unusable)
        if not cards:
            run.fail(COMPANY_SOURCE, "listado", url, "página de carrera sin vacantes parseables")
    return found


def _pick_for_detail(per_source: dict[str, list[dict]], limit: int) -> list[dict]:
    """Reparte el tope de scrapes de detalle entre fuentes (round-robin por relevancia)."""
    queues = {key: list(items) for key, items in per_source.items() if items}
    picked: list[dict] = []
    while queues and len(picked) < limit:
        for key in list(queues):
            if len(picked) >= limit:
                break
            picked.append(queues[key].pop(0))
            if not queues[key]:
                del queues[key]
    return picked


def run_firecrawl_sources(
    db_path,
    limit: int = DEFAULT_LIMIT,
    sources: list[str] | None = None,
    max_credits: int = DEFAULT_MAX_CREDITS,
    keywords_file: str = "keywords.json",
    dry_run: bool = False,
    min_reserve: int = DEFAULT_MIN_RESERVE,
) -> dict:
    """Descubre vacantes vía Firecrawl y las guarda en SQLite. Nunca lanza por un fallo de fuente.

    ``limit`` es el tope TOTAL de scrapes de detalle de la corrida. ``sources``
    filtra las fuentes (None = todas: las de ``SOURCES`` más ``empresas``).
    ``max_credits`` limita el gasto de la corrida y ``min_reserve`` impide gastar
    si la cuenta queda por debajo. ``dry_run`` hace llamadas y parseo pero no
    inserta. Devuelve estadísticas: ``per_source``, ``failures``, ``inserted``,
    ``credits_before`` / ``credits_after`` / ``credits_used`` (contador de la
    cuenta), ``credits_reported`` (suma de ``creditsUsed`` de las respuestas),
    ``stopped`` (motivo de corte, si lo hubo) y ``total_failure``.
    """
    wanted = [SOURCE_ALIASES.get(s.strip().lower(), s.strip().lower()) for s in (sources or ALL_SOURCES)]
    unknown = [s for s in wanted if s not in ALL_SOURCES]
    selected = [s for s in dict.fromkeys(wanted) if s in ALL_SOURCES]
    run = _Run(selected, max(0, int(max_credits)), max(0, int(min_reserve)))
    stats = run.stats
    stats["dry_run"] = bool(dry_run)
    for name in unknown:
        run.fail(name, "config", name, f"fuente desconocida (válidas: {', '.join(ALL_SOURCES)})")
    limit = max(0, int(limit or 0))
    today = datetime.now(timezone.utc).date()

    # Configuración y base: si fallan, no se gasta ningún crédito.
    try:
        aristas = load_keywords(_resolve_keywords_file(keywords_file))["aristas"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        run.fail("-", "config", str(keywords_file), f"keywords ilegibles ({type(exc).__name__})")
        stats["total_failure"] = True
        return stats
    conn = None
    known_urls: set[str] = set()
    try:
        if not dry_run or Path(db_path).is_file():
            conn = db.connect(db_path) if dry_run else db.init_db(db_path)
            try:
                known_urls = {row[0] for row in conn.execute(
                    "SELECT url FROM vacantes WHERE fuente LIKE 'firecrawl:%'")}
            except Exception:  # noqa: BLE001 - en dry-run la tabla puede no existir
                if not dry_run:
                    raise
    except Exception as exc:  # noqa: BLE001
        run.fail("-", "db", str(db_path), f"no se pudo abrir la base ({type(exc).__name__}: {exc})")
        stats["db_error"] = True
        stats["total_failure"] = True
        return stats

    try:
        log.info("Firecrawl: fuentes=%s | limit detalle=%d | max_credits=%d | reserva=%d | dry_run=%s",
                 ",".join(selected) or "(ninguna)", limit, run.max_credits, run.min_reserve, dry_run)
        before, error = get_credits()
        stats["credits_before"] = run.remaining = before
        if error:
            run.fail("-", "creditos", "firecrawl_credit_usage", error)
            run.stop("no se pudo leer el saldo de créditos; no se gasta a ciegas")
        else:
            log.info("Créditos Firecrawl restantes al inicio: %d", before)
            if before < run.min_reserve:
                run.stop(f"créditos insuficientes: quedan {before}, reserva mínima {run.min_reserve}")

        # 1) Descubrimiento por fuente
        per_source: dict[str, list[dict]] = {}
        for source_key in selected:
            if run.stopped:
                break
            try:
                if source_key == COMPANY_SOURCE:
                    found = _discover_companies(run, today)
                else:
                    found = _discover(run, source_key, today)
            except Exception as exc:  # noqa: BLE001 - una fuente rota no tumba la corrida
                log.exception("FALLO inesperado en la fuente %s", source_key)
                run.fail(source_key, "fuente", source_key, f"{type(exc).__name__}: {exc}")
                found = {}
            for cand in found.values():
                # Prioridad para el detalle: pesa más el match en el título que en el extracto.
                in_title, _ = match_keywords(cand["titulo"], None, aristas)
                in_text, _ = match_keywords(cand["titulo"], cand["descripcion"], aristas)
                cand["relevancia"] = 10 * len(in_title) + len(in_text)
            per_source[source_key] = sorted(found.values(), key=lambda c: -c["relevancia"])
            stats["per_source"][source_key]["found"] = len(found)
            if not found and not run.stopped:
                log.warning("%s: CERO vacantes individuales en esta corrida", source_key)

        # 2) Detalle: solo vacantes que aún no están en la base, hasta ``limit`` en total
        pending = {key: [c for c in items if c["url"] not in known_urls]
                   for key, items in per_source.items()}
        closed: set[str] = set()
        for cand in _pick_for_detail(pending, limit):
            if run.stopped:
                break
            payload = run.call(
                cand["source"], "detalle", "firecrawl_scrape",
                {"url": cand["url"],
                 "objective": "Leer la descripción completa de esta vacante de empleo"},
                cand["url"],
            )
            if payload is None:
                continue
            try:
                reason = apply_detail(cand, payload, today)
            except Exception as exc:  # noqa: BLE001
                reason = None
                run.fail(cand["source"], "detalle", cand["url"], f"parseo: {type(exc).__name__}: {exc}")
            if reason:
                closed.add(cand["url"])
                run.fail(cand["source"], "detalle", cand["url"], f"descartada: {reason}")

        # 3) Normalización e inserción
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for source_key, items in per_source.items():
            new = 0
            for cand in items:
                if cand["url"] in closed:
                    continue
                try:
                    vacante = to_vacante(cand, aristas, now)
                    if dry_run:
                        is_new = cand["url"] not in known_urls
                        log.info("DRY-RUN %s | %s | %s | %s | %s | aristas %s | %s%s",
                                 vacante["fuente"], vacante["titulo"], vacante["empresa"] or "(sin empresa)",
                                 vacante["modalidad"], vacante["sueldo"] or "sin sueldo",
                                 vacante["aristas"], vacante["url"],
                                 "" if is_new else " | ya estaba en la base")
                    elif cand["url"] in known_urls:
                        _merge_aristas(conn, vacante)
                        is_new = False
                    else:
                        is_new = db.insert_vacante(conn, vacante)
                        known_urls.add(cand["url"])
                    if is_new:
                        new += 1
                    else:
                        stats["duplicates"] += 1
                except Exception as exc:  # noqa: BLE001 - un fallo al guardar no tumba la corrida
                    run.fail(source_key, "guardar", cand.get("url", ""), f"{type(exc).__name__}: {exc}")
            if conn is not None and not dry_run:
                conn.commit()
            stats["per_source"][source_key]["inserted"] = new
            stats["inserted"] += new
            log.info("%s | %d vacantes individuales | %d %s", source_key, len(items), new,
                     "nuevas (dry-run, no insertadas)" if dry_run else "filas nuevas")

        # 4) Créditos al final
        if before is not None:
            after, error = get_credits()
            stats["credits_after"] = after
            if error:
                run.fail("-", "creditos", "firecrawl_credit_usage", error)
            else:
                stats["credits_used"] = before - after
                log.info(
                    "Créditos Firecrawl: antes %d | después %d | delta del contador %d | "
                    "suma de creditsUsed reportados por las llamadas: %d%s",
                    before, after, before - after, run.spent,
                    " (el contador de la cuenta no reflejó el gasto todavía)"
                    if before - after == 0 and run.spent else "",
                )
    except Exception as exc:  # noqa: BLE001 - nunca se propaga un fallo hacia quien llama
        log.exception("FALLO general en Firecrawl")
        run.fail("-", "general", "-", f"{type(exc).__name__}: {exc}")
    finally:
        if conn is not None:
            try:
                if not dry_run:
                    conn.commit()
            finally:
                conn.close()

    responded = any(s["searches_ok"] or s["scrapes_ok"] for s in stats["per_source"].values())
    budget_stop = bool(run.stopped) and any(
        word in run.stopped for word in ("presupuesto", "reserva", "créditos insuficientes"))
    stats["total_failure"] = not responded and stats["inserted"] == 0 and not budget_stop
    return stats


def format_summary(stats: dict) -> str:
    """Resumen por fuente: búsquedas y scrapes ok/fallidos, vacantes encontradas y nuevas."""
    lines = [
        "Resumen Firecrawl" + (" (DRY-RUN: nada insertado)" if stats.get("dry_run") else ""),
        f"  {'fuente':<16}{'busq ok':>8}{'busq x':>8}{'scr ok':>8}{'scr x':>7}{'vacantes':>10}{'nuevas':>8}",
    ]
    for source, s in stats["per_source"].items():
        lines.append(
            f"  {source:<16}{s['searches_ok']:>8}{s['searches_failed']:>8}{s['scrapes_ok']:>8}"
            f"{s['scrapes_failed']:>7}{s['found']:>10}{s['inserted']:>8}")
    lines.append(f"  Filas nuevas: {stats['inserted']} | ya existentes: {stats.get('duplicates', 0)} | "
                 f"URLs descartadas (categoría/listado): {stats.get('discarded_urls', 0)} | "
                 f"fallos registrados: {len(stats['failures'])}")
    lines.append(f"  Créditos: antes {stats['credits_before']} | después {stats['credits_after']} | "
                 f"delta del contador {stats['credits_used']} | "
                 f"reportados por las llamadas {stats.get('credits_reported', 0)}")
    if stats.get("stopped"):
        lines.append(f"  Corrida detenida: {stats['stopped']}")
    if stats.get("total_failure"):
        lines.append("  FALLO TOTAL: ninguna fuente respondió")
    return "\n".join(lines)


def setup_logging() -> Path:
    """Log a logs/YYYY-MM-DD.log (se anexa) y a la consola, como ``run_daily.py``."""
    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"{datetime.now():%Y-%m-%d}.log"
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    console = logging.StreamHandler(sys.stdout)
    for handler in (file_handler, console):
        handler.setFormatter(formatter)
    log.setLevel(logging.INFO)
    log.handlers[:] = [file_handler, console]
    log.propagate = False
    return log_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fuentes Firecrawl del pipeline de empleo (career pages nativas).")
    parser.add_argument("--db", default=db.DEFAULT_DB)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"scrapes de detalle por corrida, en total (default {DEFAULT_LIMIT})")
    parser.add_argument("--sources",
                        help=f"fuentes separadas por coma (default: todas). Válidas: {', '.join(ALL_SOURCES)}")
    parser.add_argument("--max-credits", type=int, default=DEFAULT_MAX_CREDITS,
                        help=f"créditos máximos de la corrida (default {DEFAULT_MAX_CREDITS})")
    parser.add_argument("--min-reserve", type=int, default=DEFAULT_MIN_RESERVE,
                        help=f"no gastar si la cuenta queda por debajo (default {DEFAULT_MIN_RESERVE})")
    parser.add_argument("--dry-run", action="store_true",
                        help="hace las llamadas y el parseo pero NO inserta en la base")
    parser.add_argument("--keywords-file", default="keywords.json")
    args = parser.parse_args(argv)

    log_path = setup_logging()
    stats = run_firecrawl_sources(
        args.db, limit=args.limit, sources=parse_sources_arg(args.sources),
        max_credits=args.max_credits, keywords_file=args.keywords_file,
        dry_run=args.dry_run, min_reserve=args.min_reserve,
    )
    for line in format_summary(stats).splitlines():
        log.info(line)
    log.info("Log: %s", log_path)
    return 1 if stats["total_failure"] else 0


if __name__ == "__main__":
    sys.exit(main())


# --------------------------------------------------------------------------
# Integración en run_daily.py (ver FIRECRAWL_INTEGRATION.md)
# --------------------------------------------------------------------------
# Pegar en run_daily.main(), después del bloque que loguea el resumen de JobSpy
# ("Fuentes sin ninguna búsqueda exitosa") y antes de filter_rank.evaluate(...):
#
#     try:
#         import firecrawl_sources
#         fc_stats = firecrawl_sources.run_firecrawl_sources(
#             args.db, limit=5, max_credits=100, keywords_file=keywords_file)
#         for line in firecrawl_sources.format_summary(fc_stats).splitlines():
#             log.info(line)
#     except Exception:  # noqa: BLE001 - Firecrawl nunca tumba la corrida diaria
#         log.exception("FALLO en Firecrawl; la corrida sigue con el filtro")
#
# El logger "empleo.firecrawl" hereda los handlers de "empleo" (consola + log del día).
