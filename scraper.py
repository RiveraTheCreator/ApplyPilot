#!/usr/bin/env python3
"""Scraper diario de vacantes (Fase 1) usando JobSpy.

Plan de búsqueda principal: los perfiles de ``search-profiles.json`` (un perfil
= una búsqueda en un sitio, con sus propios parámetros). Antes de usarlos se
validan contra las reglas fatales de ``scraper-config-spec.md``. Si el archivo
falta, es ilegible o no deja ningún perfil válido, se cae al plan por keywords:
cada keyword de ``keywords.json`` en LinkedIn, Indeed, Glassdoor y ZipRecruiter.
``keywords.json`` sigue siendo siempre la fuente de aristas para atribuir y
rankear. Cada vacante se normaliza al esquema de ``vacantes`` y se guarda con
INSERT OR IGNORE (re-ejecutar no duplica filas).

Resiliencia: las búsquedas van de una en una (sin paralelismo), con pausas con
jitter; cada búsqueda fallida se reintenta con backoff exponencial; cada llamada
a JobSpy usa el siguiente user-agent de ``USER_AGENTS``; los proxies son
opcionales (``JOBSPY_PROXIES`` o ``proxy-config.json``); y un circuit breaker
por sitio deja de insistir tras varios fallos consecutivos. Un sitio caído
nunca tumba la corrida.

Uso:
    python3 scraper.py                         # los perfiles de search-profiles.json
    python3 scraper.py --profiles 12_F3_HospitalitySaaS_linkedin --limit 5
    python3 scraper.py --no-profiles --aristas 1,3,5 --limit 10   # plan por keywords
    python3 scraper.py --no-profiles --aristas 6 --max-keywords-per-arista 2

Importable: ``scrape_aristas``, ``load_search_profiles``, ``build_profile_plan``,
``load_proxies``, ``parse_salary``, ``detect_modalidad``.
Sin credenciales ni API keys: solo los endpoints públicos que usa JobSpy.
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
import random
import re
import sys
import time
import warnings
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import db
from textutil import keyword_in, load_keywords, normalize

log = logging.getLogger("empleo.scraper")

SITES = ("linkedin", "indeed", "glassdoor", "zip_recruiter")
LOCATION = "Mexico"
COUNTRY_INDEED = "Mexico"
HOURS_OLD = 168  # 7 días
DEFAULT_LIMIT = 25
LINKEDIN_MAX_RESULTS = 100  # tope del spec para results_wanted en LinkedIn

# Resiliencia
SLEEP_SECONDS = 2.0  # base de la pausa entre búsquedas (x jitter 0.5–1.5)
SITE_SWITCH_SLEEP_SECONDS = 5.0  # pausa extra al cambiar de sitio (mismo jitter)
MAX_RETRIES = 3  # reintentos por búsqueda, además del intento inicial
RETRY_DELAYS = (10.0, 30.0, 90.0)  # segundos antes de cada reintento (x jitter 0.7–1.3)
CIRCUIT_BREAKER_THRESHOLD = 5  # fallos consecutivos que dan un sitio por muerto
PROXY_ENV_VAR = "JOBSPY_PROXIES"
PROXY_CONFIG_FILE = Path(__file__).resolve().parent / "proxy-config.json"
# Navegadores de escritorio; se usa uno distinto por llamada a JobSpy (round-robin).
USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/18.1 Safari/605.1.15",
)

USD_TO_MXN = 18.5
HOURS_PER_MONTH = 160
WEEKS_PER_MONTH = 52 / 12
WORKDAYS_PER_MONTH = 260 / 12

# --------------------------------------------------------------------------
# Sueldo
# --------------------------------------------------------------------------

_NUM_RE = re.compile(r"(\d[\d.,]*\d|\d)\s*(k\b|mil\b)?")
_CURRENCY_PATTERNS = (
    ("USD", re.compile(r"\busd\b|us\$|\bu\.s\. ?dollars?\b|\bdolares\b|\bdollars\b")),
    ("MXN", re.compile(r"\bmxn\b|mx\$|\bm\.n\.|\bpesos\b")),
)
_PERIOD_PATTERNS = (
    ("hour", re.compile(r"hourly|per hour|an hour|/ ?h(?:r|our|ora)?\b|por hora|\bla hora\b")),
    ("week", re.compile(r"weekly|per week|a week|/ ?(?:week|semana)\b|semanal(?:es)?|por semana|a la semana")),
    ("day", re.compile(r"daily|per day|a day|/ ?(?:day|dia)\b|diarios?|por dia|al dia")),
    ("month", re.compile(r"monthly|per month|a month|/ ?(?:month|mes|mo)\b|mensual(?:es)?|al mes|por mes")),
    ("year", re.compile(r"yearly|annual(?:ly)?|per year|per annum|a year|/ ?(?:year|yr|ano)\b|anual(?:es)?|al ano|por ano")),
)
# Fragmento con pinta de sueldo dentro de una descripción: exige símbolo o
# código de moneda pegado al número y un periodo explícito cerca.
_DESC_SALARY_RE = re.compile(
    r"(?:mxn|mx\$|usd|us\$|\$)\s?\d[\d.,]*\s?(?:k\b|mil\b)?"
    r"(?:\s?(?:-|–|—|a|to|y|hasta)\s?(?:mxn|mx\$|usd|us\$|\$)?\s?\d[\d.,]*\s?(?:k\b|mil\b)?)?"
    r"[^.\n]{0,40}?"
    r"(?:mensual(?:es)?|al mes|por mes|per month|monthly|/ ?mes|/ ?month|a month"
    r"|anual(?:es)?|al ano|por ano|per year|yearly|annually|per annum|/ ?year|/ ?yr|a year"
    r"|por hora|per hour|hourly|/ ?hr|/ ?hour|an hour)"
)


def _to_number(token: str, suffix: str | None) -> float | None:
    """Convierte '35,000', '35.000', '35,000.50', '67000.0' o '35' (+k/mil) a float."""
    token = token.strip(".,")
    if not token:
        return None
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?", token):
        head = re.match(r"\d{1,3}(?:[.,]\d{3})+", token).group(0)
        tail = token[len(head):]
        value = float(re.sub(r"[.,]", "", head))
        if tail:
            value += float("0." + tail[1:])
    elif re.fullmatch(r"\d+[.,]\d{1,2}", token):
        value = float(token.replace(",", "."))
    elif re.fullmatch(r"\d+", token):
        value = float(token)
    else:
        return None
    return value * 1000 if suffix else value


def parse_salary(text: str | None) -> tuple[float | None, float | None]:
    """Convierte un texto de sueldo a (mín, máx) en MXN por mes.

    Reglas: USD/mes * 18.5; USD/año / 12 * 18.5; MXN/mes tal cual; MXN/año / 12;
    por hora * 160 (y luego * 18.5 si es USD). Extensión determinista: semanal
    * 52/12 y diario * 260/12. Si la moneda o el periodo no están claros
    devuelve (None, None): nunca se inventa un sueldo.

    >>> parse_salary("MXN 35000-40000 monthly")
    (35000.0, 40000.0)
    >>> parse_salary("USD 24,000 yearly")
    (37000.0, 37000.0)
    >>> parse_salary("$30,000 mensuales")
    (None, None)
    """
    norm = normalize(text)
    if not norm:
        return None, None
    currencies = [code for code, pat in _CURRENCY_PATTERNS if pat.search(norm)]
    periods = [name for name, pat in _PERIOD_PATTERNS if pat.search(norm)]
    if len(currencies) != 1 or len(periods) != 1:
        return None, None
    numbers = []
    for token, suffix in _NUM_RE.findall(norm):
        value = _to_number(token, suffix)
        if value is not None and value > 0:
            numbers.append(value)
    if not numbers:
        return None, None
    low, high = numbers[0], (numbers[1] if len(numbers) > 1 else numbers[0])
    if high < low:
        low, high = high, low
    factor = {
        "month": 1.0,
        "year": 1 / 12,
        "hour": HOURS_PER_MONTH,
        "week": WEEKS_PER_MONTH,
        "day": WORKDAYS_PER_MONTH,
    }[periods[0]]
    if currencies[0] == "USD":
        factor *= USD_TO_MXN
    return round(low * factor, 2), round(high * factor, 2)


def salary_from_description(description: str | None) -> str | None:
    """Devuelve el primer fragmento de la descripción que parsea como sueldo.

    Conservador: el fragmento debe traer moneda explícita y periodo; si
    ``parse_salary`` no lo resuelve sin ambigüedad se descarta.
    """
    norm = normalize(description)
    for match in _DESC_SALARY_RE.finditer(norm):
        snippet = match.group(0).strip()
        if parse_salary(snippet)[0] is not None:
            return snippet
    return None


# --------------------------------------------------------------------------
# Modalidad
# --------------------------------------------------------------------------

_REMOTE_RE = re.compile(
    r"\b(?:remot[oa]s?|remote(?:ly)?|home ?office|teletrabajo|work(?:ing)? from home"
    r"|wfh|trabajo (?:desde|en) casa|work from anywhere)\b"
    # "remote teams / remote support / soporte remoto a clientes" no describen la modalidad
    r"(?! (?:teams?|clients?|customers?|support|access|desktop|control|monitoring|sensing|sites?|a clientes))"
)
_STRONG_REMOTE_RE = re.compile(
    r"100 ?% ?(?:remot[oae]|home ?office)|fully[ -]remote|full[ -]remote|remote[ -]first"
    r"|(?:totalmente|completamente) remot[oa]|work from anywhere"
    r"|(?:modalidad|esquema|trabajo|puesto|posicion|vacante)(?: de trabajo)?:? (?:100 ?% ?)?remot[oa]"
    r"|(?:this is|is) a (?:fully |100 ?% ?)?remote (?:position|role|job|opportunity)"
)
_HYBRID_RE = re.compile(r"\b(?:hibrid[oa]s?|hybrid|semi ?-?presencial)\b")
_ONSITE_RE = re.compile(
    r"\b(?:presencial(?:es)?|on[ -]?site|in[ -]office|in[ -]person|en sitio|en oficina)\b"
)
_NOT_REMOTE_RE = re.compile(
    r"\b(?:no es|not(?: a)?|no|sin) (?:remot[oae]|remote|home ?office)\b|100 ?% ?presencial"
    r"|(?:modalidad|esquema|trabajo|puesto|posicion|vacante)(?: de trabajo)?:? presencial"
    r"|(?:this is|is) an? (?:fully )?(?:on[ -]?site|in[ -]office|in[ -]person) (?:position|role|job)"
)


def detect_modalidad(
    title: str | None,
    description: str | None = None,
    location: str | None = None,
    is_remote: bool | None = None,
) -> str:
    """Clasifica la modalidad: 'remoto', 'hibrido', 'presencial' o 'no especificada'.

    El título y la ubicación mandan sobre la descripción. En la descripción,
    una señal fuerte de remoto ("100% remoto", "fully remote") gana; si no,
    "híbrido" gana a una mención suelta de "remote". ``is_remote`` es la
    bandera que JobSpy trae de la fuente y solo se usa como apoyo.
    """
    head = normalize(f"{title or ''} | {location or ''}")
    body = normalize(description)
    if _HYBRID_RE.search(head):
        return "hibrido"
    if _REMOTE_RE.search(head) and not _NOT_REMOTE_RE.search(head):
        return "remoto"
    if _ONSITE_RE.search(head):
        return "presencial"
    if _STRONG_REMOTE_RE.search(body):
        return "remoto"
    if _HYBRID_RE.search(body):
        return "hibrido"
    if _NOT_REMOTE_RE.search(body):
        return "presencial"
    if is_remote is True or _REMOTE_RE.search(body):
        return "remoto"
    if _ONSITE_RE.search(body):
        return "presencial"
    return "no especificada"


# --------------------------------------------------------------------------
# Keywords / aristas
# --------------------------------------------------------------------------


def match_keywords(title: str | None, description: str | None, aristas: list[dict]):
    """Devuelve (ids de arista, keywords) cuyas keywords aparecen en título+descripción."""
    text = normalize(f"{title or ''}\n{description or ''}")
    ids, found = [], []
    for arista in aristas:
        for keyword in arista["keywords"]:
            if keyword_in(keyword, text):
                if arista["id"] not in ids:
                    ids.append(arista["id"])
                if keyword not in found:
                    found.append(keyword)
    return ids, found


def build_search_plan(aristas: list[dict], max_keywords_per_arista: int | None = None):
    """Lista ordenada de (keyword, [ids de arista]) sin repetir términos de búsqueda.

    Una keyword compartida por varias aristas (p. ej. "Technical Account
    Manager" en 3 y 11) se busca una sola vez y se atribuye a todas.
    """
    plan: dict[str, list[int]] = {}
    labels: dict[str, str] = {}
    for arista in aristas:
        keywords = arista["keywords"]
        if max_keywords_per_arista:
            keywords = keywords[:max_keywords_per_arista]
        for keyword in keywords:
            key = normalize(keyword)
            labels.setdefault(key, keyword)
            plan.setdefault(key, [])
            if arista["id"] not in plan[key]:
                plan[key].append(arista["id"])
    return [(labels[key], ids) for key, ids in plan.items()]


# --------------------------------------------------------------------------
# JobSpy -> esquema
# --------------------------------------------------------------------------


def _clean(value):
    """Normaliza NaN/NaT/None/'' de pandas a None y el resto a str recortado."""
    if value is None:
        return None
    try:
        if value != value:  # NaN
            return None
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return None if text in ("", "nan", "NaT", "None", "<NA>") else text


def _number(value) -> float | None:
    """Float o None (NaN -> None)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def _fmt_amount(value: float) -> str:
    return str(int(value)) if value == int(value) else f"{value:.2f}"


def raw_salary_text(row: dict) -> str | None:
    """Texto crudo de sueldo a partir de las columnas de JobSpy, sin interpretar nada."""
    low, high = _number(row.get("min_amount")), _number(row.get("max_amount"))
    if low is None and high is None:
        return None
    low = high if low is None else low
    high = low if high is None else high
    amount = _fmt_amount(low) if low == high else f"{_fmt_amount(low)}-{_fmt_amount(high)}"
    parts = [_clean(row.get("currency")), amount, _clean(row.get("interval"))]
    return " ".join(p for p in parts if p)


def map_job(row: dict, site: str, search_aristas: list[int], aristas: list[dict], now: str) -> dict | None:
    """Convierte una fila de JobSpy en un dict con las columnas de ``vacantes``."""
    titulo = _clean(row.get("title"))
    if not titulo:
        return None
    descripcion = _clean(row.get("description"))
    ubicacion = _clean(row.get("location"))
    sueldo = raw_salary_text(row) or salary_from_description(descripcion)
    sueldo_min, sueldo_max = parse_salary(sueldo)
    matched_ids, matched_keywords = match_keywords(titulo, descripcion, aristas)
    # Aristas = las que matchean por texto + la(s) de la búsqueda que trajo la vacante.
    arista_ids = sorted(set(matched_ids) | set(search_aristas))
    fecha = row.get("date_posted")
    if isinstance(fecha, (date, datetime)):
        fecha = fecha.isoformat()[:10]
    else:
        fecha = (_clean(fecha) or "")[:10] or None
    is_remote = row.get("is_remote")
    return {
        "titulo": titulo,
        "empresa": _clean(row.get("company")) or "",
        "ubicacion": ubicacion,
        "modalidad": detect_modalidad(
            titulo, descripcion, ubicacion, is_remote is True or str(is_remote) == "True"
        ),
        "sueldo": sueldo,
        "sueldo_min_mxn": sueldo_min,
        "sueldo_max_mxn": sueldo_max,
        "url": _clean(row.get("job_url")) or "",
        "fecha_publicacion": fecha,
        "fuente": _clean(row.get("site")) or site,
        "aristas": json.dumps(arista_ids),
        "keywords_matched": json.dumps(matched_keywords, ensure_ascii=False),
        "descripcion": descripcion,
        "fecha_scrapeo": now,
    }


def _merge_aristas(conn, vacante: dict) -> None:
    """Si la vacante ya existía, añade a su lista las aristas de esta búsqueda."""
    row = conn.execute(
        "SELECT id, aristas FROM vacantes WHERE empresa = ? AND titulo = ? AND url = ?",
        (vacante["empresa"], vacante["titulo"], vacante["url"]),
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
# Resiliencia: user-agents, proxies, circuit breaker
# --------------------------------------------------------------------------

_UA_CYCLE = itertools.cycle(USER_AGENTS)


def next_user_agent() -> str:
    """Siguiente user-agent en round-robin: uno distinto por llamada a scrape_jobs.

    La rotación la hace este módulo; JobSpy solo aplica el UA que recibe.
    """
    return next(_UA_CYCLE)


def _jitter(base: float, low: float = 0.5, high: float = 1.5) -> float:
    return base * random.uniform(low, high)


def _parse_proxy_list(raw) -> list[str]:
    """Lista limpia de URLs de proxy (sin vacíos ni duplicados, en orden)."""
    if isinstance(raw, dict):
        raw = raw.get("proxies")
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, (list, tuple)):
        return []
    proxies: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip() and item.strip() not in proxies:
            proxies.append(item.strip())
    return proxies


def _load_proxies_with_source(config_path: str | Path | None = None) -> tuple[list[str], str]:
    """(proxies, origen). El origen es un texto apto para log: nunca contiene valores."""
    from_env = _parse_proxy_list(os.environ.get(PROXY_ENV_VAR, ""))
    if from_env:
        return from_env, f"env {PROXY_ENV_VAR}"
    path = Path(config_path) if config_path else PROXY_CONFIG_FILE
    if not path.is_file():
        return [], "ninguno"
    try:
        proxies = _parse_proxy_list(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        # Solo el tipo de error: el mensaje podría citar contenido del archivo.
        log.warning("%s ilegible (%s); se continúa sin proxies", path.name, type(exc).__name__)
        return [], "ninguno"
    return (proxies, path.name) if proxies else ([], "ninguno")


def load_proxies(config_path: str | Path | None = None) -> list[str]:
    """Proxies configurados, o ``[]`` si no hay (el sistema funciona igual sin ellos).

    Orden: variable de entorno ``JOBSPY_PROXIES`` (URLs separadas por coma) y,
    si no está, ``proxy-config.json`` junto a este archivo (lista JSON o
    ``{"proxies": [...]}``). Los valores nunca se escriben en el log.
    """
    return _load_proxies_with_source(config_path)[0]


_URL_CREDENTIALS_RE = re.compile(r"(?<=://)[^/@\s:]+:[^/@\s]+(?=@)")


def _redact(text: str, proxies: list[str] | None) -> str:
    """Quita de un mensaje de error cualquier rastro de los proxies configurados."""
    for proxy in proxies or ():
        parts = {proxy, proxy.split("://", 1)[-1]}
        try:
            parsed = urlsplit(proxy if "://" in proxy else f"//{proxy}")
            parts |= {parsed.netloc, parsed.password or "", parsed.username or "",
                      parsed.hostname or ""}
        except ValueError:
            pass
        for part in sorted((p for p in parts if len(p) >= 4), key=len, reverse=True):
            text = text.replace(part, "<proxy>")
    return _URL_CREDENTIALS_RE.sub("<credenciales>", text)


class CircuitBreaker:
    """Fallos CONSECUTIVOS por sitio; al llegar al umbral el sitio queda muerto.

    Cada intento fallido (reintentos incluidos) suma 1 y un éxito resetea a 0.
    Al abrirse se registra un único log ERROR; después ``is_open`` permite
    omitir las búsquedas restantes de ese sitio sin llamar a JobSpy.
    """

    def __init__(self, threshold: int = CIRCUIT_BREAKER_THRESHOLD) -> None:
        self.threshold = threshold
        self.failures: dict[str, int] = {}
        self.open_causes: dict[str, str] = {}

    def is_open(self, site: str) -> bool:
        return site in self.open_causes

    def record_success(self, site: str) -> None:
        self.failures[site] = 0

    def record_failure(self, site: str, cause: str) -> bool:
        """Suma un fallo; devuelve True si el circuito está (o queda) abierto."""
        self.failures[site] = self.failures.get(site, 0) + 1
        if not self.is_open(site) and self.failures[site] >= self.threshold:
            self.open_causes[site] = cause
            log.error(
                "CIRCUIT BREAKER abierto para %s: %s; sitio muerto por el resto de la corrida",
                site, cause,
            )
        return self.is_open(site)


# --------------------------------------------------------------------------
# Perfiles de búsqueda (search-profiles.json)
# --------------------------------------------------------------------------

_BOOLEAN_RE = re.compile(r"\b(OR|AND)\b")


def _blocks_hours_old(site: str, profile: dict) -> bool:
    """Reglas fatales del spec: combinaciones que prohíben ``hours_old``."""
    if site == "indeed":
        return bool(profile.get("is_remote")) or bool(profile.get("job_type"))
    if site == "linkedin":
        return profile.get("easy_apply") is True
    return False


def _valid_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def load_search_profiles(path: str | Path = "search-profiles.json") -> list[dict]:
    """Carga y valida los perfiles de búsqueda; devuelve solo los utilizables.

    Impone las reglas fatales de ``scraper-config-spec.md``: lo corregible se
    corrige con log WARNING y lo que no, descarta el perfil con log ERROR.
    Lanza ``OSError``/``ValueError`` si el archivo falta, no es JSON o no es
    una lista (quien llama decide el fallback al plan por keywords).
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path}: se esperaba una lista JSON de perfiles")
    profiles: list[dict] = []
    seen: set[str] = set()
    for index, raw in enumerate(data, start=1):
        if not isinstance(raw, dict):
            log.error("Perfil #%d descartado: no es un objeto JSON", index)
            continue
        profile = dict(raw)
        name = profile.get("name")
        name = name.strip() if isinstance(name, str) else ""
        label = name or f"#{index}"
        if not name:
            log.error("Perfil %s descartado: falta name", label)
            continue
        profile["name"] = name
        site = profile.get("site_name")
        site = site.strip().lower() if isinstance(site, str) else site
        if site not in SITES:
            log.error("Perfil %s descartado: site_name desconocido %r", label, site)
            continue
        profile["site_name"] = site
        term = profile.get("search_term")
        term = term.strip() if isinstance(term, str) else ""
        if not term:
            log.error("Perfil %s descartado: search_term vacío", label)
            continue
        profile["search_term"] = term
        if name in seen:
            log.error("Perfil %s descartado: nombre duplicado (se conserva el primero)", label)
            continue
        if site != "indeed" and _BOOLEAN_RE.search(term):
            log.error(
                "Perfil %s descartado: search_term con booleanos (OR/AND) en %s; "
                "el spec solo los permite en indeed", label, site,
            )
            continue
        seen.add(name)

        location = profile.get("location")
        if not isinstance(location, str) or not location.strip():
            log.warning("Perfil %s: falta location; se usa %r", label, LOCATION)
            profile["location"] = LOCATION
        if not _valid_count(profile.get("results_wanted")):
            log.warning("Perfil %s: results_wanted ausente o inválido; se usa %d",
                        label, DEFAULT_LIMIT)
            profile["results_wanted"] = DEFAULT_LIMIT
        if "hours_old" in profile and not _valid_count(profile["hours_old"]):
            log.warning("Perfil %s: hours_old inválido; se quita", label)
            del profile["hours_old"]

        if site == "linkedin":
            for field in ("is_remote", "job_type"):
                if field in profile:
                    log.warning("Perfil %s: se quita %s (JobSpy lo ignora en linkedin)",
                                label, field)
                    del profile[field]
            if profile["results_wanted"] > LINKEDIN_MAX_RESULTS:
                log.warning("Perfil %s: results_wanted %d > %d en linkedin; se limita a %d",
                            label, profile["results_wanted"], LINKEDIN_MAX_RESULTS,
                            LINKEDIN_MAX_RESULTS)
                profile["results_wanted"] = LINKEDIN_MAX_RESULTS
        if "hours_old" in profile and _blocks_hours_old(site, profile):
            reason = "is_remote/job_type" if site == "indeed" else "easy_apply"
            log.warning("Perfil %s: se quita hours_old (prohibido en %s con %s)",
                        label, site, reason)
            del profile["hours_old"]
        if site == "glassdoor" and not profile.get("country_indeed"):
            log.warning("Perfil %s: falta country_indeed en glassdoor; se usa %r",
                        label, COUNTRY_INDEED)
            profile["country_indeed"] = COUNTRY_INDEED
        profiles.append(profile)
    return profiles


def build_profile_plan(
    profiles: list[dict],
    aristas: list[dict],
    limit: int | None = None,
    sites: tuple[str, ...] = SITES,
) -> list[dict]:
    """Convierte perfiles validados en búsquedas listas para ``scrape_jobs``.

    Conserva el orden del archivo. Cada elemento es ``{"profile", "site",
    "kwargs", "arista_ids"}``. ``hours_old`` solo se añade cuando las reglas
    fatales lo permiten (nunca en Indeed con is_remote/job_type ni en LinkedIn
    con easy_apply). ``proxies`` y ``user_agent`` no van aquí: se añaden en
    cada llamada. Las aristas se derivan con :func:`match_keywords` sobre el
    ``search_term`` del perfil.
    """
    plan = []
    for profile in profiles:
        site = profile["site_name"]
        if site not in sites:
            continue
        wanted = limit if limit else profile["results_wanted"]
        if site == "linkedin":
            wanted = min(wanted, LINKEDIN_MAX_RESULTS)
        kwargs = {
            "site_name": [site],
            "search_term": profile["search_term"],
            "location": profile["location"],
            "results_wanted": wanted,
        }
        country = profile.get("country_indeed") or (
            COUNTRY_INDEED if site in ("indeed", "glassdoor") else None
        )
        if country:
            kwargs["country_indeed"] = country
        for field in ("is_remote", "job_type", "easy_apply"):
            if field in profile:
                kwargs[field] = profile[field]
        if not _blocks_hours_old(site, profile):
            kwargs["hours_old"] = profile.get("hours_old") or HOURS_OLD
        kwargs["linkedin_fetch_description"] = True
        kwargs["verbose"] = 0
        arista_ids, _ = match_keywords(profile["search_term"], None, aristas)
        plan.append({
            "profile": profile["name"],
            "site": site,
            "kwargs": kwargs,
            "arista_ids": arista_ids,
        })
    return plan


def build_keyword_jobs(
    plan: list[tuple[str, list[int]]], sites: tuple[str, ...], limit: int
) -> list[dict]:
    """Plan por keywords (fallback) en el mismo formato que el de perfiles."""
    jobs = []
    for keyword, search_aristas in plan:
        for site in sites:
            jobs.append({
                "profile": None,
                "site": site,
                "kwargs": {
                    "site_name": [site],
                    "search_term": keyword,
                    "location": LOCATION,
                    "results_wanted": limit,
                    "hours_old": HOURS_OLD,
                    "country_indeed": COUNTRY_INDEED,
                    "linkedin_fetch_description": True,
                    "verbose": 0,
                },
                "arista_ids": search_aristas,
            })
    return jobs


# --------------------------------------------------------------------------
# Scrapeo
# --------------------------------------------------------------------------


def scrape_site(
    kwargs: dict, proxies: list[str] | None = None, user_agent: str | None = None
) -> tuple[list[dict], str | None]:
    """Una llamada a ``scrape_jobs``. Devuelve (filas, error); nunca lanza excepción.

    El error puede venir como excepción o en ``df.attrs["errors"]``; en ambos
    casos se devuelve como texto, sin rastro de los proxies.
    """
    try:
        from jobspy import scrape_jobs

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            df = scrape_jobs(**kwargs, proxies=proxies or None, user_agent=user_agent)
        rows = df.to_dict("records") if len(df) else []
        # Las versiones recientes de JobSpy no lanzan: reportan el error por sitio aquí.
        errors = getattr(df, "attrs", {}).get("errors") or {}
        error = "; ".join(str(e) for e in errors.values()) or None
    except Exception as exc:  # noqa: BLE001 - un sitio caído no debe tumbar la corrida
        rows, error = [], f"{type(exc).__name__}: {exc}"
    return rows, (_redact(error, proxies)[:300] if error else None)


def run_search(
    job: dict,
    breaker: CircuitBreaker,
    proxies: list[str] | None = None,
    max_retries: int = MAX_RETRIES,
) -> tuple[list[dict], str | None, int]:
    """Ejecuta una búsqueda con reintentos y backoff. Devuelve (filas, error, intentos).

    1 intento inicial + hasta ``max_retries`` reintentos con esperas
    ``RETRY_DELAYS`` (jitter x0.7–1.3). Cada intento usa el siguiente
    user-agent y alimenta el circuit breaker; si este se abre, no se reintenta más.
    """
    site, label = job["site"], job["profile"] or job["kwargs"]["search_term"]
    rows: list[dict] = []
    error: str | None = None
    attempts = 0
    for attempt in range(max_retries + 1):
        attempts += 1
        rows, error = scrape_site(job["kwargs"], proxies=proxies, user_agent=next_user_agent())
        if not error:
            breaker.record_success(site)
            return rows, None, attempts
        if breaker.record_failure(site, error) or attempt >= max_retries:
            break
        delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)] * random.uniform(0.7, 1.3)
        log.warning("FALLO %s | %r | intento %d/%d | %s | reintento en %.0f s",
                    site, label, attempts, max_retries + 1, error, delay)
        time.sleep(delay)
    return rows, error, attempts


def scrape_aristas(
    arista_ids: list[int] | None = None,
    limit: int | None = None,
    db_path: str = db.DEFAULT_DB,
    keywords_file: str = "keywords.json",
    max_keywords_per_arista: int | None = None,
    sites: tuple[str, ...] = SITES,
    sleep_seconds: float = SLEEP_SECONDS,
    retry_failed: bool = True,
    profiles_file: str | Path = "search-profiles.json",
    use_profiles: bool = True,
    profiles: list[str] | None = None,
) -> dict:
    """Ejecuta el plan de búsqueda, una búsqueda a la vez, y guarda en SQLite.

    Plan principal: los perfiles de ``profiles_file`` (``profiles`` filtra por
    nombre; ``sites`` y ``arista_ids`` también los filtran, este último por las
    aristas atribuidas a cada perfil). Si ``use_profiles`` es False, el archivo
    falta o es ilegible, o no queda ningún perfil válido, se usa el plan por
    keywords de ``keywords_file`` (``max_keywords_per_arista`` solo afecta a
    ese plan). ``limit`` None = ``results_wanted`` de cada perfil, o
    ``DEFAULT_LIMIT`` en el plan por keywords.

    Cada búsqueda fallida se reintenta con backoff (``retry_failed``) y un
    sitio con ``CIRCUIT_BREAKER_THRESHOLD`` fallos consecutivos se omite el
    resto de la corrida. Ningún fallo de sitio ni de guardado tumba la corrida.
    Devuelve un dict de estadísticas con ``plan_source``, ``profiles``,
    ``searches``, ``per_site`` (ok / failed / found / inserted / skipped),
    ``failures``, ``skipped``, ``inserted`` y ``total_failure`` (True si
    ninguna búsqueda respondió).
    """
    config = load_keywords(keywords_file)
    all_aristas = config["aristas"]
    known = {a["id"] for a in all_aristas}
    if arista_ids:
        unknown = sorted(set(arista_ids) - known)
        if unknown:
            raise ValueError(f"Aristas inexistentes en {keywords_file}: {unknown}")
        selected = [a for a in all_aristas if a["id"] in set(arista_ids)]
    else:
        selected = all_aristas
    selected_ids = {a["id"] for a in selected}

    jobs: list[dict] = []
    plan_source = "keywords"
    total_keywords = 0
    if use_profiles:
        try:
            valid = load_search_profiles(profiles_file)
        except (OSError, ValueError) as exc:
            log.warning("No se pudo usar %s (%s: %s); fallback al plan por keywords",
                        profiles_file, type(exc).__name__, exc)
            valid = []
        else:
            if not valid:
                log.warning("%s no tiene perfiles válidos; fallback al plan por keywords",
                            profiles_file)
        if valid and profiles:
            wanted_names = [n.strip() for n in profiles if n and n.strip()]
            missing = sorted(set(wanted_names) - {p["name"] for p in valid})
            if missing:
                raise ValueError(f"Perfiles inexistentes o inválidos en {profiles_file}: {missing}")
            valid = [p for p in valid if p["name"] in set(wanted_names)]
        if valid:
            jobs = build_profile_plan(valid, all_aristas, limit=limit, sites=sites)
            if arista_ids:
                jobs = [j for j in jobs if selected_ids & set(j["arista_ids"])]
            if jobs:
                plan_source = "profiles"
            else:
                log.warning("Ningún perfil coincide con los filtros (sitios/aristas); "
                            "fallback al plan por keywords")
    if plan_source == "keywords":
        keyword_limit = limit or DEFAULT_LIMIT
        plan = build_search_plan(selected, max_keywords_per_arista)
        total_keywords = sum(
            len(a["keywords"][:max_keywords_per_arista] if max_keywords_per_arista
                else a["keywords"])
            for a in selected
        )
        jobs = build_keyword_jobs(plan, sites, keyword_limit)
        log.info(
            "Scrapeo por KEYWORDS: %d aristas, %d keywords (%d términos únicos) x %d sitios = "
            "%d búsquedas secuenciales, limit=%d, hours_old=%d",
            len(selected), total_keywords, len(plan), len(sites), len(jobs), keyword_limit,
            HOURS_OLD,
        )
    else:
        log.info(
            "Scrapeo por PERFILES: %d perfiles de %s (%s), búsquedas secuenciales, limit=%s",
            len(jobs), profiles_file,
            ", ".join(f"{s}={sum(1 for j in jobs if j['site'] == s)}" for s in sites),
            limit if limit else "el de cada perfil",
        )

    proxies, proxy_source = _load_proxies_with_source()
    log.info("proxies configurados: %d (origen: %s)", len(proxies), proxy_source)

    stats = {
        "aristas": [a["id"] for a in selected],
        "keywords": total_keywords,
        "searches": len(jobs),
        "plan_source": plan_source,
        "profiles": len(jobs) if plan_source == "profiles" else 0,
        "per_site": {
            s: {"ok": 0, "failed": 0, "found": 0, "inserted": 0, "skipped": 0} for s in sites
        },
        "failures": [],
        "skipped": [],
        "circuit_open": {},
        "inserted": 0,
        "total_failure": False,
    }
    conn = db.init_db(db_path)

    def store(site: str, search_aristas: list[int], rows: list[dict]) -> int:
        """Guarda las filas de una búsqueda; devuelve cuántas eran nuevas."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        new = 0
        for row in rows:
            vacante = map_job(row, site, search_aristas, all_aristas, now)
            if vacante is None:
                continue
            if db.insert_vacante(conn, vacante):
                new += 1
            else:
                _merge_aristas(conn, vacante)
        conn.commit()
        stats["per_site"][site]["found"] += len(rows)
        stats["per_site"][site]["inserted"] += new
        stats["inserted"] += new
        return new

    breaker = CircuitBreaker()
    max_retries = MAX_RETRIES if retry_failed else 0
    previous_site: str | None = None
    try:
        for index, job in enumerate(jobs, start=1):
            site, term = job["site"], job["kwargs"]["search_term"]
            label = job["profile"] or term
            site_stats = stats["per_site"][site]
            if breaker.is_open(site):
                site_stats["skipped"] += 1
                stats["skipped"].append({"site": site, "profile": job["profile"], "keyword": term})
                continue
            if previous_site is not None:
                pause = _jitter(sleep_seconds)
                if site != previous_site:
                    pause += _jitter(SITE_SWITCH_SLEEP_SECONDS if sleep_seconds else 0.0)
                time.sleep(pause)
            previous_site = site

            rows, error, attempts = run_search(job, breaker, proxies, max_retries)
            if error:
                site_stats["failed"] += 1
                stats["failures"].append(
                    {"site": site, "profile": job["profile"], "keyword": term, "error": error}
                )
                log.warning("FALLO %s | %r | %d intento(s) | %s", site, label, attempts, error)
            else:
                site_stats["ok"] += 1
            new = 0
            try:
                new = store(site, job["arista_ids"], rows)
            except Exception as exc:  # noqa: BLE001 - un fallo al guardar no tumba la corrida
                log.exception("FALLO al guardar %s | %r", site, label)
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
                stats["failures"].append({
                    "site": site, "profile": job["profile"], "keyword": term,
                    "error": f"store: {type(exc).__name__}: {exc}"[:300],
                })
            log.info(
                "[%d/%d] %s | aristas %s | %r | %d devueltas%s (+%d)%s",
                index, len(jobs), site, job["arista_ids"], label, len(rows),
                "!" if error else "", new, f" | {attempts} intentos" if attempts > 1 else "",
            )
    finally:
        try:
            conn.commit()
        finally:
            conn.close()
    stats["circuit_open"] = dict(breaker.open_causes)
    if stats["skipped"]:
        by_site: dict[str, list[str]] = {}
        for item in stats["skipped"]:
            by_site.setdefault(item["site"], []).append(item["profile"] or item["keyword"])
        log.warning(
            "Omitidas por circuit breaker (%d): %s", len(stats["skipped"]),
            " | ".join(f"{site}: {', '.join(names)}" for site, names in by_site.items()),
        )
    stats["total_failure"] = all(s["ok"] == 0 for s in stats["per_site"].values())
    return stats


def format_summary(stats: dict) -> str:
    """Resumen por fuente: búsquedas ok/fallidas, vacantes devueltas y filas nuevas."""
    if stats.get("plan_source") == "profiles":
        head = (f"Resumen del scrapeo: plan por perfiles ({stats['profiles']} perfiles, "
                f"{stats['searches']} búsquedas)")
    else:
        head = (f"Resumen del scrapeo: plan por keywords ({len(stats['aristas'])} aristas, "
                f"{stats['keywords']} keywords, {stats['searches']} búsquedas)")
    lines = [
        head,
        f"  {'fuente':<14}{'ok':>5}{'fallos':>8}{'devueltas':>11}{'nuevas':>8}",
    ]
    for site, s in stats["per_site"].items():
        lines.append(f"  {site:<14}{s['ok']:>5}{s['failed']:>8}{s['found']:>11}{s['inserted']:>8}")
        if s["found"] == 0:
            lines.append(f"    -> {site}: CERO vacantes devueltas en esta corrida")
    skipped = {site: s.get("skipped", 0) for site, s in stats["per_site"].items()}
    if any(skipped.values()):
        lines.append("  Omitidas por circuit breaker: "
                     + ", ".join(f"{site}={n}" for site, n in skipped.items() if n))
    lines.append(f"  Filas nuevas en la base: {stats['inserted']}")
    return "\n".join(lines)


def parse_aristas_arg(value: str | None) -> list[int] | None:
    """'1,3,5' -> [1, 3, 5]; None o vacío -> None (todas)."""
    if not value:
        return None
    return [int(part) for part in value.split(",") if part.strip()]


def parse_profiles_arg(value: str | None) -> list[str] | None:
    """'a,b' -> ['a', 'b']; None o vacío -> None (todos los perfiles)."""
    names = [part.strip() for part in (value or "").split(",") if part.strip()]
    return names or None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scraper de vacantes con JobSpy (Fase 1).")
    parser.add_argument("--aristas", help="ids separados por coma, p. ej. 1,3,5 (default: todas)")
    parser.add_argument("--limit", type=int, default=None,
                        help="resultados por búsqueda (default: results_wanted de cada perfil; "
                             f"{DEFAULT_LIMIT} en el plan por keywords)")
    parser.add_argument("--db", default=db.DEFAULT_DB)
    parser.add_argument("--keywords-file", default="keywords.json")
    parser.add_argument("--max-keywords-per-arista", type=int, default=None,
                        help="solo plan por keywords y pruebas manuales; por defecto TODAS")
    parser.add_argument("--profiles-file", default="search-profiles.json",
                        help="perfiles de búsqueda (default search-profiles.json)")
    parser.add_argument("--no-profiles", action="store_true",
                        help="ignora los perfiles y usa el plan por keywords")
    parser.add_argument("--profiles", help="nombres de perfil separados por coma (depuración)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stdout)
    stats = scrape_aristas(
        arista_ids=parse_aristas_arg(args.aristas),
        limit=args.limit,
        db_path=args.db,
        keywords_file=args.keywords_file,
        max_keywords_per_arista=args.max_keywords_per_arista,
        profiles_file=args.profiles_file,
        use_profiles=not args.no_profiles,
        profiles=parse_profiles_arg(args.profiles),
    )
    print(format_summary(stats))
    return 1 if stats["total_failure"] else 0


if __name__ == "__main__":
    sys.exit(main())
