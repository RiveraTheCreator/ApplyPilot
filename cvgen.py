#!/usr/bin/env python3
"""Fase 2 — generación de un CV .docx a medida por vacante (sin mentir).

Cómo funciona (por qué no puede inventar):
  * Todos los hechos viven en ``profile.json`` (viñetas ES/EN ya redactadas con
    ATOP y verificadas contra ``cv-template-base.tex`` + ``build_cv_alianza.py``).
  * Este módulo NO redacta hechos: analiza la vacante, elige familia de rol
    (titular + perfil), selecciona y reordena viñetas/habilidades por match,
    expande acrónimos ("Nombre completo (ACR)") y renderiza el .docx.
  * Lo que la vacante pide y Alex no tiene (``not_possessed``) se omite y se
    compensa reforzando la habilidad transferible; queda en el reporte de cobertura.
  * Lo no verificado nunca entra al documento: va a ``confirmar`` en la cobertura.

Reglas duras (cv-prompt.md): una columna, sin tablas/columnas/líneas, sin emojis,
sin "yo"/"mi", sin "Objetivo", sin "Referencias", e-mail:/LinkedIn:, ubicación en
cada empleo, orden cronológico inverso, Core Skills al final. PROHIBIDO texto
oculto/blanco/instrucciones para IAs: ``validate_docx`` lo comprueba en cada CV.

Uso:
  .venv/bin/python cvgen.py --vacante-id 200
  .venv/bin/python cvgen.py --top 5                # default: 5 mejores (4-5★) pendientes
  .venv/bin/python cvgen.py --all --confirmo-todo  # lote completo, solo con confirmación
  .venv/bin/python cvgen.py --vacante-id 200 --force   # regenerar (nunca si ya se envió)
  .venv/bin/python cvgen.py --vacante-id 200 --dry-run # genera en /tmp, no registra
  .venv/bin/python cvgen.py --validate ~/workspace/cv/cv-x.docx
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
import tempfile
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import db  # noqa: E402

PROFILE_PATH = BASE_DIR / "profile.json"
CV_DIR = Path.home() / "workspace" / "cv"
LOG_DIR = BASE_DIR / "logs"
CRM_SCHEMA = Path.home() / "workspace" / "ts-spaces" / "crm-empleo" / "server" / "src" / "schema.ts"
CRM_IMPORT = BASE_DIR / "crm-import-2026-11-15.json"
NEXT_STEP = "CV listo; pendiente de aplicar (Fase 3)"
SENT_STATES = {"aplicada", "respuesta", "entrevista", "rechazada", "oferta", "contratada", "enviada"}
MIN_DESC_CHARS = 200
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

log = logging.getLogger("empleo.cvgen")

# --------------------------------------------------------------------------
# Esquema de registro (tabla propia; no toca columnas del ranker)
# --------------------------------------------------------------------------

CV_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS cv_generaciones (
    vacante_id     INTEGER PRIMARY KEY REFERENCES vacantes(id),
    estado         TEXT NOT NULL,      -- generado | duplicado | pendiente_descripcion | error
    cv_file        TEXT,               -- nombre del .docx en ~/workspace/cv/
    cv_version     TEXT,               -- "v1 2026-11-16"
    version_n      INTEGER,
    idioma         TEXT,               -- es | en
    familia        TEXT,
    duplicado_de   INTEGER,            -- vacante_id representante si es duplicado (empresa, título)
    motivo         TEXT,
    cobertura      TEXT,               -- JSON del reporte de cobertura
    actualizado    TEXT                -- ISO 8601 UTC
);
"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.executescript(CV_TABLE_SQL)
    conn.commit()


# --------------------------------------------------------------------------
# Utilidades de texto
# --------------------------------------------------------------------------

def strip_accents(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def slugify(text: str, limit: int = 60) -> str:
    s = strip_accents(text or "").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:limit].rstrip("-") or "sin-nombre"


_COMPANY_NOISE = {"inc", "llc", "ltd", "sa", "de", "cv", "sapi", "rl", "tech", "technologies", "technology",
                  "corp", "corporation", "group", "co", "company", "sas", "srl", "gmbh", "the", "mexico"}


def norm_company(name: str) -> str:
    words = re.findall(r"[a-z0-9]+", strip_accents(name or "").lower())
    kept = [w for w in words if w not in _COMPANY_NOISE]
    return " ".join(kept or words)


def norm_title(title: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", strip_accents(title or "").lower()))


def dedup_key(row) -> tuple[str, str]:
    return norm_company(row["empresa"]), norm_title(row["titulo"])


_ES_WORDS = re.compile(r"\b(de|la|el|los|las|para|con|en|una|del|que|por|experiencia|requisitos|empresa|trabajo)\b", re.I)
_EN_WORDS = re.compile(r"\b(the|and|for|with|you|our|will|experience|requirements|of|to|we|your|team)\b", re.I)


def detect_language(text: str) -> str:
    es = len(_ES_WORDS.findall(text or ""))
    en = len(_EN_WORDS.findall(text or ""))
    return "es" if es > en else "en"


# --------------------------------------------------------------------------
# Perfil
# --------------------------------------------------------------------------

def load_profile(path: Path = PROFILE_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Análisis de la vacante
# --------------------------------------------------------------------------

def _count(patterns: list[str], text: str) -> int:
    return sum(len(re.findall(p, text, re.I)) for p in patterns)


_YEARS_RE = re.compile(r"[^\n.]*?\b\d+\s*(?:[-–]\s*\d+\s*)?\+?\s*(?:years?|años)\b[^\n.]*", re.I)


def analyze_vacancy(row, profile: dict) -> dict:
    titulo = row["titulo"] or ""
    desc = row["descripcion"] or ""
    text = f"{titulo}\n{desc}"
    title_l = titulo.lower()

    # Habilidades que Alex SÍ tiene y que la vacante menciona.
    matched: dict[str, float] = {}
    for sk in profile["skills"]:
        n_desc = _count(sk["patterns"], desc)
        n_title = _count(sk["patterns"], titulo)
        if n_desc or n_title:
            matched[sk["id"]] = min(n_desc, 4) + 3 * min(n_title, 1)

    # Requisitos que Alex NO tiene: se omiten y se compensan.
    missing = []
    for gid, grp in profile["not_possessed"]["groups"].items():
        hits = [term for term, pat in grp["terms"].items() if re.search(pat, text, re.I)]
        if hits:
            missing.append({"grupo": gid, "terminos": hits, "boost_tags": grp["boost_tags"],
                            "compensacion": grp["compensation"]})

    # Familia de rol.
    fam_scores = {}
    for fid, fam in profile["families"].items():
        if fid.startswith("_") or not isinstance(fam, dict):
            continue
        t = sum(3 for p in fam["patterns"] if p in title_l)
        d = min(sum(1 for p in fam["patterns"] if p in desc.lower()), 4)
        pos = min([title_l.find(p) for p in fam["patterns"] if p in title_l] or [999])
        fam_scores[fid] = (t + d, -pos)
    best = max(fam_scores.items(), key=lambda kv: kv[1])
    familia = best[0] if best[1][0] > 0 else profile["families"]["default"]
    fam_scores = {k: v[0] for k, v in fam_scores.items()}

    # Pesos por tag para elegir viñetas.
    skills_by_id = {s["id"]: s for s in profile["skills"]}
    tags: dict[str, float] = {}
    for sid, w in matched.items():
        for tg in skills_by_id[sid]["tags"]:
            tags[tg] = tags.get(tg, 0) + w
    for tg in profile["families"][familia]["tags"]:
        tags[tg] = tags.get(tg, 0) + 2
    for m in missing:
        for tg in m["boost_tags"]:
            tags[tg] = tags.get(tg, 0) + 1.5

    years = sorted({" ".join(m.group(0).split())[:160] for m in _YEARS_RE.finditer(desc)})

    try:
        arista_kw = json.loads(row["keywords_matched"] or "[]")
    except ValueError:
        arista_kw = []

    return {
        "idioma": detect_language(desc or titulo),
        "familia": familia,
        "familia_scores": fam_scores,
        "matched": matched,
        "missing": missing,
        "tags": tags,
        "years_reqs": years,
        "arista_keywords": arista_kw,
        "english_required": "english" in matched,
    }


# --------------------------------------------------------------------------
# Construcción del contenido
# --------------------------------------------------------------------------

MONTHS = {
    "es": ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"],
    "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
}
LABELS = {
    "es": {"profile": "Perfil", "experience": "Experiencia", "projects": "Proyectos y comunidad",
           "publication": "Publicación", "education": "Educación",
           "skills": "Habilidades y certificaciones", "core": "Core Skills", "present": "actualidad"},
    "en": {"profile": "Profile", "experience": "Experience", "projects": "Projects and Community",
           "publication": "Publication", "education": "Education",
           "skills": "Skills and Certifications", "core": "Core Skills", "present": "Present"},
}


def fmt_month(ym: str | None, lang: str) -> str:
    if not ym:
        return LABELS[lang]["present"]
    y, m = ym.split("-")
    return f"{MONTHS[lang][int(m) - 1]} {y}"


def _score_tags(item_tags: list[str], weights: dict[str, float]) -> float:
    return sum(weights.get(t, 0) for t in item_tags)


def _join(items: list[str], lang: str) -> str:
    if len(items) <= 1:
        return "".join(items)
    conj = " y " if lang == "es" else " and "
    return ", ".join(items[:-1]) + conj + items[-1]


def build_content(profile: dict, analysis: dict, lang: str) -> dict:
    w = analysis["tags"]
    fam = profile["families"][analysis["familia"]]
    skills = profile["skills"]
    matched = analysis["matched"]

    def skill_rank(sk):
        return (-(matched.get(sk["id"], 0) * 10 + _score_tags(sk["tags"], w) * 0.1), skills.index(sk))

    ranked_skills = sorted(skills, key=skill_rank)
    matched_skills = [s for s in ranked_skills if s["id"] in matched]

    # Perfil: intro de la familia + habilidades REALES que hicieron match + cierre.
    def short(sk):
        return sk.get(f"core_{lang}") or sk[lang]

    tech = [short(s) for s in matched_skills if s["cat"] in ("ai", "dev", "infra", "consulting") and s["id"] != "degree"]
    if len(tech) < 3:
        tech += [short(s) for s in ranked_skills if s["cat"] in ("ai", "dev", "consulting") and short(s) not in tech]
    profile_text = " ".join([
        fam["intro"][lang],
        profile["skills_sentence"][lang].format(skills=_join(tech[:4], lang)),
        fam["close"][lang],
    ] + ([profile["english_sentence"][lang]] if analysis["english_required"] else []))

    # Experiencia: orden cronológico inverso (actuales primero, luego por fecha de fin/inicio).
    exps = sorted(profile["experience"], key=lambda e: (e["end"] or "9999-12", e["start"]), reverse=True)
    experience = []
    for e in exps:
        scored = sorted(e["bullets"], key=lambda b: (-_score_tags(b["tags"], w), e["bullets"].index(b)))
        chosen = [b for b in scored if b.get("core")]
        for b in scored:
            if len(chosen) >= e["max_bullets"]:
                break
            if b not in chosen:
                chosen.append(b)
        # La viñeta core (contexto del rol) va primero; el resto por relevancia para la vacante.
        chosen.sort(key=lambda b: (not b.get("core"), -_score_tags(b["tags"], w), e["bullets"].index(b)))
        experience.append({
            "title": e["title"][lang],
            "org": e.get("org_en", e["org"]) if lang == "en" else e["org"],
            "dates": f"{fmt_month(e['start'], lang)} - {fmt_month(e['end'], lang)}",
            "location": e["location"][lang],
            "bullets": [b[lang] for b in chosen],
            "bullet_ids": [b["id"] for b in chosen],
        })

    projects = sorted(profile["projects"]["items"], key=lambda p: (-_score_tags(p["tags"], w),
                                                                    profile["projects"]["items"].index(p)))
    projects = projects[: profile["projects"]["max"]]

    # Habilidades por categoría: categorías con más match primero; dentro, match primero.
    cats = profile["skill_categories"]
    by_cat: dict[str, list[dict]] = {}
    for s in ranked_skills:
        if s.get("show_in_skills", True):
            by_cat.setdefault(s["cat"], []).append(s)
    fixed_tail = ["certs", "lang"]
    movable = [c for c in by_cat if c not in fixed_tail]
    movable.sort(key=lambda c: -sum(matched.get(s["id"], 0) for s in by_cat[c]))
    skill_lines = [(cats[c][lang], [s[lang] for s in by_cat[c]]) for c in movable + fixed_tail if c in by_cat]

    # Core Skills: match directo primero, luego transferibles; solo palabras separadas por comas.
    core = []
    for s in matched_skills + ranked_skills:
        if s.get("show_in_skills", True) is False:
            continue
        name = short(s)
        if name not in core and "," not in name:
            core.append(name)
    core = core[:26]

    return {
        "lang": lang,
        "headline": fam["headline"][lang],
        "profile": profile_text,
        "experience": experience,
        "projects": [p[lang] for p in projects],
        "project_ids": [p["id"] for p in projects],
        "publication": profile["publication"][lang],
        "education": [e[lang] for e in profile["education"]],
        "skill_lines": skill_lines,
        "core": core,
    }


# --------------------------------------------------------------------------
# Acrónimos: primera aparición -> "Nombre completo (ACR)"
# --------------------------------------------------------------------------

ACRONYMS = {
    "es": [
        ("IA", "inteligencia artificial", None),
        ("APIs?", "interfaces de programación de aplicaciones", "API"),
        ("POC", "prueba de concepto", None),
        ("PMS", "sistema de gestión hotelera", None),
        ("B2B", "negocio a negocio", None),
        ("DMARC", "Domain-based Message Authentication, Reporting and Conformance", None),
        ("TRL", "nivel de madurez tecnológica", None),
        ("TI", "tecnologías de la información", None),
    ],
    "en": [
        ("AI", "artificial intelligence", None),
        ("APIs?", "application programming interface", "API"),
        ("POC", "proof of concept", None),
        ("PMS", "Property Management System", None),
        ("B2B", "Business-to-Business", None),
        ("DMARC", "Domain-based Message Authentication, Reporting and Conformance", None),
        ("TRL", "Technology Readiness Level", None),
        ("IT", "Information Technology", None),
    ],
}


def expand_acronyms(blocks: list[str], lang: str) -> list[str]:
    """Expande la primera aparición de cada acrónimo en el orden del documento."""
    introduced: set[str] = set()
    out = []
    for text in blocks:
        for pat, full, _canon in ACRONYMS[lang]:
            if pat in introduced:
                continue
            if re.search(rf"\(({pat})\)", text):
                introduced.add(pat)
                continue
            m = re.search(rf"(?<![\w(/-])(?<!Cloud )(?<!WhatsApp )({pat})(?![\w)-])", text)
            if not m:
                continue
            acr = m.group(1)
            plural = acr.endswith("s") and pat.endswith("s?")
            full_txt = full + ("s" if plural and lang == "en" else "")
            if plural and lang == "es":
                full_txt = full  # "interfaces ..." ya es plural en español
            text = text[: m.start()] + f"{full_txt} ({acr})" + text[m.end():]
            introduced.add(pat)
        out.append(text)
    return out


# --------------------------------------------------------------------------
# Render .docx (generaliza build_cv_alianza.py: Calibri 11, márgenes 2 cm)
# --------------------------------------------------------------------------

def render_docx(content: dict, profile: dict, out_path: Path) -> None:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm, Pt

    lang = content["lang"]
    L = LABELS[lang]
    ident = profile["identity"]

    # Expansión de acrónimos en orden de lectura (encabezado, publicación y Core Skills se excluyen).
    seq: list[str] = [content["profile"]]
    for e in content["experience"]:
        seq.extend(e["bullets"])
    seq.extend(content["projects"])
    seq.extend(content["education"])
    expanded = iter(expand_acronyms(seq, lang))
    content = dict(content)
    content["profile"] = next(expanded)
    content["experience"] = [dict(e, bullets=[next(expanded) for _ in e["bullets"]]) for e in content["experience"]]
    content["projects"] = [next(expanded) for _ in content["projects"]]
    content["education"] = [next(expanded) for _ in content["education"]]

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)
    style.paragraph_format.space_after = Pt(4)
    style.paragraph_format.space_before = Pt(0)
    for section in doc.sections:
        section.top_margin = Cm(1.8)
        section.bottom_margin = Cm(1.8)
        section.left_margin = Cm(2.0)
        section.right_margin = Cm(2.0)

    def para(text="", size=None, bold=False, after=4, before=0):
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        p.paragraph_format.space_after = Pt(after)
        p.paragraph_format.space_before = Pt(before)
        if text:
            r = p.add_run(text)
            r.bold = bold
            if size:
                r.font.size = Pt(size)
        return p

    def heading(text):
        return para(text.upper(), size=12, bold=True, after=4, before=12)

    def bullet(text):
        p = doc.add_paragraph(style="List Bullet")
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        p.paragraph_format.space_after = Pt(2)
        p.paragraph_format.left_indent = Cm(0.6)
        p.add_run(text)
        return p

    # Encabezado
    para(ident["name"], size=20, bold=True, after=2)
    para(content["headline"], size=12, after=4)
    para(f"e-mail: {ident['email']}  |  LinkedIn: {ident['linkedin']}  |  {ident['github']}  |  "
         f"{ident['phone']}  |  {ident['location'][lang]}", size=10, after=2)

    heading(L["profile"])
    para(content["profile"])

    heading(L["experience"])
    for e in content["experience"]:
        para(f"{e['title']} | {e['org']}", size=11, bold=True, after=2, before=8)
        para(f"{e['dates']} | {e['location']}", after=2)
        for b in e["bullets"]:
            bullet(b)

    heading(L["projects"])
    for p in content["projects"]:
        bullet(p)

    heading(L["publication"])
    para(content["publication"])

    heading(L["education"])
    for ed in content["education"]:
        bullet(ed)

    heading(L["skills"])
    for cat, items in content["skill_lines"]:
        bullet(f"{cat}: {', '.join(items)}.")

    heading(L["core"])
    para(", ".join(content["core"]))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))


# --------------------------------------------------------------------------
# Validación del .docx (reglas duras + anti texto oculto)
# --------------------------------------------------------------------------

_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF⭐⬆↔-⇿️]")


def docx_text(path: Path) -> tuple[list[str], object]:
    from docx import Document
    d = Document(str(path))
    return [p.text for p in d.paragraphs], d


def validate_docx(path: Path, profile: dict | None = None) -> dict:
    """Comprueba las reglas duras sobre el archivo final. Devuelve {check: bool} + detalles."""
    from docx.shared import Pt
    profile = profile or load_profile()
    paras, d = docx_text(path)
    full = "\n".join(paras)
    # Texto para chequeos de estilo: sin publicación (lleva iniciales "I. A.") ni el nombre de empresa "Mi Primera Chamba".
    style_text = "\n".join(p for p in paras if not p.startswith("Rivera Barrón, I. A.")).replace("Mi Primera Chamba", "")

    hidden = []
    for p in d.paragraphs:
        for r in p.runs:
            f = r.font
            color = None
            try:
                color = f.color.rgb if f.color and f.color.type is not None else None
            except Exception:  # noqa: BLE001
                color = None
            if f.hidden or (color is not None and str(color).upper() in ("FFFFFF", "FEFEFE")) \
                    or (f.size is not None and f.size < Pt(8)):
                hidden.append(r.text[:60])
    body_xml = d.element.body.xml
    sections_ok = all(len(s._sectPr.xpath("./w:cols[@w:num>1]")) == 0 for s in d.sections)

    forbidden_hits = [pat for pat in profile["forbidden_claims"] if re.search(pat, full, re.I)]
    checks = {
        "abre_con_python_docx": True,
        "cero_tablas": len(d.tables) == 0,
        "etiqueta_email": "e-mail:" in full,
        "etiqueta_linkedin": "LinkedIn:" in full,
        "core_skills": any(p.strip().upper() == "CORE SKILLS" for p in paras),
        "core_skills_al_final": [p.strip().upper() for p in paras if p.strip()][-2] == "CORE SKILLS",
        "sin_emojis": not _EMOJI_RE.search(full),
        "sin_objetivo": not re.search(r"\b(objetivo|objective)\b", full, re.I),
        "sin_referencias": not re.search(r"referencias disponibles|references available", full, re.I),
        "sin_pronombres": not re.search(r"\b(yo|mi|mis|me)\b|\b(I|my|me)\b(?!\.)", style_text),
        "sin_pasivas": not re.search(r"responsable de|mis deberes|responsible for|duties included", full, re.I),
        "una_columna": sections_ok and "<w:tbl>" not in body_xml and "w:pBdr" not in body_xml,
        "sin_texto_oculto": not hidden and "w:vanish" not in body_xml and "w:webHidden" not in body_xml,
        "sin_instrucciones_ia": not re.search(r"(ignore (all|previous)|ignora (todas|las) instrucciones|"
                                              r"language model|modelo de lenguaje|\bats:|chatgpt,? )", full, re.I),
        "sin_afirmaciones_prohibidas": not forbidden_hits,
        "sin_confirmar_en_documento": "[confirmar]" not in full.lower(),
    }
    return {"ok": all(checks.values()), "checks": checks, "forbidden_hits": forbidden_hits,
            "hidden_runs": hidden, "paragraphs": len(paras), "tables": len(d.tables)}


# --------------------------------------------------------------------------
# Cobertura
# --------------------------------------------------------------------------

def coverage_report(row, analysis: dict, content: dict, cv_text: str, profile: dict) -> dict:
    lang = content["lang"]
    skills_by_id = {s["id"]: s for s in profile["skills"]}
    low = cv_text.lower()
    covered, not_covered = [], []
    for sid in sorted(analysis["matched"], key=lambda k: -analysis["matched"][k]):
        sk = skills_by_id[sid]
        name = sk[lang] if sk[lang].lower() in low else (sk.get(f"core_{lang}") or sk[lang])
        (covered if name.lower() in low else not_covered).append(name)
    missing_terms = {t.lower() for m in analysis["missing"] for t in m["terminos"]}
    for kw in analysis["arista_keywords"]:
        if kw.lower() in missing_terms:
            continue
        target = covered if kw.lower() in low else not_covered
        if kw not in target:
            target.append(f"{kw} (keyword del ranker)" if target is not_covered else kw)
    for m in analysis["missing"]:
        not_covered.extend(m["terminos"])
    return {
        "vacante_id": row["id"],
        "empresa": row["empresa"],
        "titulo": row["titulo"],
        "url": row["url"],
        "idioma_cv": lang,
        "familia_rol": analysis["familia"],
        "keywords_cubiertas": covered,
        "keywords_no_cubiertas": not_covered,
        "requisitos_omitidos": [
            {"requisito": ", ".join(m["terminos"]), "motivo": "Alex no lo posee (cero mentiras): omitido",
             "habilidad_transferible": m["compensacion"][lang]} for m in analysis["missing"]
        ],
        "requisitos_de_anios_no_afirmados": analysis["years_reqs"],
        "confirmar": [c["es"] for c in profile.get("confirmar", [])],
        "vinetas_usadas": [bid for e in content["experience"] for bid in e["bullet_ids"]],
        "proyectos_usados": content["project_ids"],
    }


# --------------------------------------------------------------------------
# Descripción faltante -> Firecrawl (vía helper; nunca se lee la API key)
# --------------------------------------------------------------------------

def fetch_description(url: str) -> tuple[str | None, str | None]:
    if not url:
        return None, "vacante sin URL"
    try:
        import firecrawl_sources as fcs
    except Exception as exc:  # noqa: BLE001
        return None, f"no se pudo cargar firecrawl_sources ({type(exc).__name__})"
    payload, error, kind = fcs.call_tool("firecrawl_scrape", {
        "url": url, "formats": ["markdown"], "onlyMainContent": True})
    if error:
        return None, f"Firecrawl {kind}: {error}"
    text = fcs.clean_markdown(payload.get("markdown") or "")
    if len(text) < MIN_DESC_CHARS:
        return None, f"Firecrawl devolvió {len(text)} caracteres (< {MIN_DESC_CHARS}); probablemente login/bloqueo"
    if re.search(r"(sign in|inicia sesión|join now|log in) to (see|view|ver)|authwall|captcha", text[:1500], re.I) \
            and len(text) < 1500:
        return None, "Firecrawl devolvió un muro de login, no la vacante"
    return text[: db.MAX_DESCRIPCION], None


# --------------------------------------------------------------------------
# Registro: tabla, logs de cobertura y sync del CRM
# --------------------------------------------------------------------------

def crm_has_cvfile() -> bool | None:
    """True/False si el esquema local del artefacto crm-empleo tiene cvFile; None si no se puede leer."""
    try:
        return "cvFile" in CRM_SCHEMA.read_text(encoding="utf-8")
    except OSError:
        return None


def crm_import_keys() -> set[str]:
    try:
        data = json.loads(CRM_IMPORT.read_text(encoding="utf-8"))
        return {v.get("externalKey") or v.get("url") for v in data.get("vacancies", [])}
    except (OSError, ValueError):
        return set()


def _merge_json_log(path: Path, header: dict, key: str, entries: list[dict]) -> None:
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
    data.update(header)
    existing = {str(e.get(key)): e for e in data.get("entries", [])}
    for e in entries:
        existing[str(e[key])] = e
    data["entries"] = list(existing.values())
    data["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_logs(coverage: list[dict], crm: list[dict], today: str) -> tuple[Path, Path]:
    cov_path = LOG_DIR / f"cv-coverage-{today}.json"
    crm_path = LOG_DIR / f"cv-crm-sync-{today}.json"
    if coverage:
        _merge_json_log(cov_path, {"modulo": "cvgen.py (Fase 2)"}, "vacante_id", coverage)
    if crm:
        has = crm_has_cvfile()
        _merge_json_log(crm_path, {
            "modulo": "cvgen.py (Fase 2)",
            "action": ("crm-empleo › updateVacancy por cada entrada con los campos de 'updateVacancy' "
                       "(cvVersion, cvFile, nextStep). NO tocar status ni notes. Opcional: uploadCv "
                       "(vacancyId, filename, mimeType, dataBase64 del archivo en 'cv_path') para que Alex "
                       "lo abra desde el detalle."),
            "match_key": "externalKey (= url de la vacante). crm_id = null: esta sesión no tiene acceso al CRM.",
            "cvFile_field": {True: "existe (ts-spaces/crm-empleo/server/src/schema.ts: cv_file; updateVacancy lo acepta)",
                             False: "NO existe: el nombre del archivo va en cvVersion y nextStep (hueco pendiente)",
                             None: "no verificable desde esta sesión: se envía cvFile y también va en cvVersion"}[has],
            "si_no_existe_en_crm": ("Si externalKey no matchea ninguna fila (p. ej. vacantes nuevas de Firecrawl "
                                    "posteriores al import del 2026-11-15), crearla con importVacancies "
                                    "(externalKey=url) y luego aplicar updateVacancy."),
        }, "vacante_id", crm)
    return cov_path, crm_path


def crm_entry(row, cv_file: str, version: str, cv_path: Path, import_keys: set[str], dup_of: int | None = None) -> dict:
    has = crm_has_cvfile()
    upd = {"cvVersion": version if has else f"{version} — {cv_file}", "nextStep": NEXT_STEP}
    if has is not False:
        upd["cvFile"] = cv_file
    if has is False:
        upd["nextStep"] = f"{NEXT_STEP} — CV: {cv_file}"
    return {
        "vacante_id": row["id"],
        "crm_id": None,
        "externalKey": row["url"],
        "title": row["titulo"],
        "company": row["empresa"],
        "en_import_crm_2026_11_15": row["url"] in import_keys,
        "duplicado_de": dup_of,
        "updateVacancy": upd,
        "uploadCv": {"filename": cv_file, "mimeType": DOCX_MIME, "cv_path": str(cv_path)},
    }


# --------------------------------------------------------------------------
# Orquestación
# --------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_record(conn, vid: int):
    return conn.execute("SELECT * FROM cv_generaciones WHERE vacante_id = ?", (vid,)).fetchone()


def upsert_record(conn, **f) -> None:
    f["actualizado"] = _now()
    cols = ", ".join(f)
    marks = ", ".join("?" for _ in f)
    updates = ", ".join(f"{k}=excluded.{k}" for k in f if k != "vacante_id")
    conn.execute(f"INSERT INTO cv_generaciones ({cols}) VALUES ({marks}) "
                 f"ON CONFLICT(vacante_id) DO UPDATE SET {updates}", list(f.values()))
    conn.commit()


def duplicates_of(conn, row) -> list:
    key = dedup_key(row)
    rows = conn.execute("SELECT * FROM vacantes WHERE id != ?", (row["id"],)).fetchall()
    return [r for r in rows if dedup_key(r) == key]


def choose_filename(conn, row, group_ids: set[int]) -> str:
    base = f"cv-{slugify(row['empresa'], 40)}-{slugify(row['titulo'], 60)}"
    name = f"{base}.docx"
    owner = conn.execute("SELECT vacante_id FROM cv_generaciones WHERE cv_file = ? AND estado = 'generado'",
                         (name,)).fetchone()
    if owner and owner["vacante_id"] not in group_ids:
        name = f"{base}-{row['id']}.docx"
    elif not owner and (CV_DIR / name).exists():
        name = f"{base}-{row['id']}.docx"  # archivo ajeno al pipeline (p. ej. CV hecho a mano): no se pisa
    return name


def generate_for(conn, row, profile: dict, *, force: bool = False, dry_run: bool = False,
                 out_dir: Path = CV_DIR, import_keys: set[str] | None = None) -> dict:
    """Genera el CV de una vacante. Devuelve un dict de resultado (nunca lanza por datos)."""
    import_keys = import_keys if import_keys is not None else crm_import_keys()
    vid = row["id"]
    today = date.today().isoformat()
    dups = duplicates_of(conn, row)
    group = [row] + dups
    group_ids = {r["id"] for r in group}

    if any((r["estado"] or "").lower() in SENT_STATES for r in group):
        return {"vacante_id": vid, "estado": "omitida", "motivo": "ya enviada (estado aplicada o superior): no se toca"}
    prev = get_record(conn, vid)
    if prev and prev["estado"] in ("generado", "duplicado") and not force:
        return {"vacante_id": vid, "estado": "omitida", "motivo": f"ya tiene CV ({prev['cv_file']}); usa --force"}

    # Descripción: si falta, Firecrawl; si no se obtiene, pendiente sin CV.
    desc = (row["descripcion"] or "").strip()
    if len(desc) < MIN_DESC_CHARS:
        donor = next((r for r in dups if len((r["descripcion"] or "").strip()) >= MIN_DESC_CHARS), None)
        if donor:
            desc = donor["descripcion"]
        elif dry_run:
            return {"vacante_id": vid, "estado": "pendiente_descripcion", "motivo": "sin descripción (dry-run no scrapea)"}
        else:
            text, err = fetch_description(row["url"])
            if not text:
                upsert_record(conn, vacante_id=vid, estado="pendiente_descripcion", motivo=err)
                return {"vacante_id": vid, "estado": "pendiente_descripcion", "motivo": err}
            conn.execute("UPDATE vacantes SET descripcion = ? WHERE id = ?", (text, vid))
            conn.commit()
            desc = text
        row = dict(row)
        row["descripcion"] = desc

    analysis = analyze_vacancy(row, profile)
    lang = analysis["idioma"]
    content = build_content(profile, analysis, lang)

    if dry_run:
        out_dir = Path(tempfile.mkdtemp(prefix="cvgen-dry-"))
        cv_file = f"cv-{slugify(row['empresa'], 40)}-{slugify(row['titulo'], 60)}.docx"
    else:
        cv_file = prev["cv_file"] if (prev and prev["cv_file"]) else choose_filename(conn, row, group_ids)
    out_path = out_dir / cv_file
    render_docx(content, profile, out_path)

    val = validate_docx(out_path, profile)
    paras, _d = docx_text(out_path)
    cov = coverage_report(row, analysis, content, "\n".join(paras), profile)
    cov["validacion"] = val["checks"]
    cov["cv_file"] = cv_file

    if not val["ok"]:
        failed = [k for k, v in val["checks"].items() if not v]
        if not dry_run:
            out_path.unlink(missing_ok=True)
            upsert_record(conn, vacante_id=vid, estado="error", motivo=f"validación falló: {failed}",
                          cobertura=json.dumps(cov, ensure_ascii=False))
        return {"vacante_id": vid, "estado": "error", "motivo": f"validación falló: {failed}", "cobertura": cov,
                "detalle": val}

    n = (prev["version_n"] or 0) + 1 if prev and prev["version_n"] else 1
    version = f"v{n} {today}"
    cov["cv_version"] = version
    result = {"vacante_id": vid, "estado": "generado", "cv_file": cv_file, "cv_path": str(out_path),
              "cv_version": version, "idioma": lang, "familia": analysis["familia"], "cobertura": cov,
              "duplicados": [r["id"] for r in dups], "validacion": val}
    if dry_run:
        return result

    upsert_record(conn, vacante_id=vid, estado="generado", cv_file=cv_file, cv_version=version, version_n=n,
                  idioma=lang, familia=analysis["familia"], duplicado_de=None, motivo=None,
                  cobertura=json.dumps(cov, ensure_ascii=False))
    crm = [crm_entry(row, cv_file, version, out_path, import_keys)]
    for d in dups:
        upsert_record(conn, vacante_id=d["id"], estado="duplicado", cv_file=cv_file, cv_version=version,
                      version_n=n, idioma=lang, familia=analysis["familia"], duplicado_de=vid,
                      motivo=f"misma (empresa, título) que la vacante {vid}", cobertura=None)
        crm.append(crm_entry(d, cv_file, version, out_path, import_keys, dup_of=vid))
    write_logs([cov], crm, today)
    return result


def ranked_candidates(conn, min_stars: int) -> list:
    rows = conn.execute(
        "SELECT * FROM vacantes WHERE pasa_filtro = 1 AND estado = 'nueva' AND estrellas >= ?", (min_stars,)
    ).fetchall()
    rows = sorted(rows, key=lambda r: (-(r["estrellas"] or 0), -(r["score"] or 0), (r["titulo"] or "").lower()))
    seen, out = set(), []
    for r in rows:
        k = dedup_key(r)
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def pending_for_today(conn, row) -> bool:
    """True si la vacante ya se intentó hoy y quedó pendiente de descripción (no re-scrapear el mismo día)."""
    rec = get_record(conn, row["id"])
    return bool(rec and rec["estado"] == "pendiente_descripcion" and (rec["actualizado"] or "")[:10] == date.today().isoformat())


def run_batch(conn, profile: dict, limit: int | None, min_stars: int, force: bool, dry_run: bool) -> list[dict]:
    results = []
    import_keys = crm_import_keys()
    for row in ranked_candidates(conn, min_stars):
        if limit is not None and sum(1 for r in results if r["estado"] == "generado") >= limit:
            break
        rec = get_record(conn, row["id"])
        if rec and rec["estado"] in ("generado", "duplicado") and not force:
            continue
        if pending_for_today(conn, row):
            continue
        res = generate_for(conn, row, profile, force=force, dry_run=dry_run, import_keys=import_keys)
        log.info("vacante %s: %s %s", row["id"], res["estado"], res.get("cv_file") or res.get("motivo") or "")
        results.append(res)
    return results


def print_summary(results: list[dict]) -> None:
    for r in results:
        if r["estado"] == "generado":
            c = r["cobertura"]
            print(f"[OK] {r['vacante_id']} {c['empresa']} — {c['titulo']} -> {r['cv_file']} ({r['cv_version']}, "
                  f"{r['idioma']}, familia {r['familia']}) | duplicados: {r['duplicados'] or '-'}")
            print(f"     cubiertas ({len(c['keywords_cubiertas'])}): {', '.join(c['keywords_cubiertas'])}")
            print(f"     no cubiertas ({len(c['keywords_no_cubiertas'])}): {', '.join(c['keywords_no_cubiertas']) or '-'}")
            for o in c["requisitos_omitidos"]:
                print(f"     omitido: {o['requisito']} -> compensa: {o['habilidad_transferible']}")
            for y in c["requisitos_de_anios_no_afirmados"]:
                print(f"     años (no se afirman): {y}")
        else:
            print(f"[{r['estado'].upper()}] {r['vacante_id']}: {r.get('motivo')}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fase 2: CV .docx a medida por vacante (sin mentir).")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--vacante-id", type=int, action="append", help="id de vacantes.db (repetible)")
    mode.add_argument("--top", type=int, help="N mejores pendientes por estrellas/score (default 5)")
    mode.add_argument("--all", action="store_true", help="todas las pendientes (exige --confirmo-todo)")
    mode.add_argument("--validate", type=Path, help="solo valida un .docx existente")
    ap.add_argument("--confirmo-todo", action="store_true", help="confirmación explícita del operador para --all")
    ap.add_argument("--min-estrellas", type=int, default=4, help="estrellas mínimas para --top/--all (default 4)")
    ap.add_argument("--force", action="store_true", help="regenera aunque ya tenga CV (nunca si ya se envió)")
    ap.add_argument("--dry-run", action="store_true", help="genera en un directorio temporal y no registra nada")
    ap.add_argument("--db", default=str(BASE_DIR / db.DEFAULT_DB))
    ap.add_argument("--json", action="store_true", help="imprime los resultados como JSON")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    profile = load_profile()
    if args.validate:
        res = validate_docx(args.validate.expanduser(), profile)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["ok"] else 1

    conn = db.connect(args.db)
    ensure_table(conn)

    if args.vacante_id:
        results = []
        for vid in args.vacante_id:
            row = conn.execute("SELECT * FROM vacantes WHERE id = ?", (vid,)).fetchone()
            if not row:
                results.append({"vacante_id": vid, "estado": "error", "motivo": "no existe en vacantes.db"})
                continue
            results.append(generate_for(conn, row, profile, force=args.force, dry_run=args.dry_run))
    elif args.all:
        if not args.confirmo_todo:
            if sys.stdin.isatty():
                n = len(ranked_candidates(conn, args.min_estrellas))
                ans = input(f"Generar CVs para hasta {n} vacantes (>= {args.min_estrellas}★)? Escribe SI: ")
                if ans.strip() != "SI":
                    print("Cancelado.")
                    return 1
            else:
                print("--all exige confirmación explícita: agrega --confirmo-todo.", file=sys.stderr)
                return 2
        results = run_batch(conn, profile, None, args.min_estrellas, args.force, args.dry_run)
    else:
        results = run_batch(conn, profile, args.top or 5, args.min_estrellas, args.force, args.dry_run)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
    else:
        print_summary(results)
    return 0 if all(r["estado"] != "error" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
