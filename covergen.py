#!/usr/bin/env python3
"""Carta de presentación + resumen profesional a medida por vacante (sin mentir).

Hermano de ``cvgen.py``. Misma disciplina:
  * Todos los hechos viven en ``profile.json`` (viñetas ES/EN verificadas, intro/cierre por
    familia de rol, identidad, compensaciones de ``not_possessed``, ``forbidden_claims``).
  * Este módulo NO redacta hechos: analiza la vacante, selecciona y reordena viñetas por match,
    y las une con marcos fijos (``FRAMES``) que no contienen ningún hecho ni ningún número.
  * Para pasar de la voz de CV ("Builds ...", "Construye ...") a primera persona solo se cambia
    la forma del verbo con tablas explícitas (``VERBS_1P`` / ``COORD_1P``); los tests verifican
    token por token que no cambia nada más.
  * Lo que la vacante pide y Alex no tiene se OMITE (ni siquiera se nombra) y se compensa con la
    habilidad transferible de ``not_possessed``.
  * Vacante sin descripción => sin carta (pendiente_descripcion). Nunca se genera a ciegas.

Reglas duras (``validate_text``): sin afirmaciones prohibidas, sin emojis, sin texto oculto/blanco,
sin instrucciones para IAs/ATS, sin números que no estén en profile.json o en la vacante, sin
afirmar años de experiencia, conteo de palabras en rango, idioma de la vacante. Si falla, el archivo
generado se BORRA (mismo patrón que ``cvgen.validate_docx``).

Solo lectura: vacantes.db se abre con ``mode=ro``; no escribe tablas, logs ni el perfil.
No hace llamadas de red (no Firecrawl).

Uso:
  .venv/bin/python covergen.py --vacante-id 1766                    # carta + resumen
  .venv/bin/python covergen.py --vacante-id 1766 --tipo carta --palabras 400
  .venv/bin/python covergen.py --top 3 --tipo resumen
  .venv/bin/python covergen.py --vacante-id 1325 --dry-run          # genera en /tmp e imprime
  .venv/bin/python covergen.py --vacante-id 1325 --force            # sobrescribe
  .venv/bin/python covergen.py --validate ~/workspace/cv/cover-x.txt
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import cvgen  # noqa: E402  (reutiliza análisis, slug, acrónimos, dedupe)
import db  # noqa: E402

OUT_DIR = cvgen.CV_DIR
MIN_DESC_CHARS = cvgen.MIN_DESC_CHARS
LETTER_MIN, LETTER_MAX = 250, 400
SUMMARY_MIN, SUMMARY_MAX = 60, 90
MAX_EVIDENCE = 3

# --------------------------------------------------------------------------
# Marcos fijos (sin hechos, sin números). Los {campos} se llenan SOLO con datos de
# profile.json o con título/empresa de la vacante.
# --------------------------------------------------------------------------

FRAMES = {
    "en": {
        "greeting": "Dear {empresa} hiring team,",
        "hook": "I am applying for the {titulo} position at {empresa}. I am a {intro}",
        "evidence_lead": "Some results from my recent work that relate to this role:",
        "evidence_org": "At {org}, {text}",
        "evidence_org_again": "Also at {org}, {text}",
        "evidence_project": "In a separate project, {text}",
        "mirror": "From what this role describes, I bring hands-on experience in {skills}.",
        "compensation": ("For the parts of your stack I have not worked with yet, the closest strengths "
                         "I bring are: {items}."),
        "compensation_one": ("For the parts of your stack I have not worked with yet, the closest strength "
                             "I bring is: {items}."),
        "based": "I am based in {location}, and would welcome a conversation about the {titulo} role.",
        "language": "Language level: {english}",
        "contact": "You can reach me at {email} or {phone}; LinkedIn: {linkedin}.",
        "signoff": "Sincerely,",
        "and": " and ",
    },
    "es": {
        "greeting": "Estimado equipo de {empresa}:",
        "hook": "Me postulo a la vacante de {titulo} en {empresa}. Soy {intro}",
        "evidence_lead": "Algunos resultados de mi trabajo reciente relacionados con este puesto:",
        "evidence_org": "En {org}, {text}",
        "evidence_org_again": "También en {org}, {text}",
        "evidence_project": "En otro proyecto, {text}",
        "mirror": "De lo que describe la vacante, aporto experiencia práctica en {skills}.",
        "compensation": ("Para las herramientas de su stack que aún no he usado, las fortalezas más cercanas "
                         "que aporto son: {items}."),
        "compensation_one": ("Para las herramientas de su stack que aún no he usado, la fortaleza más cercana "
                             "que aporto es: {items}."),
        "based": "Vivo en {location}, y me gustaría conversar sobre la vacante de {titulo}.",
        "language": "Nivel de idioma: {english}",
        "contact": "Puede contactarme en {email} o al {phone}; LinkedIn: {linkedin}.",
        "signoff": "Atentamente,",
        "and": " y ",
    },
}

# Voz de CV (3a persona / sujeto omitido) -> 1a persona. Solo cambia la forma del verbo.
VERBS_1P = {
    "en": {  # presente 3a persona -> forma base (se antepone "I"); pasados quedan igual
        "designs": "design", "builds": "build", "standardizes": "standardize", "packages": "package",
        "serves": "serve", "runs": "run", "conducts": "conduct", "defines": "define", "works": "work",
        "translates": "translate", "prioritizes": "prioritize", "combines": "combine",
        "coordinates": "coordinate", "takes": "take", "brings": "bring", "teaches": "teach",
        "co-founded": "co-founded", "identified": "identified", "designed": "designed", "sold": "sold",
        "built": "built", "co-authored": "co-authored", "organized": "organized", "automated": "automated",
        "reached": "reached", "used": "used", "led": "led", "created": "created", "implemented": "implemented",
    },
    "es": {
        "diseña": "diseño", "construye": "construyo", "estandariza": "estandarizo", "empaqueta": "empaqueto",
        "ejerce": "ejerzo", "opera": "opero", "ejecuta": "ejecuto", "define": "defino", "trabaja": "trabajo",
        "traduce": "traduzco", "prioriza": "priorizo", "combina": "combino", "coordina": "coordino",
        "lleva": "llevo", "aporta": "aporto", "imparte": "imparto",
        "cofundó": "cofundé", "detectó": "detecté", "diseñó": "diseñé", "vendió": "vendí",
        "construyó": "construí", "coautoró": "coautoré", "organizó": "organicé", "automatizó": "automaticé",
        "llegó": "llegué", "usó": "usé",
    },
}

# Verbos coordinados cuyo sujeto es la persona (no una cláusula relativa). Frase exacta -> frase.
COORD_1P = {
    "en": {
        "and designs the integration": "and design the integration",
        ": teaches Ship It Live": ": teach Ship It Live",
        "and keeps the relationship": "and keep the relationship",
        "and documents what gets built": "and document what gets built",
    },
    "es": {
        "y las validó": "y las validé",
        "e instaló": "e instalé",
        "y cerró el": "y cerré el",
        "; construyó en Base44": "; construí en Base44",
        "y redujo el proceso": "y reduje el proceso",
        ": imparte Ship It Live": ": imparto Ship It Live",
        "y diseña la integración": "y diseño la integración",
        "y mantiene la relación": "y mantengo la relación",
        "y documenta lo que construye": "y documento lo que construyo",
    },
}

_SENT_SPLIT = re.compile(r"(?<=\.)\s+(?=[A-ZÁÉÍÓÚÑ])")


def to_first_person(text: str, lang: str, *, lower_first: bool = True) -> str | None:
    """Convierte una viñeta en voz de CV a primera persona. None si el verbo inicial no está en la tabla."""
    table = VERBS_1P[lang]
    out = []
    for i, sent in enumerate(_SENT_SPLIT.split(text.strip())):
        first, _, rest = sent.partition(" ")
        conv = table.get(first.lower())
        if conv is None:
            if i == 0:
                return None
            out.append(sent)
            continue
        if lang == "en":
            out.append(f"I {conv} {rest}".rstrip())
        else:
            verb = conv if (i == 0 and lower_first) else conv[:1].upper() + conv[1:]
            out.append(f"{verb} {rest}".rstrip())
    res = " ".join(out)
    for src, dst in COORD_1P[lang].items():
        res = res.replace(src, dst)
    return res


def expand_acronyms(blocks: list[str], lang: str) -> list[str]:
    """cvgen.expand_acronyms, pero sin expandir un acrónimo seguido de paréntesis
    ("Generative AI (OpenAI, ...)" quedaría "artificial intelligence (AI) (OpenAI, ...)")."""
    masks = {}

    def mask(m):
        key = f"\u00a7{len(masks)}\u00a7"
        masks[key] = m.group(1)
        return key + " ("

    pats = [pat for pat, _full, _c in cvgen.ACRONYMS[lang]]
    masked = [re.sub(rf"(?<![\w(/-])((?:{'|'.join(pats)}))(?![\w)-]) \(", mask, b) for b in blocks]
    out = cvgen.expand_acronyms(masked, lang)
    for key, val in masks.items():
        out = [b.replace(key, val) for b in out]
    return out


def word_count(text: str) -> int:
    return len([t for t in text.split() if re.search(r"\w", t)])


def _ucfirst(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


# --------------------------------------------------------------------------
# Base de datos (solo lectura)
# --------------------------------------------------------------------------

def connect_ro(path: str | Path) -> sqlite3.Connection:
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(p)
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def get_vacancy(conn, vid: int):
    return conn.execute("SELECT * FROM vacantes WHERE id = ?", (vid,)).fetchone()


# --------------------------------------------------------------------------
# Análisis de la vacante
# --------------------------------------------------------------------------

_REQ_HEADER = re.compile(r"(requirements?|qualifications?|must have|what you.ll (need|bring)|what we.re looking for|"
                         r"skills|requisitos|buscamos|lo que necesitas|perfil)", re.I)
_STOP_HEADER = re.compile(r"(benefits?|beneficios|about (us|the company)|perks|what we offer|ofrecemos|"
                          r"compensation|salary|structure|sueldo|prestaciones)", re.I)


def extract_requirements(desc: str, profile: dict, limit: int = 25) -> list[dict]:
    """Líneas de requisitos de la vacante con las habilidades REALES del perfil que las cubren (solo reporte)."""
    lines = [re.sub(r"^[\s*\-•·]+|\*+", "", ln).strip() for ln in (desc or "").splitlines()]
    lines = [ln for ln in lines if ln]
    out, active = [], False
    for ln in lines:
        short = len(ln.split()) <= 6
        if short and _STOP_HEADER.search(ln):
            active = False
            continue
        if short and _REQ_HEADER.search(ln):
            active = True
            continue
        if not active or len(ln.split()) > 60:
            continue
        covered = [s["id"] for s in profile["skills"] if any(re.search(p, ln, re.I) for p in s["patterns"])]
        gaps = [t for g in profile["not_possessed"]["groups"].values() for t, pat in g["terms"].items()
                if re.search(pat, ln, re.I)]
        out.append({"requisito": ln[:200], "cubierto_por": covered, "no_posee": gaps})
        if len(out) >= limit:
            break
    return out


def analyze_vacancy(row, profile: dict) -> dict:
    """Análisis honesto: idioma, familia de rol, habilidades del perfil que hacen match y huecos."""
    base = cvgen.analyze_vacancy(row, profile)
    skills_by_id = {s["id"]: s for s in profile["skills"]}
    lang = base["idioma"]
    base["keywords"] = [skills_by_id[sid][lang] for sid in sorted(base["matched"], key=lambda k: -base["matched"][k])]
    base["requisitos"] = extract_requirements(row["descripcion"] or "", profile)
    base["titulo"] = row["titulo"] or ""
    base["empresa"] = row["empresa"] or ""
    base["url"] = row["url"] or ""
    return base


# --------------------------------------------------------------------------
# Selección de hechos
# --------------------------------------------------------------------------

def evidence_pool(profile: dict, analysis: dict, lang: str) -> list[dict]:
    """Viñetas cuantificadas (con número) convertibles a 1a persona, ordenadas por match con la vacante."""
    w = analysis["tags"]
    pool = []
    order = 0
    for e in profile["experience"]:
        org = e.get("org_en", e["org"]) if lang == "en" else e["org"]
        for b in e["bullets"]:
            order += 1
            pool.append({"id": b["id"], "kind": "org", "org": org, "src": b[lang], "tags": b["tags"],
                         "current": e["end"] is None, "order": order})
    for p in profile["projects"]["items"]:
        order += 1
        pool.append({"id": p["id"], "kind": "project", "org": None, "src": p[lang], "tags": p["tags"],
                     "current": False, "order": order})
    out = []
    for item in pool:
        if not re.search(r"\d", item["src"]) or re.search(r"\((see|ver) Publica", item["src"]):
            continue
        fp = to_first_person(item["src"], lang)
        if fp is None:
            continue
        item["first_person"] = fp
        item["score"] = cvgen._score_tags(item["tags"], w)
        out.append(item)
    out.sort(key=lambda it: (-it["score"], it["order"]))
    # Diversidad: máximo 2 viñetas por organización entre las primeras.
    picked, per_org = [], {}
    for it in out:
        key = it["org"] or it["id"]
        if per_org.get(key, 0) >= 2:
            continue
        per_org[key] = per_org.get(key, 0) + 1
        picked.append(it)
    return picked


def matched_skill_names(profile: dict, analysis: dict, lang: str, limit: int = 5, short: bool = False) -> list[str]:
    skills_by_id = {s["id"]: s for s in profile["skills"]}
    names = []
    for sid in sorted(analysis["matched"], key=lambda k: (-analysis["matched"][k],
                                                            [s["id"] for s in profile["skills"]].index(k))):
        sk = skills_by_id[sid]
        if sk["cat"] in ("soft", "lang", "certs") or sk.get("show_in_skills", True) is False:
            continue
        name = (sk.get(f"core_{lang}") or sk[lang]) if short else sk[lang]
        if sid == "hubspot":
            name = sk[lang]  # conserva el calificador "(user at Grupo Libera ...)": nunca "experto en HubSpot"
        if name not in names:
            names.append(name)
        if len(names) >= limit:
            break
    return names


def compensations(analysis: dict, lang: str, limit: int = 2) -> list[str]:
    out = []
    for m in sorted(analysis["missing"], key=lambda m: -len(m["terminos"])):
        txt = re.sub(r"^(transferable narrative|narrativa transferible):\s*", "", m["compensacion"][lang], flags=re.I)
        txt = txt.rstrip(".")
        if txt and txt not in out:
            out.append(txt)
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------
# Construcción
# --------------------------------------------------------------------------

def _evidence_line(item: dict, lang: str, repeated: bool = False) -> str:
    F = FRAMES[lang]
    if item["kind"] == "project":
        return F["evidence_project"].format(text=item["first_person"])
    return F["evidence_org_again" if repeated else "evidence_org"].format(org=item["org"], text=item["first_person"])


def _assemble_letter(profile, analysis, lang, evidence, use_mirror, use_comp, use_lang) -> dict:
    F = FRAMES[lang]
    ident = profile["identity"]
    fam = profile["families"][analysis["familia"]]
    titulo, empresa = analysis["titulo"], analysis["empresa"]
    close_1p = to_first_person(fam["close"][lang], lang, lower_first=False)

    paras: list[tuple[str, str | list[str]]] = []
    parts: list[dict] = []
    paras.append(("p", F["greeting"].format(empresa=empresa)))
    paras.append(("p", F["hook"].format(titulo=titulo, empresa=empresa, intro=fam["intro"][lang])))
    parts.append({"kind": "intro", "source": f"families.{analysis['familia']}.intro.{lang}", "text": fam["intro"][lang]})

    lines, seen_orgs = [], set()
    for it in evidence:
        lines.append(_evidence_line(it, lang, repeated=it["org"] in seen_orgs))
        if it["org"]:
            seen_orgs.add(it["org"])
    paras.append(("p", F["evidence_lead"]))
    paras.append(("bullets", lines))
    for it in evidence:
        parts.append({"kind": "evidence", "source": it["id"], "text": it["first_person"], "src_text": it["src"]})

    mid = []
    skills = matched_skill_names(profile, analysis, lang)
    if use_mirror and skills:
        mid.append(F["mirror"].format(skills=cvgen._join(skills, lang)))
        parts.append({"kind": "skills", "source": "skills", "text": skills})
    comps = compensations(analysis, lang)
    if use_comp and comps:
        mid.append(F["compensation" if len(comps) > 1 else "compensation_one"].format(items="; ".join(comps)))
        parts.append({"kind": "compensation", "source": "not_possessed", "text": comps})
    if mid:
        paras.append(("p", " ".join(mid)))

    close = []
    if close_1p:
        close.append(close_1p)
        parts.append({"kind": "close", "source": f"families.{analysis['familia']}.close.{lang}", "text": close_1p,
                      "src_text": fam["close"][lang]})
    close.append(F["based"].format(location=ident["location"][lang], titulo=titulo))
    if use_lang and analysis.get("english_required"):
        close.append(F["language"].format(english=profile["english_sentence"][lang]))
    close.append(F["contact"].format(email=ident["email"], phone=ident["phone"], linkedin=ident["linkedin"]))
    paras.append(("p", " ".join(close)))
    paras.append(("p", f"{F['signoff']}\n{ident['name']}"))

    # Acrónimos: primera aparición -> "Nombre completo (ACR)" (en orden de lectura).
    flat = []
    for kind, val in paras:
        flat.extend(val if kind == "bullets" else [val])
    expanded = iter(expand_acronyms(flat, lang))
    paras = [(k, [next(expanded) for _ in v] if k == "bullets" else next(expanded)) for k, v in paras]
    return {"paragraphs": paras, "parts": parts}


def paragraphs_to_text(paras) -> str:
    blocks = []
    for kind, val in paras:
        blocks.append("\n".join(f"- {b}" for b in val) if kind == "bullets" else val)
    # La despedida y el nombre van en líneas consecutivas.
    text = "\n\n".join(blocks)
    return text


def build_letter(profile: dict, analysis: dict, *, max_words: int = LETTER_MAX, min_words: int | None = None) -> dict:
    """Carta: gancho (rol + encaje) / evidencia (2-3 logros cuantificados) / cierre (disponibilidad + contacto)."""
    lang = analysis["idioma"]
    min_words = min_words if min_words is not None else min(LETTER_MIN, int(max_words * 0.625))
    pool = evidence_pool(profile, analysis, lang)
    if len(pool) < 2:
        return {"ok": False, "motivo": "menos de 2 logros cuantificados convertibles en profile.json"}
    n3 = min(MAX_EVIDENCE, len(pool))
    variants = [  # (n evidencias, mirror, compensación, idioma) en orden de preferencia
        (n3, True, True, True), (n3, True, False, True), (2, True, True, True), (2, True, False, True),
        (n3, False, False, True), (2, False, False, True), (2, False, False, False),
    ]
    best = None
    for n, mir, comp, lng in variants:
        built = _assemble_letter(profile, analysis, lang, pool[:n], mir, comp, lng)
        text = paragraphs_to_text(built["paragraphs"])
        wc = word_count(text)
        cand = dict(built, text=text, words=wc, lang=lang, min_words=min_words, max_words=max_words,
                    evidence_ids=[it["id"] for it in pool[:n]], variant={"evidencias": n, "mirror": mir,
                                                                         "compensacion": comp})
        if min_words <= wc <= max_words:
            return dict(cand, ok=True)
        if best is None or abs(wc - (min_words + max_words) / 2) < abs(best["words"] - (min_words + max_words) / 2):
            best = cand
    return dict(best, ok=False, motivo=f"no se logró {min_words}-{max_words} palabras (mejor: {best['words']})")


def build_summary(profile: dict, analysis: dict, *, min_words: int = SUMMARY_MIN, max_words: int = SUMMARY_MAX) -> dict:
    """Resumen profesional (~60-90 palabras) en voz de CV, con los mismos hechos de profile.json."""
    lang = analysis["idioma"]
    fam = profile["families"][analysis["familia"]]
    skills = matched_skill_names(profile, analysis, lang, limit=4, short=True)
    skills_sent = profile["skills_sentence"][lang].format(skills=cvgen._join(skills, lang)) if skills else None
    pool = evidence_pool(profile, analysis, lang)[:3]
    eng = profile["english_sentence"][lang] if analysis.get("english_required") else None
    intro = fam["intro"][lang]
    combos = []
    for b in pool:
        combos.append([intro, skills_sent, b["src"]])
    for b in pool:
        combos.append([intro, b["src"]])
    combos += [[intro, skills_sent, fam["close"][lang]], [intro, skills_sent], [intro, fam["close"][lang]]]
    if eng:
        combos = [c + [eng] for c in combos] + combos
    best = None
    for c in combos:
        sents = [s for s in c if s]
        text = " ".join(expand_acronyms([" ".join(sents)], lang))
        wc = word_count(text)
        cand = {"text": text, "words": wc, "lang": lang, "sources": sents, "min_words": min_words,
                "max_words": max_words}
        if min_words <= wc <= max_words:
            return dict(cand, ok=True)
        if best is None or abs(wc - (min_words + max_words) / 2) < abs(best["words"] - (min_words + max_words) / 2):
            best = cand
    return dict(best, ok=False, motivo=f"no se logró {min_words}-{max_words} palabras (mejor: {best['words']})")


# --------------------------------------------------------------------------
# Validación (reglas duras). Si falla durante la generación, el archivo se borra.
# --------------------------------------------------------------------------

_HIDDEN_CHARS = re.compile("[​-‏  ‪-‮⁠-⁤⁦-⁩﻿­"
                           "\U000e0000-\U000e007f-\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HIDDEN_MARKUP = re.compile(r"(display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0|"
                            r"color\s*:\s*(#?f{3}\b|#?f{6}\b|white)|opacity\s*:\s*0|<\s*/?\s*[a-z][^>]*>)", re.I)
_AI_INSTRUCTION = re.compile(
    r"(ignore (all |any )?(the )?(previous|prior|above|earlier|preceding) (instructions|prompts?|text)|"
    r"disregard (all |the )?(previous|prior|above)|ignora (todas )?(las )?instrucciones|"
    r"\b(if|when) you are an? (ai|llm|language model|bot|chatbot|assistant|screening)|"
    r"\bas an ai\b|language model|modelo de lenguaje|\b(system|hidden) prompt|\bprompt injection|"
    r"\b(note|message|instructions?) (to|for) (the )?(ai|ats|llm|bot|screen|recruit\w* (ai|bot))|"
    r"\bats\s*:|\bchatgpt\b|\bllm\s*:|"
    r"\b(rank|rate|score|classify|mark|flag) (this|the) (candidate|applicant|application|resume|cv)\b|"
    r"(recommend|shortlist|advance|select) this (candidate|applicant)|"
    r"you (must|should|will) (select|shortlist|recommend|advance|hire)|"
    r"<\s*/?\s*(system|instructions?|prompt)\s*>|\[\s*(system|instructions?|inst)\s*\])", re.I)
_YEARS_CLAIM = re.compile(r"\b\d+\s*\+?\s*(?:years?|yrs?|años)\b", re.I)
_LEFTOVER = re.compile(r"(\{[a-z_]+\}|\[confirmar\]|\bTODO\b|\bXXX\b|lorem ipsum)", re.I)
_NUM = re.compile(r"\d+(?:[.,]\d+)*")


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in _NUM.findall(text or "")}


def validate_text(text: str, profile: dict, *, kind: str = "carta", min_words: int | None = None,
                  max_words: int | None = None, lang: str | None = None, vacancy=None) -> dict:
    """Reglas duras sobre el texto final. Devuelve {ok, checks, ...}. No borra nada por sí mismo."""
    if min_words is None:
        min_words = SUMMARY_MIN if kind == "resumen" else LETTER_MIN
    if max_words is None:
        max_words = SUMMARY_MAX if kind == "resumen" else LETTER_MAX
    titulo = (vacancy["titulo"] if vacancy is not None else "") or ""
    empresa = (vacancy["empresa"] if vacancy is not None else "") or ""
    desc = (vacancy["descripcion"] if vacancy is not None else "") or ""

    # Nombrar el rol o la empresa no es afirmar nada: se quitan antes del chequeo de afirmaciones.
    claim_text = text
    for s in (titulo, empresa):
        if s:
            claim_text = claim_text.replace(s, " ")
    forbidden_hits = [pat for pat in profile["forbidden_claims"] if re.search(pat, claim_text, re.I)]

    allowed_nums = _numbers(json.dumps(profile, ensure_ascii=False)) | _numbers(desc) | _numbers(titulo) \
        | _numbers(empresa)
    invented = sorted(n for n in _numbers(text) if n not in allowed_nums)
    profile_blob = json.dumps(profile, ensure_ascii=False).lower()
    years = [m.group(0) for m in _YEARS_CLAIM.finditer(text) if m.group(0).lower() not in profile_blob]

    wc = word_count(text)
    detected = cvgen.detect_language(text)
    checks = {
        "sin_afirmaciones_prohibidas": not forbidden_hits,
        "sin_emojis": not cvgen._EMOJI_RE.search(text),
        "sin_texto_oculto": not _HIDDEN_CHARS.search(text) and not _HIDDEN_MARKUP.search(text),
        "sin_instrucciones_ia": not _AI_INSTRUCTION.search(text),
        "sin_numeros_inventados": not invented,
        "sin_anios_afirmados": not years,
        "sin_marcadores_pendientes": not _LEFTOVER.search(text),
        "palabras_en_rango": min_words <= wc <= max_words,
        "idioma_correcto": lang is None or detected == lang,
    }
    return {"ok": all(checks.values()), "checks": checks, "palabras": wc, "rango": [min_words, max_words],
            "idioma_detectado": detected, "idioma_esperado": lang, "forbidden_hits": forbidden_hits,
            "numeros_inventados": invented, "anios_afirmados": years}


def docx_paragraph_texts(path: Path) -> tuple[list[str], object]:
    from docx import Document
    d = Document(str(path))
    return [p.text for p in d.paragraphs], d


def docx_hidden_issues(d) -> list[str]:
    """Runs ocultos, blancos o diminutos; tablas; marcas vanish/webHidden (mismo criterio que cvgen)."""
    from docx.shared import Pt
    issues = []
    for p in d.paragraphs:
        for r in p.runs:
            f = r.font
            try:
                color = f.color.rgb if f.color and f.color.type is not None else None
            except Exception:  # noqa: BLE001
                color = None
            if f.hidden or (color is not None and str(color).upper() in ("FFFFFF", "FEFEFE", "FFFFFE")) \
                    or (f.size is not None and f.size < Pt(8)):
                issues.append(f"run oculto/blanco/diminuto: {r.text[:60]!r}")
    xml = d.element.body.xml
    if "w:vanish" in xml or "w:webHidden" in xml or "w:specVanish" in xml:
        issues.append("marca w:vanish/webHidden en el XML")
    if d.tables:
        issues.append(f"{len(d.tables)} tabla(s)")
    return issues


def validate_file(path: Path, profile: dict, *, kind: str | None = None, min_words: int | None = None,
                  max_words: int | None = None, lang: str | None = None, vacancy=None,
                  delete_on_fail: bool = False) -> dict:
    """Valida un .txt o .docx generado. Con ``delete_on_fail`` borra el archivo si no pasa."""
    path = Path(path)
    if kind is None:
        kind = "resumen" if path.name.startswith("summary-") else "carta"
    extra = {}
    if path.suffix.lower() == ".docx":
        paras, d = docx_paragraph_texts(path)
        text = "\n".join(paras)
        issues = docx_hidden_issues(d)
        extra = {"docx_sin_texto_oculto": not issues}
    else:
        text = path.read_text(encoding="utf-8")
        issues = []
    res = validate_text(text, profile, kind=kind, min_words=min_words, max_words=max_words, lang=lang,
                        vacancy=vacancy)
    res["checks"].update(extra)
    res["ok"] = all(res["checks"].values())
    res["docx_issues"] = issues
    res["archivo"] = str(path)
    res["tipo"] = kind
    if not res["ok"] and delete_on_fail:
        path.unlink(missing_ok=True)
        res["borrado"] = True
    return res


# --------------------------------------------------------------------------
# Render .docx (Calibri 11, márgenes 2 cm, una columna, sin tablas, todo visible)
# --------------------------------------------------------------------------

def render_docx(paras, out_path: Path) -> None:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm, Pt

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)
    style.paragraph_format.space_after = Pt(8)
    for section in doc.sections:
        section.top_margin = section.bottom_margin = Cm(2.0)
        section.left_margin = section.right_margin = Cm(2.0)
    for kind, val in paras:
        if kind == "bullets":
            for b in val:
                p = doc.add_paragraph(style="List Bullet")
                p.alignment = WD_ALIGN_PARAGRAPH.LEFT
                p.paragraph_format.space_after = Pt(4)
                p.add_run(b)
        else:
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            p.add_run(val)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))


# --------------------------------------------------------------------------
# Orquestación
# --------------------------------------------------------------------------

def file_base(row) -> str:
    return f"{cvgen.slugify(row['empresa'], 40)}-{cvgen.slugify(row['titulo'], 60)}"


def output_paths(row, out_dir: Path) -> dict:
    b = file_base(row)
    return {"carta_txt": out_dir / f"cover-{b}.txt", "carta_docx": out_dir / f"cover-{b}.docx",
            "resumen_txt": out_dir / f"summary-{b}.txt"}


def _with_description(conn, row):
    """Devuelve (row_dict, None) con descripción usable (propia o de un duplicado), o (None, motivo)."""
    desc = (row["descripcion"] or "").strip()
    if len(desc) >= MIN_DESC_CHARS:
        return dict(row), None
    donor = next((r for r in cvgen.duplicates_of(conn, row)
                  if len((r["descripcion"] or "").strip()) >= MIN_DESC_CHARS), None)
    if donor:
        d = dict(row)
        d["descripcion"] = donor["descripcion"]
        return d, None
    return None, (f"sin descripción ({len(desc)} caracteres < {MIN_DESC_CHARS}); no se genera a ciegas. "
                  "Conseguir la descripción (cvgen/Firecrawl) y volver a correr.")


def generate_for(conn, row, profile: dict, *, tipo: str = "ambos", palabras: int = LETTER_MAX,
                 force: bool = False, dry_run: bool = False, out_dir: Path | None = None) -> dict:
    vid = row["id"]
    base = {"vacante_id": vid, "empresa": row["empresa"], "titulo": row["titulo"]}
    if (row["estado"] or "").lower() in cvgen.SENT_STATES and not force:
        return dict(base, estado="omitida", motivo=f"estado '{row['estado']}' (ya enviada): usa --force si de verdad hace falta")
    vrow, why = _with_description(conn, row)
    if vrow is None:
        return dict(base, estado="pendiente_descripcion", motivo=why)

    if dry_run:
        out_dir = Path(tempfile.mkdtemp(prefix="covergen-dry-"))
    out_dir = Path(out_dir or OUT_DIR)
    paths = output_paths(vrow, out_dir)
    analysis = analyze_vacancy(vrow, profile)
    lang = analysis["idioma"]
    want = {"carta": ["carta"], "resumen": ["resumen"], "ambos": ["carta", "resumen"]}[tipo]
    result = dict(base, estado="generado", idioma=lang, familia=analysis["familia"], archivos={}, palabras={},
                  keywords_cubiertas=matched_skill_names(profile, analysis, lang, limit=50),
                  requisitos_omitidos=[{"terminos": m["terminos"], "compensa": m["compensacion"][lang]}
                                       for m in analysis["missing"]],
                  requisitos_de_anios_no_afirmados=analysis["years_reqs"], errores=[], textos={})
    if dry_run:
        result["dry_run_dir"] = str(out_dir)

    for k in want:
        targets = [paths["carta_txt"], paths["carta_docx"]] if k == "carta" else [paths["resumen_txt"]]
        existing = [p for p in targets if p.exists()]
        if existing and not force:
            result["errores"].append(f"{k}: ya existe {existing[0].name}; usa --force para sobrescribir")
            result.setdefault("omitidos", []).append(k)
            continue
        if k == "carta":
            built = build_letter(profile, analysis, max_words=palabras)
            bounds = (built.get("min_words"), built.get("max_words"))
        else:
            built = build_summary(profile, analysis)
            bounds = (SUMMARY_MIN, SUMMARY_MAX)
        if "text" not in built:
            result["errores"].append(f"{k}: {built.get('motivo')}")
            continue
        targets[0].parent.mkdir(parents=True, exist_ok=True)
        targets[0].write_text(built["text"] + "\n", encoding="utf-8")
        if k == "carta":
            render_docx(built["paragraphs"], targets[1])
        vals = [validate_file(p, profile, kind=k, min_words=bounds[0], max_words=bounds[1], lang=lang,
                              vacancy=vrow, delete_on_fail=True) for p in targets]
        if not all(v["ok"] for v in vals):
            for p in targets:  # si una de las dos versiones falla, se borran ambas
                p.unlink(missing_ok=True)
            failed = sorted({c for v in vals for c, ok in v["checks"].items() if not ok})
            result["errores"].append(f"{k}: validación falló {failed} (archivo borrado)")
            result.setdefault("validacion", {})[k] = vals
            continue
        result["archivos"][k] = [str(p) for p in targets]
        result["palabras"][k] = vals[0]["palabras"]
        result["textos"][k] = built["text"]
        if k == "carta":
            result["evidencia_usada"] = built["evidence_ids"]
    if result["errores"] and not result["archivos"]:
        result["estado"] = "omitida" if result.get("omitidos") and len(result["omitidos"]) == len(want) else "error"
    elif result["errores"]:
        result["estado"] = "parcial"
    return result


def has_outputs(row, tipo: str, out_dir: Path = OUT_DIR) -> bool:
    p = output_paths(row, out_dir)
    want = {"carta": ["carta_txt"], "resumen": ["resumen_txt"], "ambos": ["carta_txt", "resumen_txt"]}[tipo]
    return all(p[k].exists() for k in want)


def run_top(conn, profile, n: int, min_stars: int, tipo: str, palabras: int, force: bool, dry_run: bool) -> list[dict]:
    results = []
    for row in cvgen.ranked_candidates(conn, min_stars):
        if sum(1 for r in results if r["estado"] in ("generado", "parcial")) >= n:
            break
        if has_outputs(row, tipo) and not force:
            continue
        results.append(generate_for(conn, row, profile, tipo=tipo, palabras=palabras, force=force, dry_run=dry_run))
    return results


def find_vacancy_for_file(conn, path: Path):
    """Busca la vacante cuyo nombre de archivo coincide (cover-/summary-<empresa>-<titulo>)."""
    stem = re.sub(r"^(cover|summary)-", "", Path(path).stem)
    best = None
    for r in conn.execute("SELECT * FROM vacantes").fetchall():
        if file_base(r) == stem:
            if len((r["descripcion"] or "").strip()) >= MIN_DESC_CHARS:
                return r
            best = best or r
    return best


def print_summary(results: list[dict], show_text: bool = False) -> None:
    for r in results:
        head = f"[{r['estado'].upper()}] {r['vacante_id']} {r.get('empresa')} — {r.get('titulo')}"
        if r["estado"] in ("generado", "parcial"):
            print(f"{head} ({r['idioma']}, familia {r['familia']})")
            for k, files in r["archivos"].items():
                print(f"     {k}: {r['palabras'][k]} palabras -> {', '.join(files)}")
            print(f"     keywords cubiertas: {', '.join(r['keywords_cubiertas']) or '-'}")
            for o in r["requisitos_omitidos"]:
                print(f"     omitido (no se afirma): {', '.join(o['terminos'])} -> compensa: {o['compensa']}")
            for y in r["requisitos_de_anios_no_afirmados"]:
                print(f"     años (no se afirman): {y}")
            for e in r["errores"]:
                print(f"     error: {e}")
            if show_text:
                for k, t in r["textos"].items():
                    print(f"\n----- {k} -----\n{t}\n")
        else:
            print(f"{head}: {r.get('motivo') or '; '.join(r.get('errores', []))}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Carta de presentación + resumen a medida por vacante (sin mentir).")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--vacante-id", type=int, action="append", help="id de vacantes.db (repetible)")
    mode.add_argument("--top", type=int, help="N mejores pendientes (estrellas/score) sin carta aún")
    mode.add_argument("--validate", type=Path, help="valida un cover-*.txt/.docx o summary-*.txt existente")
    ap.add_argument("--tipo", choices=["carta", "resumen", "ambos"], default="ambos")
    ap.add_argument("--palabras", type=int, default=LETTER_MAX, help=f"máximo de palabras de la carta (default {LETTER_MAX})")
    ap.add_argument("--min-estrellas", type=int, default=4, help="estrellas mínimas para --top (default 4)")
    ap.add_argument("--force", action="store_true", help="sobrescribe archivos existentes")
    ap.add_argument("--dry-run", action="store_true", help="genera en un directorio temporal e imprime el texto")
    ap.add_argument("--db", default=str(BASE_DIR / db.DEFAULT_DB))
    ap.add_argument("--json", action="store_true", help="imprime los resultados como JSON")
    args = ap.parse_args(argv)
    if args.palabras < 50:
        ap.error("--palabras debe ser >= 50")

    profile = cvgen.load_profile()
    conn = connect_ro(args.db)

    if args.validate:
        path = args.validate.expanduser()
        vac = find_vacancy_for_file(conn, path)
        lang = cvgen.detect_language(vac["descripcion"] or vac["titulo"]) if vac is not None else None
        kind = "resumen" if path.name.startswith("summary-") else "carta"
        mn = None if kind == "resumen" else min(LETTER_MIN, int(args.palabras * 0.625))
        mx = None if kind == "resumen" else args.palabras
        res = validate_file(path, profile, kind=kind, min_words=mn, max_words=mx, lang=lang, vacancy=vac)
        res["vacante_id"] = vac["id"] if vac is not None else None
        if vac is None:
            res["aviso"] = "no se encontró la vacante: números solo contra profile.json e idioma sin verificar"
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["ok"] else 1

    if args.vacante_id:
        results = []
        for vid in args.vacante_id:
            row = get_vacancy(conn, vid)
            if row is None:
                results.append({"vacante_id": vid, "estado": "error", "motivo": "no existe en vacantes.db"})
                continue
            results.append(generate_for(conn, row, profile, tipo=args.tipo, palabras=args.palabras,
                                        force=args.force, dry_run=args.dry_run))
    else:
        results = run_top(conn, profile, args.top, args.min_estrellas, args.tipo, args.palabras, args.force,
                          args.dry_run)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
    else:
        print_summary(results, show_text=args.dry_run)
    return 0 if all(r["estado"] not in ("error",) for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
