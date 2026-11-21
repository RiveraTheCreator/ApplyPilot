#!/usr/bin/env python3
"""Filtro + ranking por estrellas de vacantes (Ranker v2). Autoridad: ``ranking-rules.md`` v2.

Único filtro duro: se descarta SOLO lo 100% presencial (on-site sin ningún
componente remoto ni híbrido). Híbrido, modalidad no especificada, visa /
relocación y sueldo bajo o desconocido se quedan en el pool y bajan estrellas.

Cada vacante recibe:
  - ``score`` 0-100 (misma base que v1; sueldo máx. 8, desconocido = 4 neutro),
  - ``estrellas`` 1-5 (NULL si se descarta por presencial),
  - ``pasa_filtro`` 1/0.

Pesos, umbrales, on/off de keywords y países viven en ``filter-config.json``
(versionado, con ``filter-changelog.md``). Tras cada evaluación se escribe
``logs/filter-health-YYYY-MM-DD.json`` con métricas y alertas (ver
:mod:`filter_health`).

NO existe filtro por años de experiencia ni por título "senior": los años
pedidos son la señal más débil del score (0 a 5 puntos, nunca penaliza).
Desde la config 2.1.0 (reapunte 2026-11-20) un TÍTULO senior topa en 3★ y una
región de contratación EU/Asia (título + ubicación) topa en 2★, con −10 de score
cada uno; son caps, no descartes. Sin esas claves en la config (2.0.0) no aplican.

Uso:
    python3 filter_rank.py                 # evalúa filas con estado='nueva'
    python3 filter_rank.py --all --top 20  # todas las filas, imprime el top 20
    python3 filter_rank.py --test-onsite   # prueba el detector presencial
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

import db
from scraper import _HYBRID_RE, _REMOTE_RE, _STRONG_REMOTE_RE
from textutil import keyword_in, load_keywords, normalize

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE_DIR / "filter-config.json"

DROP_ONSITE = "100% presencial (sin componente remoto ni híbrido)"

# --------------------------------------------------------------------------
# Detector de modalidad v2 (con manejo de negaciones y menciones neutras)
# --------------------------------------------------------------------------

_ONSITE_TERM = (
    r"(?:presencial(?:es|mente|idad)?|on[ -]?site|in[ -]office|in[ -]person|en sitio(?! web| de internet)"
    # "oficina" solo cuenta con contexto de trabajo ("Nescafé en oficinas" no es modalidad).
    r"|(?:trabajo|trabajar|laborar|labores|asistir|acudir|presentarse|asistencia) (?:\w+ ){0,2}?"
    r"(?:en|a) (?:la |las |nuestras? )?oficinas?|desde (?:la |nuestras? )?oficinas?)"
)
# Detector principal: menciones de trabajo en sitio.
_ONSITE_RE = re.compile(rf"\b{_ONSITE_TERM}\b")

# Negaciones: "no presencial", "no requiere presencial", "sin asistir a oficina",
# "not on-site", "100% on-site: no". Se reemplazan por un marcador remoto.
_NEGATED_ONSITE_RE = re.compile(
    r"\b(?:no|sin|nunca|cero|ni)\s+(?:es\s+|sera\s+|hay\s+|se\s+)?"
    r"(?:requiere(?:s|n)?\s+|necesita(?:s|n)?\s+|necesidad\s+de\s+|obligacion\s+de\s+|tienes\s+que\s+|debes\s+)?"
    r"(?:(?:de|ir|ir a|asistir|asistir a|acudir|acudir a|trabajar|forma|manera|modo|modalidad|asistencia|"
    r"traslados?|desplazarte|la|las|una|un|a)\s+){0,4}"
    rf"(?:{_ONSITE_TERM}|oficinas?)"
    r"|\b(?:not|no|non|never|without)[ -](?:required\s+to\s+(?:be|work)\s+|need(?:ed)?\s+to\s+(?:be|work)\s+|"
    r"an?\s+|requirement\s+to\s+(?:be|work)\s+|going\s+(?:to\s+)?|commut\w*\s+(?:to\s+)?)?(?:the\s+|an\s+)?"
    r"(?:on[ -]?site|in[ -]office|in[ -]person|office)\b(?!\s+(?:visits?|meetings?))"
    rf"|{_ONSITE_TERM}\W{{0,6}}(?:no|false|n/a)\b(?!\s+(?:es|is)\b)"
)

# Menciones de presencia que NO describen la modalidad del puesto
# (entrevistas, eventos, offsites, inducción, visitas ocasionales).
_NEUTRAL_ONSITE_RE = re.compile(
    rf"(?:entrevistas?|interviews?|reuniones? ocasionales?|eventos?|events?|offsites?|sprints?|"
    rf"retreats?|meetups?|workshops?|induccion|onboarding|capacitacion(?:es)?|trainings?|kick[ -]?off)"
    rf"(?:\s+\w+){{0,3}}\s+{_ONSITE_TERM}"
    rf"|{_ONSITE_TERM}(?:\s+\w+){{0,2}}\s+(?:interviews?|entrevistas?|events?|eventos?|sessions?|sesiones|"
    rf"visits?|visitas|meetings?|reuniones|offsites?|team (?:sprints?|offsites?|retreats?)|"
    rf"interactions?|collaboration|training|capacitacion|and (?:virtual|online|remote)|y (?:virtual|en linea|remoto))"
    rf"|(?:occasional(?:ly)?|ocasional(?:es|mente)?|eventual(?:es|mente)?|periodic(?:ally)?|periodic[oa]s?|"
    rf"puntual(?:es)?|esporadic[oa]s?|a few times|times (?:a|per) year|yearly|anual(?:es)?|quarterly|trimestral(?:es)?)"
    rf"(?:\s+\w+){{0,4}}\s+{_ONSITE_TERM}"
    rf"|{_ONSITE_TERM}(?:\s+\w+){{0,4}}\s+(?:occasional(?:ly)?|ocasional(?:es|mente)?|puntual(?:es)?|"
    rf"(?:two|three|four|2|3|4|una|dos|tres) (?:to \w+ )?times (?:a |per )?year(?:ly)?|veces al ano)"
    rf"|(?:by telephone|telephone|telefonica|virtual|online|video|chat|calls?|phone|email) \(?(?:or|o|and|y) {_ONSITE_TERM}"
    rf"|{_ONSITE_TERM} (?:and|or|y|o) (?:via |by |por )?(?:telephone|phone|telefono|virtual|online|video)"
    rf"|(?:visitas|visits) {_ONSITE_TERM}"
    rf"|{_ONSITE_TERM} (?:teams?|equipos?|staff|personnel|documentation|documentacion)\b"
)

# Señales fuertes de 100% presencial (descartan aunque haya una mención remota débil).
_STRONG_ONSITE_RE = re.compile(
    r"100 ?% ?(?:presencial|on[ -]?site|in[ -]office|in[ -]person)"
    r"|(?:fully|completely|full[ -]time|totally) (?:on[ -]?site|in[ -]office|in[ -]person)"
    r"|(?:totalmente|completamente|solo|solamente|unicamente) presencial"
    # "Tipo de trabajo: empleo presencial" / "Work location: in person" son etiquetas
    # genéricas de Indeed que aparecen también en vacantes remotas: señal normal, no fuerte.
    r"|(?:modalidad|esquema|(?<!tipo de )(?<!lugar de )trabajo|puesto|posicion|vacante|work modality|work model|"
    r"work arrangement|location type|workplace type)(?: de trabajo)?\W{0,4}"
    r"(?:empleo |trabajo |100 ?% ?)?(?:presencial|on[ -]?site|in[ -]office|in[ -]person)\b"
    r"(?!\W{0,4}(?:con|with|y|and|/|o|or)\b)"
    r"|(?:this is|is) an? (?:fully |100 ?% ?)?(?:on[ -]?site|in[ -]office|in[ -]person) (?:position|role|job)"
    r"|\b(?:on[ -]?site|in[ -]office) (?:position|role|job|work model)\b"
    r"|\b(?:no es|not(?: a)?|no|sin) (?:un (?:puesto|trabajo) )?(?:remot[oae]|remote|home ?office)\b(?! (?:first|friendly))"
    r"|(?:on[ -]?site|presencial|in[ -]office)(?:\s+\w+){0,4}\s+(?:five|5|cinco) (?:days|dias)"
    r"|(?:five|5|cinco) (?:days|dias) (?:a week |a la semana |por semana )?(?:on[ -]?site|in[ -]office|presencial|en (?:la )?oficina)"
)

_REMOTE_EXTRA_RE = re.compile(r"\bdesde casa\b|\bhome ?office\b|\b__noonsite__\b|\bteletrabajo\b|\bvirtual (?:role|position)\b")
# "presencial con algunos días de home office", "3 days on site, 2 days home".
_HYBRID_EXTRA_RE = re.compile(
    r"presencial con (?:\w+ ){0,3}(?:home ?office|remoto|trabajo remoto)"
    r"|(?:[1-4]|one|two|three|four|uno|dos|tres|cuatro) (?:days?|dias?) (?:\w+ ){0,3}(?:on[ -]?site|in[ -]office|en (?:la )?oficina|presencial(?:es)?|home ?office|remot[oa]s?|from home)"
    r"|(?:on[ -]?site|in[ -]office|presencial) (?:with|con) (?:flexibility|flexibilidad|home ?office|some remote)"
    r"|remote(?:ly)? (?:and|or|y|o) (?:on[ -]?site|in[ -]office)|remoto (?:y|o) presencial"
    # "70% de presencialidad", "at least 50% of their time in-office", "presencial 60%".
    r"|\b[1-9]\d? ?% (?:(?:of (?:their|your|the) time|del tiempo|de (?:la )?(?:semana|jornada)) )?(?:de )?"
    r"(?:presencialidad|presencial|on[ -]?site|in[ -]office|in the office|en (?:la )?oficina|remot\w*|home ?office)"
    r"|(?:presencialidad|presencial|on[ -]?site|in[ -]office|remot[oa]|remote) (?:del |al |de )?[1-9]\d? ?%"
)


# "soporte remoto", "software de asistencia remota", "remote desktop": jerga técnica, no modalidad.
_TECH_REMOTE_RE = re.compile(
    r"\b(?:asistencia|soporte|acceso|escritorio|conexion(?:es)?|control|monitoreo|administracion|"
    r"herramientas?|sesion(?:es)?|gestion|atencion|mantenimiento|diagnostico) remot[oa]s?\b"
    r"|\bremote (?:desktop|access|support|assistance|monitoring|control|sessions?|troubleshooting|management)\b"
)


def _clean_onsite(text_norm: str) -> str:
    """Quita negaciones (las vuelve marcador remoto), menciones neutras de presencia
    y jerga técnica con "remoto" que no describe la modalidad."""
    text = _NEGATED_ONSITE_RE.sub(" __noonsite__ ", text_norm)
    text = _TECH_REMOTE_RE.sub(" ", text)
    return _NEUTRAL_ONSITE_RE.sub(" ", text)


def classify_modality(text_norm: str, scraper_modalidad: str | None = None) -> tuple[str, list[str]]:
    """Modalidad v2 del texto ya normalizado (título + ubicación + descripción).

    Devuelve (``remoto`` | ``hibrido`` | ``presencial`` | ``no_especificada``, señales).
    Solo ``presencial`` descarta: hay señal de trabajo en sitio y ninguna de
    remoto/híbrido, o una señal fuerte de 100% presencial.
    """
    text = _clean_onsite(text_norm)
    signals = []
    strong_remote = bool(_STRONG_REMOTE_RE.search(text))
    strong_onsite = bool(_STRONG_ONSITE_RE.search(text))
    hybrid = bool(_HYBRID_RE.search(text) or _HYBRID_EXTRA_RE.search(text))
    remote = bool(_REMOTE_RE.search(text) or _REMOTE_EXTRA_RE.search(text)) and not (
        strong_onsite and not strong_remote and not hybrid
    )
    onsite = bool(_ONSITE_RE.search(text))
    for name, flag in (("strong_remote", strong_remote), ("strong_onsite", strong_onsite),
                       ("hybrid", hybrid), ("remote", remote), ("onsite", onsite)):
        if flag:
            signals.append(name)

    if strong_remote and not strong_onsite:
        return "remoto", signals
    if hybrid:
        return "hibrido", signals
    if strong_onsite:
        return "presencial", signals
    if remote and onsite:
        return "hibrido", signals + ["mixed_remote_onsite"]
    if remote:
        return "remoto", signals
    if onsite:
        return "presencial", signals
    if scraper_modalidad == "remoto":
        return "remoto", signals + ["scraper_remoto"]
    if scraper_modalidad == "hibrido":
        return "hibrido", signals + ["scraper_hibrido"]
    return "no_especificada", signals


def is_onsite_only(text: str) -> bool:
    """Atajo para pruebas: True si el texto (crudo) se descartaría por 100% presencial."""
    return classify_modality(normalize(text))[0] == "presencial"


# --------------------------------------------------------------------------
# Señales del score
# --------------------------------------------------------------------------

_VISA_RELOCATION_PATTERNS = [
    r"(?:authorized|authorised|authorization|authorisation|eligible|eligibility|legally (?:authorized|permitted)|right) to work in (?:the )?(?:u\.?s\.?a?\b|united states|canada|u\.?k\.?\b|united kingdom|europe|eu\b|european union|australia)",
    r"(?:u\.?s\.?|us|united states|canadian|uk|eu) (?:work authorization|work permit|citizens?(?:hip)?|residents?|residency)\b",
    r"\b(?:green card|security clearance)\b",
    r"must (?:reside|live|be (?:based|located)) in (?:the )?(?:u\.?s\.?a?\b|united states|canada|u\.?k\.?\b|united kingdom|europe)",
    r"(?:requiere|requiere de|requisito:?|indispensable:?|contar con|tener|necesaria|necesario) visa\b",
    r"visa (?:americana|de trabajo|laboral|vigente|requerida|required|is required)",
    r"(?:requires?|must have|need) (?:a |an )?(?:valid )?(?:work )?visa\b",
    r"visa sponsorship (?:is )?(?:available|provided|offered|required)",
    r"(?:we|will|can) (?:will )?(?:sponsor|provide) (?:your |a |work )?visas?",
    r"relocation (?:is )?(?:required|necessary|mandatory)|(?:must|required to|need to|willing(?:ness)? to|able to) relocate",
    r"relocation (?:assistance|package|support|bonus)",
    r"(?:requiere|disponibilidad (?:para|de)|dispuest[oa] a) (?:reubicarse|reubicacion|cambiar de residencia|cambio de residencia|relocalizacion|relocacion)",
    r"(?:reubicacion|relocacion) (?:requerida|obligatoria|necesaria)",
]
_VISA_RELOCATION_RE = re.compile("|".join(f"(?:{p})" for p in _VISA_RELOCATION_PATTERNS))

_FAST_BASE = (
    r"easy apply|solicitud (?:sencilla|rapida)|postulacion (?:rapida|sencilla)|quick apply"
    r"|contratacion inmediata|incorporacion inmediata|ingreso inmediato|inicio inmediato"
    r"|immediate (?:hir(?:e|ing)|start|joiner)|hiring immediately|urgent(?:ly)? hiring"
    r"|start (?:immediately|asap)|proceso (?:corto|rapido|agil)|respuesta rapida"
    r"|fast (?:hiring )?process|quick (?:hiring )?process"
)
_YEARS_RE = re.compile(
    r"(\d{1,2})\s*(?:\+|\s*(?:-|a|to|–)\s*(\d{1,2}))?\s*\+?\s*(?:years?|yrs?|anos?)"
    r"(?:\s+\w+){0,4}?\s+(?:of\s+)?(?:experienc|exp\b)"
)
_YEARS_RE_ALT = re.compile(
    r"experienc\w*\s*(?:\w+\s+){0,3}?(?:de\s+|of\s+)?(?:al menos\s+|minim[oa]\s+(?:de\s+)?|at least\s+)?"
    r"(\d{1,2})\s*\+?\s*(?:years?|yrs?|anos?)"
)
_HOSPITALITY_RE = re.compile(
    r"\b(?:hospitality|hoteler[ioa]\w*|hotel(?:s|es)?|hospedaje|travel ?tech|pms|property management system"
    r"|guest experience|resorts?|short[ -]term rentals?|vacation rentals?)\b"
)


def load_filter_config(path: str | Path = DEFAULT_CONFIG) -> dict:
    """Carga filter-config.json (pesos, umbrales de estrellas, keywords off, países, health)."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def build_fast_regex(config: dict) -> re.Pattern:
    """Señales de proceso rápido: las base + las de ``filters.fast_process_signals``."""
    extra = [
        re.escape(normalize(signal))
        for signal in config.get("filters", {}).get("fast_process_signals", [])
        if "<" not in signal  # "publicada hace < 7 días" se evalúa por fecha, no por texto
    ]
    return re.compile("|".join([_FAST_BASE] + extra))


def years_required(text_norm: str) -> int | None:
    """Menor número de años de experiencia pedido explícitamente, o None (solo bono débil)."""
    found = [int(m.group(1)) for m in _YEARS_RE.finditer(text_norm)]
    found += [int(m.group(1)) for m in _YEARS_RE_ALT.finditer(text_norm)]
    found = [n for n in found if 0 < n <= 25]
    return min(found) if found else None


def salary_status(low, high, pay_min: float, fcfg: dict) -> str:
    """``target`` | ``unknown`` | ``open_min_below`` | ``slightly_below`` | ``low`` | ``clearly_below``.

    Nunca se inventa: sin datos = ``unknown``. Con solo un mínimo por debajo
    del objetivo (sin máximo) = ``open_min_below`` (el techo es desconocido).
    """
    if low is None and high is None:
        return "unknown"
    if (low is not None and low >= pay_min) or (high is not None and high >= pay_min):
        return "target"
    if high is None:
        return "open_min_below"
    ratio = high / pay_min
    if ratio >= fcfg["salary"]["slightly_below_ratio"]:
        return "slightly_below"
    if ratio >= fcfg["salary"]["low_ratio"]:
        return "low"
    return "clearly_below"


def fit_level(kw_points: float, stars_cfg: dict) -> str:
    if kw_points >= stars_cfg["fit_strong_min"]:
        return "strong"
    if kw_points >= stars_cfg["fit_good_min"]:
        return "good"
    if kw_points >= stars_cfg["fit_weak_min"]:
        return "weak"
    return "none"


def build_seniority_regex(fcfg: dict) -> re.Pattern | None:
    """Regex de título senior (``seniority.title_re``) o None si la config no lo trae (≤ 2.0.0)."""
    pattern = (fcfg.get("seniority") or {}).get("title_re")
    return re.compile(pattern, re.I) if pattern else None


def build_region_matcher(fcfg: dict) -> tuple[re.Pattern | None, tuple[str, ...]]:
    """(regex título+ubicación, países/ciudades en ubicación) de ``region.*``; vacíos si faltan."""
    region = fcfg.get("region") or {}
    pattern = region.get("excluded_title_location_re")
    countries = tuple(normalize(c) for c in region.get("excluded_location_countries", []) if c)
    return (re.compile(pattern, re.I) if pattern else None), countries


def is_senior_title(title_norm: str, senior_re: re.Pattern | None) -> bool:
    """True si el TÍTULO (normalizado) es senior/chief/lead/staff/principal… La descripción no entra."""
    return bool(senior_re and senior_re.search(title_norm))


def is_region_excluded(title_norm: str, loc_norm: str, region_re: re.Pattern | None,
                       countries: tuple[str, ...]) -> bool:
    """True si título + ubicación indican contratación restringida a Europa/Asia.

    La descripción NO entra (listas de países globales y boilerplate APAC/EMEA dan
    falsos positivos). Región desconocida = neutra (False).
    """
    if region_re and region_re.search(f"{title_norm} {loc_norm}"):
        return True
    return any(c in loc_norm for c in countries)


def compute_stars(modality: str, fit: str, sal: str, fast_or_recent: bool,
                  friction: bool, stars_cfg: dict, senior: bool = False,
                  region_excluded: bool = False) -> int:
    """Estrellas 1-5 según ranking-rules.md v2 (umbrales en filter-config.json).

    Los caps de título senior y de región excluida (config ≥ 2.1.0) van DESPUÉS de
    todas las demás reglas. Si la config no trae el cap, no se aplica (compatibilidad 2.0.0).
    """
    tier = modality if modality in stars_cfg["base"] else "no_especificada"
    stars = stars_cfg["base"][tier][fit]
    remote = modality == "remoto"
    if remote and fit == "strong" and (sal == "target" or fast_or_recent):
        stars = 5
    penalty = 0 if remote else stars_cfg["non_remote_penalty"]
    if sal == "slightly_below":
        stars = min(stars, stars_cfg["salary_slightly_below_cap"])
    elif sal == "low":
        stars = min(stars, stars_cfg["salary_low_cap"]) - penalty
    elif sal == "clearly_below":
        stars = min(stars, stars_cfg["salary_clearly_below_cap"]) - penalty
    if friction:
        stars = min(stars, stars_cfg["friction_cap_remote"]) if remote else stars - penalty
    if senior and stars_cfg.get("seniority_title_cap") is not None:
        stars = min(stars, stars_cfg["seniority_title_cap"])
    if region_excluded and stars_cfg.get("region_excluded_cap") is not None:
        stars = min(stars, stars_cfg["region_excluded_cap"])
    return max(1, min(5, int(stars)))


def score_row(row, config: dict, fcfg: dict, fast_re: re.Pattern, today: date) -> dict:
    """Evalúa una fila: modalidad, descarte, score 0-100, estrellas y desglose."""
    w = fcfg["weights"]
    disabled = {normalize(k) for k in fcfg.get("keywords", {}).get("disabled", [])}
    aristas = {a["id"]: a for a in config["aristas"]}
    pay_min = float(config["filters"]["pay_target_mxn"][0])
    title_norm = normalize(row["titulo"])
    desc_norm = normalize(row["descripcion"])
    loc_norm = normalize(row["ubicacion"])
    text_norm = f"{title_norm}\n{desc_norm}"
    parts: dict[str, float] = {}

    # 1) Alineación con las aristas: keywords en título (x2) y en descripción.
    points, seen, text_aristas, matched = 0, set(), set(), []
    for arista in config["aristas"]:
        for keyword in arista["keywords"]:
            key = normalize(keyword)
            if key in disabled:
                continue
            in_title = keyword_in(keyword, title_norm)
            in_desc = keyword_in(keyword, desc_norm)
            if in_title or in_desc:
                text_aristas.add(arista["id"])
            if key in seen:
                continue
            if in_title:
                points += w["keyword_title"]
                seen.add(key)
                matched.append(keyword)
            elif in_desc:
                points += w["keyword_desc"]
                seen.add(key)
                matched.append(keyword)
    parts["keywords"] = min(w["keywords_max"], points)

    # 2) Sueldo (peso máx. 8; desconocido = 4 neutro; nunca inventado).
    sal = salary_status(row["sueldo_min_mxn"], row["sueldo_max_mxn"], pay_min, fcfg)
    parts["sueldo"] = {
        "target": w["salary_target"], "unknown": w["salary_unknown"],
        "slightly_below": w["salary_slightly_below"], "open_min_below": w["salary_open_min_below"],
        "low": w["salary_low"], "clearly_below": w["salary_low"],
    }[sal]

    # 3) Publicación reciente.
    parts["reciente"] = w["recent_unknown"]
    recent = False
    if row["fecha_publicacion"]:
        try:
            age = (today - date.fromisoformat(row["fecha_publicacion"][:10])).days
            recent = age < w["recent_days"]
            parts["reciente"] = w["recent"] if recent else 0
        except ValueError:
            pass

    # 4) Proceso rápido.
    fast = bool(fast_re.search(text_norm))
    parts["proceso_rapido"] = w["fast_process"] if fast else 0

    # 5) Boost hotelería.
    hospitality = w["hospitality_arista"] in text_aristas or bool(_HOSPITALITY_RE.search(text_norm))
    parts["hoteleria"] = w["hospitality"] if hospitality else 0

    # 6) Prioridad de la arista.
    row_aristas = set(json.loads(row["aristas"] or "[]")) | text_aristas
    priorities = {str(aristas[i].get("priority", "")) for i in row_aristas if i in aristas}
    parts["prioridad"] = (
        w["priority"] if any(p == "main" or p.startswith("high") for p in priorities) else 0
    )

    # 7) Experiencia: señal DÉBIL. Bono pequeño, nunca penalización, nunca filtro.
    years = years_required(text_norm)
    if years is None:
        parts["experiencia"] = w["exp_none"]
    elif years <= 3:
        parts["experiencia"] = w["exp_low"]
    elif years <= 5:
        parts["experiencia"] = w["exp_mid"]
    else:
        parts["experiencia"] = w["exp_high"]

    # 8) Reapunte (config ≥ 2.1.0): título senior y región excluida. Solo título
    #    (senior) y título + ubicación (región); la descripción NO entra. Sin las
    #    secciones ``seniority`` / ``region`` en la config no hay partes, ni caps, ni flags.
    #    Se calcula antes del score porque las penalizaciones forman parte de la suma.
    senior_re = build_seniority_regex(fcfg)
    region_re, region_countries = build_region_matcher(fcfg)
    senior_title = is_senior_title(title_norm, senior_re)
    region_excluded = is_region_excluded(title_norm, loc_norm, region_re, region_countries)
    if senior_re is not None:
        penalty = w.get("seniority_title_penalty", fcfg["seniority"].get("score_penalty", 0))
        parts["seniority"] = -penalty if senior_title else 0
    if region_re is not None or region_countries:
        penalty = w.get("region_excluded_penalty", fcfg["region"].get("score_penalty", 0))
        parts["region"] = -penalty if region_excluded else 0

    # Acotado a [0, 100]: con 2.0.0 todas las partes son >= 0 y el piso es un no-op;
    # con las penalizaciones del reapunte mantiene el contrato "score 0-100".
    score = float(max(0, min(100, sum(parts.values()))))

    # Modalidad y único descarte.
    modality, signals = classify_modality(f"{title_norm}\n{loc_norm}\n{desc_norm}", row["modalidad"])
    reasons = []
    if modality == "presencial" and fcfg["hard_filter"].get("discard_onsite_only", True):
        reasons.append(DROP_ONSITE)

    friction = bool(_VISA_RELOCATION_RE.search(text_norm))
    fit = fit_level(parts["keywords"], fcfg["stars"])
    stars = None if reasons else compute_stars(
        modality, fit, sal, fast or recent, friction, fcfg["stars"],
        senior=senior_title, region_excluded=region_excluded)

    flags = []
    if senior_title:
        flags.append("senior_title")
    if region_excluded:
        flags.append("region_excluded")
    if sal == "unknown":
        flags.append("salary_unknown")
    if friction:
        flags.append("visa_relocacion")
    if modality == "no_especificada":
        flags.append("modalidad_no_especificada")
    return {
        "score": score, "parts": parts, "stars": stars, "reasons": reasons, "flags": flags,
        "modality": modality, "modality_signals": signals, "fit": fit, "salary": sal,
        "fast": fast, "recent": recent, "keywords": matched,
        "aristas": sorted(row_aristas),
    }


# --------------------------------------------------------------------------
# Evaluación sobre la base
# --------------------------------------------------------------------------

def ensure_columns(conn) -> list[str]:
    """Añade la columna ``estrellas`` si falta (no borra datos). Devuelve columnas añadidas."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(vacantes)")}
    added = []
    if "estrellas" not in cols:
        conn.execute("ALTER TABLE vacantes ADD COLUMN estrellas INTEGER")
        added.append("estrellas")
    if "modalidad_v2" not in cols:
        conn.execute("ALTER TABLE vacantes ADD COLUMN modalidad_v2 TEXT")
        added.append("modalidad_v2")
    conn.commit()
    return added


def evaluate(db_path: str = db.DEFAULT_DB, keywords_file: str = "keywords.json",
             all_rows: bool = False, config_file: str | Path = DEFAULT_CONFIG,
             write_health: bool = True, health_dir: str | Path | None = None) -> dict:
    """Evalúa las vacantes; escribe ``score``, ``estrellas``, ``modalidad_v2`` y ``pasa_filtro``.

    Devuelve ``evaluated``, ``passed``, ``dropped``, ``drop_reasons`` (Counter),
    ``stars`` (Counter), ``ranked`` (las que pasan: estrellas desc, score desc)
    y ``health_path`` (si se escribió el JSON de salud).
    """
    config = load_keywords(keywords_file)
    fcfg = load_filter_config(config_file)
    fast_re = build_fast_regex(config)
    today = datetime.now(timezone.utc).date()

    conn = db.init_db(db_path)
    ensure_columns(conn)
    where = "" if all_rows else "WHERE estado = 'nueva'"
    rows = conn.execute(f"SELECT * FROM vacantes {where}").fetchall()
    drop_reasons: Counter = Counter()
    stars_count: Counter = Counter()
    ranked, evaluated = [], []
    for row in rows:
        res = score_row(row, config, fcfg, fast_re, today)
        passes = not res["reasons"]
        conn.execute(
            "UPDATE vacantes SET score = ?, estrellas = ?, modalidad_v2 = ?, pasa_filtro = ? WHERE id = ?",
            (res["score"], res["stars"], res["modality"], 1 if passes else 0, row["id"]),
        )
        item = {
            "id": row["id"], "score": res["score"], "stars": res["stars"], "titulo": row["titulo"],
            "empresa": row["empresa"], "fuente": row["fuente"], "ubicacion": row["ubicacion"],
            "fecha_scrapeo": row["fecha_scrapeo"], "aristas": res["aristas"],
            "keywords": res["keywords"], "modality": res["modality"], "fit": res["fit"],
            "salary": res["salary"], "sueldo": row["sueldo"], "url": row["url"],
            "flags": res["flags"], "parts": res["parts"], "reasons": res["reasons"],
            "passes": passes,
        }
        evaluated.append(item)
        if passes:
            ranked.append(item)
            stars_count[res["stars"]] += 1
        else:
            drop_reasons.update(res["reasons"])
    conn.commit()
    conn.close()
    ranked.sort(key=lambda r: (-r["stars"], -r["score"], r["titulo"].lower()))
    result = {
        "evaluated": len(rows), "passed": len(ranked), "dropped": len(rows) - len(ranked),
        "drop_reasons": drop_reasons, "stars": stars_count, "ranked": ranked,
        "scope": "all" if all_rows else "estado=nueva", "health_path": None,
    }
    if write_health:
        import filter_health
        result["health_path"] = str(filter_health.write_health(
            result, evaluated, db_path=db_path, fcfg=fcfg, config=config,
            config_file=config_file, log_dir=health_dir,
        ))
    return result


def _group_key(item: dict) -> tuple[str, str]:
    """Empresa + título normalizados (solo alfanumérico), para agrupar la misma vacante
    publicada varias veces (p. ej. LinkedIn la publica una vez por país con otro id)."""
    norm = lambda s: " ".join(re.findall(r"[a-z0-9]+", normalize(s)))  # noqa: E731
    return norm(item.get("empresa")), norm(item.get("titulo"))


def group_ranked(ranked: list[dict]) -> list[list[dict]]:
    """Agrupa el ranking por empresa+título conservando el orden (el primero es el mejor).
    Solo presentación: no cambia score, estrellas ni filas de la base."""
    groups: dict[tuple, list] = {}
    for item in ranked:
        groups.setdefault(_group_key(item), []).append(item)
    return list(groups.values())


def format_report(result: dict, top: int | None = None) -> str:
    """Lista rankeada (estrellas, score) + conteos por estrella y motivos de descarte.

    El listado agrupa por empresa+título: cada vacante aparece una vez y las demás
    publicaciones del mismo rol se indican con ``(+N publicaciones más: ids)``.
    """
    groups = group_ranked(result["ranked"])
    shown = groups if top is None else groups[:top]
    lines = ["Ranking v2 (estrellas desc, score desc) — único descarte: 100% presencial:"]
    for rank, group in enumerate(shown, start=1):
        item = group[0]
        sueldo = item["sueldo"] or "[confirmar]"
        lines.append(
            f"{rank:>3}. {'★' * item['stars']:<5} [{item['score']:5.1f}] {item['titulo']} | "
            f"{item['empresa'] or '(sin empresa)'} | {item['fuente']} | {item['modality']} | "
            f"aristas {item['aristas']} | sueldo: {sueldo}"
        )
        lines.append(f"       {item['url']}")
        if len(group) > 1:
            ids = ", ".join(str(g["id"]) for g in group[1:8]) + (" …" if len(group) > 8 else "")
            lines.append(f"       (+{len(group) - 1} publicaciones más del mismo rol: ids {ids})")
    if not shown:
        lines.append("  (ninguna vacante pasó el filtro)")
    elif top is not None and len(groups) > top:
        lines.append(f"  ... y {len(groups) - top} vacantes distintas más (usa --top 0 para ver todas)")
    lines.append("")
    lines.append(
        f"Evaluadas: {result['evaluated']} | Pasan: {result['passed']} | "
        f"Descartadas: {result['dropped']}"
    )
    dup_groups = [g for g in groups if len(g) > 1]
    if dup_groups:
        lines.append(
            f"Vacantes distintas (empresa+título) que pasan: {len(groups)} | "
            f"publicaciones repetidas: {sum(len(g) - 1 for g in dup_groups)} en {len(dup_groups)} grupos"
        )
    stars = result.get("stars") or {}
    lines.append("Por estrellas: " + " | ".join(f"{s}★ {stars.get(s, 0)}" for s in (5, 4, 3, 2, 1)))
    if result["drop_reasons"]:
        lines.append("Motivos de descarte:")
        for reason, count in result["drop_reasons"].most_common(10):
            lines.append(f"  {count:>5}  {reason}")
    if result.get("health_path"):
        lines.append(f"Salud del filtro: {result['health_path']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Prueba del detector presencial
# --------------------------------------------------------------------------

ONSITE_TEST_CASES = [
    # (texto, se_espera_descarte)
    ("Analista de Operaciones. Modalidad: 100% presencial en Monterrey, lunes a viernes.", True),
    ("Tipo de trabajo: Empleo presencial. Sueldo: $30,000 al mes. Ubicación: Guadalajara.", True),
    ("This is a fully on-site role at our Guadalajara plant, five days a week.", True),
    ("Work modality: on-site. Location: Mexico City office. Monday to Friday.", True),
    ("Ejecutivo de cuenta. Trabajo en oficina en Polanco, horario de 9 a 6.", True),
    ("Puesto 100% remoto, no presencial. Trabajo desde casa en cualquier parte de México.", False),
    ("No requiere asistir de forma presencial: el equipo es remote-first.", False),
    ("Hybrid role: 3 days on-site and 2 days from home in CDMX.", False),
    ("Modalidad presencial con algunos días de home office.", False),
    ("Fully remote position. The hiring process includes an in-person interview and yearly team offsites in person.", False),
]


def run_onsite_tests() -> tuple[int, list[str]]:
    """Corre los 10 ejemplos. Devuelve (fallos, líneas de reporte)."""
    lines, failures = [], 0
    for text, expected in ONSITE_TEST_CASES:
        modality, signals = classify_modality(normalize(text))
        got = modality == "presencial"
        ok = got == expected
        failures += not ok
        kind = ("FALSO POSITIVO" if got else "FALSO NEGATIVO") if not ok else "ok"
        lines.append(f"[{kind:^14}] esperado={'descarta' if expected else 'conserva':8} "
                     f"modalidad={modality:15} señales={signals} | {text}")
    return failures, lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Filtro + ranking por estrellas (Ranker v2).")
    parser.add_argument("--db", default=db.DEFAULT_DB)
    parser.add_argument("--keywords-file", default="keywords.json")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--all", action="store_true",
                        help="evalúa todas las filas, no solo estado='nueva'")
    parser.add_argument("--top", type=int, default=0,
                        help="imprime solo las N mejores (0 = todas, default)")
    parser.add_argument("--no-health", action="store_true", help="no escribe el JSON de salud")
    parser.add_argument("--test-onsite", action="store_true",
                        help="prueba el detector presencial con 10 ejemplos y sale")
    args = parser.parse_args(argv)
    if args.test_onsite:
        failures, lines = run_onsite_tests()
        print("\n".join(lines))
        print(f"\nFallos: {failures}/{len(ONSITE_TEST_CASES)}")
        return 1 if failures else 0
    result = evaluate(args.db, args.keywords_file, args.all, args.config,
                      write_health=not args.no_health)
    print(format_report(result, top=args.top or None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
