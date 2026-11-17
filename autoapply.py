#!/usr/bin/env python3
"""Fase 3 — auto-apply: cola + paquetes de aplicación para el flujo de navegador.

Autorizado por Alex el 2026-11-15 18:48 (aplicar en su nombre y actualizar el CRM).

Qué hace y qué NO hace:
  * Selecciona vacantes (pasa_filtro=1, estado 'nueva', mejor rankeadas) que YA tengan
    CV de Fase 2 (tabla ``cv_generaciones`` + archivo en ~/workspace/cv/). Sin CV no se
    aplica: se reporta "pendiente de Fase 2".
  * Construye un PAQUETE por vacante (url, plataforma, ruta del .docx, datos del candidato
    leídos de profile.json, respuestas conocidas con fuente, lista de preguntas solo-Alex)
    y lo guarda en ``apply-queue.json``.
  * NO abre navegadores, NO hace login, NO guarda ni lee contraseñas. La ejecución la hace
    el operador (agente principal) con su tarea de navegador y la sesión guardada:
    ``--run-queue`` le entrega las tareas; ``--report`` recibe el resultado y registra
    (cola, log diario, vacantes.estado, sync del CRM en logs/apply-crm-sync-*.json).
  * Login / CAPTCHA / verificación / sesión expirada => ``bloqueada`` (no es error fatal).
    Pregunta fuera del paquete => ``pendiente-respuesta-Alex`` (no se responde ni se envía).

Uso:
  .venv/bin/python autoapply.py --dry-run                 # top 5: cola simulada + valida CVs, no toca nada
  .venv/bin/python autoapply.py --dry-run --limit 10
  .venv/bin/python autoapply.py --limit 5                 # encola las 5 mejores (no envía nada)
  .venv/bin/python autoapply.py --vacante-id 200          # encola una sola vacante
  .venv/bin/python autoapply.py --run-queue [--limit N] [--vacante-id N]   # entrega tareas al operador
  .venv/bin/python autoapply.py --report resultado.json   # (o '-' para stdin) registra el resultado
  .venv/bin/python autoapply.py --status                  # resumen de la cola y del cupo diario
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import random
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import cvgen  # noqa: E402  (normalización, validación de .docx, esquema CRM)
import db  # noqa: E402

CONFIG_PATH = BASE_DIR / "apply-config.json"
QUEUE_PATH = BASE_DIR / "apply-queue.json"
LOG_DIR = BASE_DIR / "logs"
BACKUP_DIR = BASE_DIR / "backups"
CV_DIR = cvgen.CV_DIR
TZ = ZoneInfo("America/Mexico_City")

# Estados de la cola
PENDIENTE = "pendiente"
EN_CURSO = "en_curso"
APLICADA = "aplicada"
BLOQUEADA = "bloqueada"
FALLIDA = "fallida"
PREGUNTA = "pendiente-respuesta-Alex"
YA_APLICADA = "ya-aplicada"            # el CRM ya la tenía como aplicada o superior
QUEUE_STATES = {PENDIENTE, EN_CURSO, APLICADA, BLOQUEADA, FALLIDA, PREGUNTA, YA_APLICADA}
FINAL_OUTCOMES = {APLICADA, BLOQUEADA, FALLIDA, PREGUNTA, YA_APLICADA}
# No se re-encolan sin --reintentar (idempotencia).
STICKY = {APLICADA, EN_CURSO, PREGUNTA, YA_APLICADA, PENDIENTE}
RETRYABLE = {BLOQUEADA, FALLIDA}

MOTIVOS_BLOQUEO = {
    "sesion_expirada", "login_requerido", "captcha", "verificacion", "sitio_bloqueado",
    "limite_sitio", "requiere_cuenta_externa", "vacante_cerrada", "otro",
}

NEXT_STEP_APLICADA = "Esperar respuesta de {empresa}; revisar Indeed/LinkedIn y correo"

_EMAIL_APPLY = re.compile(
    r"[^.\n]*\b(apply|send|email|e-mail|env[ií]a|enviar|manda|postula)\b[^.\n]{0,120}?"
    r"([\w.+-]+@[\w-]+(?:\.[\w-]+)+)[^\n]*",
    re.I,
)


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------

def now_local() -> datetime:
    return datetime.now(TZ)


def today() -> str:
    return now_local().date().isoformat()


def iso_now() -> str:
    return now_local().isoformat(timespec="seconds")


def load_config(path: Path = CONFIG_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


@contextlib.contextmanager
def queue_lock(path: Path):
    """Bloqueo exclusivo de la cola (evita dos operadores escribiendo a la vez)."""
    lock = path.with_suffix(path.suffix + ".lock")
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def load_queue(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {
        "_doc": ("Cola de auto-apply (Fase 3). Una entrada por vacante (la representante; los duplicados "
                 "de la misma empresa+título van en 'duplicados'). Estados: pendiente | en_curso | aplicada | "
                 "bloqueada | fallida | pendiente-respuesta-Alex | ya-aplicada. La escribe autoapply.py; "
                 "no editar a mano mientras corre."),
        "version": 1,
        "entries": {},
    }


def save_queue(path: Path, queue: dict) -> None:
    queue["updated_at"] = iso_now()
    _atomic_write(path, queue)


def qid(vid: int) -> str:
    return f"v{vid}"


def log_line(text: str, day: str | None = None) -> Path:
    path = LOG_DIR / f"apply-{day or today()}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{iso_now()} {text}\n")
    return path


# --------------------------------------------------------------------------
# Plataforma y canal
# --------------------------------------------------------------------------

def detect_platform(url: str, fuente: str | None) -> str:
    u = (url or "").lower()
    for dom, name in (("linkedin.com", "linkedin"), ("indeed.", "indeed"), ("greenhouse.io", "greenhouse"),
                      ("lever.co", "lever"), ("workable.com", "workable"), ("wellfound.com", "wellfound")):
        if dom in u:
            return name
    f = (fuente or "").lower()
    for name in ("linkedin", "indeed", "greenhouse", "lever", "workable", "wellfound"):
        if name in f:
            return name
    return "otro"


def detect_email_apply(descripcion: str | None) -> dict | None:
    """Si la vacante pide aplicar por correo, devuelve el correo y la instrucción literal."""
    if not descripcion:
        return None
    m = _EMAIL_APPLY.search(descripcion)
    if not m:
        return None
    return {"email": m.group(2).rstrip("."), "instruccion_literal": m.group(0).strip()}


# --------------------------------------------------------------------------
# Selección
# --------------------------------------------------------------------------

SELECT_SQL = """
SELECT v.id, v.titulo, v.empresa, v.url, v.fuente, v.score, v.estrellas, v.estado,
       v.ubicacion, v.modalidad_v2, v.sueldo, v.descripcion, v.pasa_filtro,
       c.estado AS cv_estado, c.cv_file, c.cv_version, c.duplicado_de, c.idioma
FROM vacantes v LEFT JOIN cv_generaciones c ON c.vacante_id = v.id
"""


def _order_clause(orden: str) -> str:
    if orden == "score":
        return " ORDER BY v.score DESC, v.estrellas DESC, v.id"
    return " ORDER BY v.estrellas DESC, v.score DESC, v.id"


def ranked_groups(conn, orden: str, only_ids: list[int] | None = None) -> list[list]:
    """Vacantes candidatas agrupadas por (empresa normalizada, título) en orden de ranking.

    Cada grupo es la misma vacante publicada en varias plataformas (p. ej. LinkedIn + Indeed):
    se aplica UNA vez por grupo.
    """
    cvgen.ensure_table(conn)
    where = " WHERE v.pasa_filtro = 1 AND v.estado = 'nueva'"
    rows = conn.execute(SELECT_SQL + where + _order_clause(orden)).fetchall()
    groups: dict[tuple, list] = {}
    order: list[tuple] = []
    for r in rows:
        key = cvgen.dedup_key(r)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(r)
    out = [groups[k] for k in order]
    if only_ids:
        wanted = set(only_ids)
        out = [g for g in out if wanted & {r["id"] for r in g}]
    return out


def resolve_cv(group: list) -> dict | None:
    """CV de Fase 2 del grupo (representante 'generado' o duplicado que lo comparte)."""
    for r in group:
        if r["cv_estado"] in ("generado", "duplicado") and r["cv_file"]:
            path = CV_DIR / r["cv_file"]
            return {"cv_file": r["cv_file"], "cv_version": r["cv_version"], "cv_path": str(path),
                    "existe": path.is_file(), "idioma": r["idioma"]}
    return None


def pick_primary(group: list, tiers: dict) -> tuple:
    """Fila por la que se aplica: mejor tier de plataforma; empate => la que generó el CV."""
    def key(r):
        tier = tiers.get(detect_platform(r["url"], r["fuente"]), 3)
        return (tier, 0 if r["cv_estado"] == "generado" else 1, -(r["score"] or 0), r["id"])
    ordered = sorted(group, key=key)
    return ordered[0], ordered[1:]


# --------------------------------------------------------------------------
# Paquete
# --------------------------------------------------------------------------

def candidate_data(profile: dict) -> dict:
    ident = profile["identity"]
    first, _, last = ident["name"].partition(" ")
    return {
        "nombre_completo": ident["name"],
        "nombre": first,
        "apellidos": last,
        "email": ident["email"],
        "telefono": ident["phone"],
        "linkedin": "https://www." + ident["linkedin"].removeprefix("https://").removeprefix("www."),
        "github": "https://" + ident["github"].removeprefix("https://"),
        "ubicacion": ident["location"],
        "ingles": profile.get("english_sentence"),
        "fuente": "profile.json › identity (hechos verificados)",
    }


def build_package(primary, dups: list, cv: dict, profile: dict, config: dict) -> dict:
    plat = detect_platform(primary["url"], primary["fuente"])
    email_apply = detect_email_apply(primary["descripcion"])
    canal = "correo" if (email_apply and plat not in ("linkedin", "indeed")) else "formulario"
    return {
        "vacante_id": primary["id"],
        "titulo": primary["titulo"],
        "empresa": primary["empresa"],
        "url": primary["url"],
        "plataforma": plat,
        "tier": config["tiers_plataforma"].get(plat, 3),
        "canal": canal,
        "aplicacion_por_correo": email_apply,
        "estrellas": primary["estrellas"],
        "score": primary["score"],
        "ubicacion_vacante": primary["ubicacion"],
        "modalidad": primary["modalidad_v2"],
        "sueldo_publicado": primary["sueldo"],
        "cv_file": cv["cv_file"],
        "cv_version": cv["cv_version"],
        "cv_path": cv["cv_path"],
        "idioma_cv": cv.get("idioma"),
        "candidato": candidate_data(profile),
        "respuestas_conocidas": config["respuestas_conocidas"],
        "solo_alex": config["solo_alex"],
        "alternativas": [
            {"vacante_id": d["id"], "url": d["url"], "plataforma": detect_platform(d["url"], d["fuente"])}
            for d in dups
        ],
    }


# --------------------------------------------------------------------------
# Construcción de la cola (dry-run o real)
# --------------------------------------------------------------------------

def build(conn, profile: dict, config: dict, queue: dict, *, limit: int, only_ids: list[int] | None,
          orden: str, reintentar: bool, validate: bool) -> dict:
    """Recorre las `limit` mejores vacantes distintas (o las pedidas por id) y decide qué hacer.

    `--limit N` = ventana de las N mejores vacantes distintas no aplicadas. Las que no tienen CV
    se reportan como pendientes de Fase 2 y NO se sustituyen por vacantes de menor rango
    (se aplica a lo mejor del ranking, no a lo que casualmente ya tiene CV).
    """
    tiers = config["tiers_plataforma"]
    result = {"encoladas": [], "pregunta_alex": [], "pendiente_fase2": [], "omitidas": [], "cv_invalido": []}
    groups = ranked_groups(conn, orden, only_ids)
    if only_ids:
        found = {r["id"] for g in groups for r in g}
        for vid in only_ids:
            if vid not in found:
                row = conn.execute("SELECT id, estado, pasa_filtro FROM vacantes WHERE id=?", (vid,)).fetchone()
                motivo = "no existe" if row is None else f"estado={row['estado']} pasa_filtro={row['pasa_filtro']}"
                result["omitidas"].append({"vacante_id": vid, "motivo": f"no elegible ({motivo})"})
    window = 0
    for group in groups:
        if not only_ids and window >= limit:
            break
        ids = [r["id"] for r in group]
        existing = next((queue["entries"][qid(i)] for i in ids if qid(i) in queue["entries"]), None)
        if existing:
            st = existing["estado"]
            if st in STICKY or (st in RETRYABLE and not reintentar):
                result["omitidas"].append({"vacante_id": existing["vacante_id"], "titulo": existing["titulo"],
                                           "empresa": existing["empresa"],
                                           "motivo": f"ya en cola con estado '{st}'"
                                           + ("" if st in STICKY else " (usa --reintentar)")})
                continue
        window += 1
        cv = resolve_cv(group)
        primary, dups = pick_primary(group, tiers)
        base = {"vacante_id": primary["id"], "titulo": primary["titulo"], "empresa": primary["empresa"],
                "estrellas": primary["estrellas"], "score": primary["score"], "url": primary["url"],
                "plataforma": detect_platform(primary["url"], primary["fuente"]),
                "duplicados": [d["id"] for d in dups]}
        if cv is None or not cv["existe"]:
            motivo = "sin CV generado" if cv is None else f"CV registrado pero no existe: {cv['cv_path']}"
            result["pendiente_fase2"].append({**base, "motivo": motivo,
                                              "siguiente": f".venv/bin/python cvgen.py --vacante-id {primary['id']}"})
            continue
        if validate:
            v = cvgen.validate_docx(Path(cv["cv_path"]), profile)
            if not v["ok"]:
                fails = [k for k, ok in v["checks"].items() if not ok]
                result["cv_invalido"].append({**base, "cv_path": cv["cv_path"], "checks_fallidos": fails})
                continue
        pkg = build_package(primary, dups, cv, profile, config)
        entry = {
            "id": qid(primary["id"]),
            **base,
            "estado": PENDIENTE,
            "motivo": None,
            "pregunta_alex": None,
            "paquete": pkg,
            "creado": iso_now(),
            "actualizado": iso_now(),
            "intentos": (existing or {}).get("intentos", 0),
            "historial": (existing or {}).get("historial", []) + [{"t": iso_now(), "estado": PENDIENTE,
                                                                   "nota": "encolada"}],
        }
        if pkg["canal"] == "correo":
            ea = pkg["aplicacion_por_correo"]
            pregunta = (f"La vacante no tiene formulario: pide aplicar por correo a {ea['email']}. "
                        f"Instrucción literal: \"{ea['instruccion_literal']}\" ¿Autorizas enviar ese correo "
                        "en tu nombre y qué respondo en lo que solo tú puedes dar (pretensión salarial, "
                        "disponibilidad, video/texto de un sistema de agentes, proyectos a enlazar)?")
            entry.update(estado=PREGUNTA, motivo="aplicación por correo (sin formulario)", pregunta_alex=pregunta)
            entry["historial"].append({"t": iso_now(), "estado": PREGUNTA, "nota": "canal correo detectado"})
            result["pregunta_alex"].append(entry)
        else:
            result["encoladas"].append(entry)
    return result


def crm_entry(vid: int, url: str, title: str, company: str, **fields) -> dict:
    upd = {k: v for k, v in fields.items() if v is not None}
    return {"vacante_id": vid, "crm_id": None, "externalKey": url, "title": title, "company": company,
            "updateVacancy": upd}


def write_crm_sync(entries: list[dict], day: str | None = None) -> Path:
    path = LOG_DIR / f"apply-crm-sync-{day or today()}.json"
    has_cvfile = cvgen.crm_has_cvfile()
    header = {
        "modulo": "autoapply.py (Fase 3)",
        "action": ("crm-empleo › por cada entrada: listVacancies y buscar la fila cuyo url == externalKey; "
                   "si no existe, crearla con importVacancies (externalKey=url) y luego updateVacancy(id, "
                   "**updateVacancy). Si la entrada trae uploadCv, subir el archivo para que Alex lo abra."),
        "match_key": "externalKey (= url de la vacante). crm_id = null: Claude Code no tiene acceso al CRM.",
        "cvFile_field": ("existe (ts-spaces/crm-empleo/server/src/schema.ts: cv_file)" if has_cvfile else
                         "NO existe: cvFile va también en cvVersion/nextStep"),
    }
    cvgen._merge_json_log(path, header, "externalKey", entries)
    return path


def enqueue(conn, profile, config, args) -> int:
    with queue_lock(QUEUE_PATH):
        queue = load_queue(QUEUE_PATH)
        res = build(conn, profile, config, queue, limit=args.limit, only_ids=args.vacante_id,
                    orden=args.orden, reintentar=args.reintentar, validate=True)
        if args.dry_run:
            out = {"modo": "dry-run", "generado": iso_now(), "limit": args.limit, "orden": args.orden,
                   "nota": "Nada se envió ni se escribió en apply-queue.json / vacantes.db / CRM.", **res}
            path = LOG_DIR / f"apply-dryrun-{now_local().strftime('%Y%m%d-%H%M%S')}.json"
            _atomic_write(path, out)
            print_summary(res, dry_run=True)
            print(f"\nDetalle: {path}")
            return 0
        crm = []
        for e in res["encoladas"] + res["pregunta_alex"]:
            queue["entries"][e["id"]] = e
            log_line(f"ENCOLADA {e['id']} [{e['estado']}] {e['empresa']} — {e['titulo']} ({e['plataforma']}) "
                     f"cv={e['paquete']['cv_file']}")
            if e["estado"] == PREGUNTA:
                crm.append(crm_entry(e["vacante_id"], e["url"], e["titulo"], e["empresa"], status="nueva",
                                     notes=e["pregunta_alex"], nextStep=f"Pregunta para Alex: {e['pregunta_alex']}",
                                     cvVersion=e["paquete"]["cv_version"], cvFile=e["paquete"]["cv_file"]))
        save_queue(QUEUE_PATH, queue)
        if crm:
            write_crm_sync(crm)
    print_summary(res, dry_run=False)
    print(f"\nCola: {QUEUE_PATH}\nSiguiente: .venv/bin/python autoapply.py --run-queue")
    return 0


def print_summary(res: dict, dry_run: bool) -> None:
    tag = "[DRY-RUN] " if dry_run else ""
    print(f"{tag}Encoladas para aplicar: {len(res['encoladas'])}")
    for e in res["encoladas"]:
        p = e["paquete"]
        alt = f" (+alt: {', '.join(a['plataforma'] + ' ' + str(a['vacante_id']) for a in p['alternativas'])})" \
            if p["alternativas"] else ""
        print(f"  - {e['vacante_id']} {e['estrellas']}★ {e['score']:.0f} {e['empresa']} — {e['titulo']} "
              f"[{p['plataforma']}]{alt}\n      CV OK: {p['cv_path']}")
    if res["pregunta_alex"]:
        print(f"{tag}Pendientes de respuesta de Alex: {len(res['pregunta_alex'])}")
        for e in res["pregunta_alex"]:
            print(f"  - {e['vacante_id']} {e['empresa']} — {e['titulo']}: {e['motivo']}\n      CV OK: "
                  f"{e['paquete']['cv_path']}")
    if res["pendiente_fase2"]:
        print(f"{tag}Pendientes de Fase 2 (sin CV, no se aplica): {len(res['pendiente_fase2'])}")
        for e in res["pendiente_fase2"]:
            print(f"  - {e['vacante_id']} {e['estrellas']}★ {e['score']:.0f} {e['empresa']} — {e['titulo']} "
                  f"[{e['plataforma']}]: {e['motivo']} → {e['siguiente']}")
    if res["cv_invalido"]:
        print(f"{tag}CV inválido (no se aplica): {len(res['cv_invalido'])}")
        for e in res["cv_invalido"]:
            print(f"  - {e['vacante_id']} {e['empresa']}: {e['checks_fallidos']}")
    if res["omitidas"]:
        print(f"{tag}Omitidas: {len(res['omitidas'])}")
        for e in res["omitidas"]:
            print(f"  - {e['vacante_id']}: {e['motivo']}")


# --------------------------------------------------------------------------
# Ejecución: tareas para el operador (agente principal + navegador)
# --------------------------------------------------------------------------

def applied_today(queue: dict, day: str) -> int:
    n = 0
    for e in queue["entries"].values():
        if e["estado"] == EN_CURSO:
            n += 1
        elif e["estado"] == APLICADA and (e.get("applied_date") == day):
            n += 1
    return n


def browser_instructions(entry: dict) -> str:
    p = entry["paquete"]
    c = p["candidato"]
    alt = ""
    if p["alternativas"]:
        alt = (" Si la URL principal queda bloqueada o no tiene flujo de aplicación propio, puedes usar UNA "
               "alternativa de la misma vacante: " + ", ".join(f"{a['plataforma']} {a['url']}" for a in p["alternativas"])
               + ". Nunca apliques en las dos.")
    return (
        f"Aplica en nombre de Alex (autorización 2026-11-15) a '{p['titulo']}' en {p['empresa']}: {p['url']}.{alt}\n"
        "0) Antes de abrir: en crm-empleo › listVacancies busca las filas con esas url; si alguna no está en "
        "'nueva', NO apliques y reporta outcome 'ya-aplicada'. En el sitio, si ves 'Ya te postulaste'/'Applied', "
        "también 'ya-aplicada'.\n"
        "1) Usa SOLO la sesión guardada del navegador (LinkedIn/Indeed). No escribas contraseñas, no hagas login, "
        "no crees cuentas. Si pide login, CAPTCHA, código de verificación o la sesión expiró: DETENTE y reporta "
        "'bloqueada' con el motivo (sesion_expirada | login_requerido | captcha | verificacion | sitio_bloqueado | "
        "limite_sitio | requiere_cuenta_externa | vacante_cerrada | otro).\n"
        "2) LinkedIn: solo 'Solicitud sencilla'/'Easy Apply'. Indeed: 'Postularse ahora' dentro de Indeed. Si el botón "
        "lleva al sitio de la empresa (ATS externo) y pide crear cuenta: 'bloqueada' (requiere_cuenta_externa).\n"
        f"3) Sube el CV: {p['cv_path']} (reemplaza cualquier CV anterior del perfil para esta postulación).\n"
        f"4) Datos del candidato (solo estos): {c['nombre_completo']}, e-mail {c['email']}, tel {c['telefono']}, "
        f"LinkedIn {c['linkedin']}, ubicación {c['ubicacion']['es']} / {c['ubicacion']['en']}. Respuestas conocidas: "
        + "; ".join(f"{r['pregunta_tipo']} → {r['es'].rstrip('.')} (EN: {r['en'].rstrip('.')})"
                    for r in p["respuestas_conocidas"]) + ".\n"
        "5) Si un campo OBLIGATORIO pide algo que no está arriba (pretensión salarial, años con una herramienta, "
        "autorización de trabajo/visa, fecha de inicio, disponibilidad de entrevista, carta, video, RFC/CURP...): "
        "NO lo respondas, NO inventes, NO envíes. Cierra sin enviar y reporta 'pendiente-respuesta-Alex' con la "
        "pregunta EXACTA tal como aparece (y sus opciones si es de selección). Campos opcionales desconocidos: en blanco.\n"
        "6) Antes de enviar, revisa la pantalla de revisión (nombre, e-mail, teléfono, CV correcto). Envía.\n"
        "7) Copia el texto de confirmación literal del sitio (ej. 'Tu postulación se ha enviado') y el folio si "
        "aparece; si no hay folio, folio = null (no lo inventes). Si el envío falla, reporta 'fallida' con la causa.\n"
        "8) Reporta con: .venv/bin/python autoapply.py --report <archivo.json> (formato en 'reporte_plantilla')."
    )


def report_template(entry: dict) -> dict:
    return {
        "queue_id": entry["id"],
        "vacante_id": entry["vacante_id"],
        "outcome": "aplicada | bloqueada | pendiente-respuesta-Alex | fallida | ya-aplicada",
        "url_usada": entry["url"],
        "confirmation_text": "texto literal del sitio (obligatorio si aplicada)",
        "folio": None,
        "pregunta": "pregunta exacta (obligatorio si pendiente-respuesta-Alex)",
        "motivo": "obligatorio si bloqueada (uno de MOTIVOS_BLOQUEO) o fallida (causa)",
        "detalle": "opcional: lo observado",
        "cv_subido": entry["paquete"]["cv_file"],
        "observado": "ISO 8601 con zona",
    }


def run_queue(config: dict, args) -> int:
    lim = config["limites"]
    with queue_lock(QUEUE_PATH):
        queue = load_queue(QUEUE_PATH)
        day = today()
        stale = [e for e in queue["entries"].values() if e["estado"] == EN_CURSO]
        remaining = lim["diario"] - applied_today(queue, day)
        pend = [e for e in queue["entries"].values() if e["estado"] == PENDIENTE]
        if args.vacante_id:
            pend = [e for e in pend if e["vacante_id"] in set(args.vacante_id)
                    or set(e["duplicados"]) & set(args.vacante_id)]
        pend.sort(key=lambda e: (e["paquete"]["tier"], -(e["estrellas"] or 0), -(e["score"] or 0)))
        n = max(0, min(args.limit, remaining))
        batch = pend[:n]
        tasks = []
        for i, e in enumerate(batch):
            e["estado"] = EN_CURSO
            e["intentos"] = e.get("intentos", 0) + 1
            e["actualizado"] = iso_now()
            e["historial"].append({"t": iso_now(), "estado": EN_CURSO, "nota": "entregada al operador"})
            delay = 0 if i == 0 else random.randint(lim["delay_min_s"], lim["delay_max_s"])
            tasks.append({"queue_id": e["id"], "orden": i + 1, "delay_before_s": delay,
                          "paquete": e["paquete"], "instrucciones_navegador": browser_instructions(e),
                          "reporte_plantilla": report_template(e)})
            log_line(f"EN_CURSO {e['id']} {e['empresa']} — {e['titulo']} ({e['paquete']['plataforma']}) "
                     f"intento {e['intentos']}")
        save_queue(QUEUE_PATH, queue)
    out = {
        "generado": iso_now(),
        "cupo_diario": lim["diario"], "cupo_restante_antes": remaining, "entregadas": len(tasks),
        "regla_delay": f"espera delay_before_s antes de cada tarea ({lim['delay_min_s']}–{lim['delay_max_s']} s)",
        "en_curso_sin_reporte": [e["id"] for e in stale],
        "aviso_en_curso": ("Estas tareas se entregaron antes y no se reportaron: NO se re-entregan. Verifica en "
                           "LinkedIn/Indeed (Mis empleos) si se enviaron y repórtalas con --report.") if stale else None,
        "tareas": tasks,
    }
    path = LOG_DIR / f"apply-tasks-{now_local().strftime('%Y%m%d-%H%M%S')}.json"
    _atomic_write(path, out)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"\nTareas guardadas en {path}", file=sys.stderr)
    if remaining <= 0:
        print(f"Cupo diario agotado ({lim['diario']}).", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------
# Registro de resultados
# --------------------------------------------------------------------------

class ReportError(ValueError):
    pass


def validate_report(r: dict, entry: dict | None) -> None:
    if entry is None:
        raise ReportError(f"queue_id {r.get('queue_id')!r} no está en la cola")
    oc = r.get("outcome")
    if oc not in FINAL_OUTCOMES:
        raise ReportError(f"outcome inválido: {oc!r} (válidos: {sorted(FINAL_OUTCOMES)})")
    if entry["estado"] == APLICADA:
        raise ReportError(f"{entry['id']} ya está aplicada ({entry.get('applied_date')}); no se registra dos veces")
    if entry["estado"] != EN_CURSO:
        raise ReportError(f"{entry['id']} está en '{entry['estado']}', no en en_curso: entrégala con --run-queue")
    if oc == APLICADA and not (r.get("confirmation_text") or "").strip():
        raise ReportError("aplicada exige confirmation_text literal del sitio")
    if oc == PREGUNTA and not (r.get("pregunta") or "").strip():
        raise ReportError("pendiente-respuesta-Alex exige la pregunta exacta")
    if oc == BLOQUEADA and r.get("motivo") not in MOTIVOS_BLOQUEO:
        raise ReportError(f"bloqueada exige motivo en {sorted(MOTIVOS_BLOQUEO)}")
    if oc == FALLIDA and not (r.get("motivo") or "").strip():
        raise ReportError("fallida exige motivo (causa)")
    urls = {entry["url"]} | {a["url"] for a in entry["paquete"]["alternativas"]}
    if r.get("url_usada") and r["url_usada"] not in urls:
        raise ReportError(f"url_usada no pertenece a esta vacante: {r['url_usada']}")


def backup_db(db_path: Path) -> Path:
    BACKUP_DIR.mkdir(exist_ok=True)
    dest = BACKUP_DIR / f"vacantes-pre-autoapply-{now_local().strftime('%Y%m%d-%H%M%S')}.db"
    shutil.copy2(db_path, dest)
    return dest


def record(conn, db_path: Path, reports: list[dict]) -> int:
    day = today()
    crm, errors, done = [], [], []
    with queue_lock(QUEUE_PATH):
        queue = load_queue(QUEUE_PATH)
        for r in reports:
            entry = queue["entries"].get(r.get("queue_id") or qid(int(r.get("vacante_id") or 0)))
            try:
                validate_report(r, entry)
            except ReportError as ex:
                errors.append(str(ex))
                continue
            oc = r["outcome"]
            p = entry["paquete"]
            url_usada = r.get("url_usada") or entry["url"]
            plat = detect_platform(url_usada, None)
            entry.update(estado=oc, actualizado=iso_now(), resultado=r)
            entry["historial"].append({"t": iso_now(), "estado": oc,
                                       "nota": r.get("confirmation_text") or r.get("pregunta") or r.get("motivo")})
            group_ids = [entry["vacante_id"]] + entry["duplicados"]
            rows = {row["id"]: row for row in conn.execute(
                f"SELECT id, url, titulo, empresa FROM vacantes WHERE id IN ({','.join('?' * len(group_ids))})",
                group_ids)}
            if oc == APLICADA:
                folio = (r.get("folio") or "").strip() or None
                entry.update(applied_date=day, url_usada=url_usada, folio=folio)
                if not done:
                    backup_db(db_path)
                conn.execute(f"UPDATE vacantes SET estado='aplicada' WHERE id IN ({','.join('?' * len(group_ids))})",
                             group_ids)
                conn.commit()
                log_line(f"APLICADA {entry['id']} | {entry['empresa']} — {entry['titulo']} | plataforma={plat} | "
                         f"url={url_usada} | confirmación=\"{r['confirmation_text'].strip()}\" | "
                         f"folio={folio or 'sin folio'} | cv={p['cv_file']}")
                nxt = NEXT_STEP_APLICADA.format(empresa=entry["empresa"])
                for vid in group_ids:
                    row = rows.get(vid)
                    if not row:
                        continue
                    via = "" if row["url"] == url_usada else f" (misma vacante; aplicada vía {plat}: {url_usada})"
                    note = (f"Aplicada por auto-apply {day} vía {plat}. Confirmación: \"{r['confirmation_text'].strip()}\". "
                            f"Folio: {folio or 'sin folio'}.{via}")
                    ce = crm_entry(vid, row["url"], row["titulo"], row["empresa"], status="aplicada",
                                   appliedDate=day, cvVersion=p["cv_version"], cvFile=p["cv_file"],
                                   nextStep=nxt, notes=note)
                    ce["uploadCv"] = {"filename": p["cv_file"], "mimeType": cvgen.DOCX_MIME, "cv_path": p["cv_path"]}
                    crm.append(ce)
            elif oc == PREGUNTA:
                q = r["pregunta"].strip()
                entry["pregunta_alex"] = q
                log_line(f"PREGUNTA-ALEX {entry['id']} | {entry['empresa']} — {entry['titulo']} | plataforma={plat} | "
                         f"NO enviada | pregunta=\"{q}\"")
                crm.append(crm_entry(entry["vacante_id"], entry["url"], entry["titulo"], entry["empresa"],
                                     status="nueva", notes=f"Auto-apply detenido {day} ({plat}): el formulario "
                                     f"pregunta \"{q}\". No se respondió ni se envió.",
                                     nextStep=f"Pregunta para Alex: {q}", cvVersion=p["cv_version"],
                                     cvFile=p["cv_file"]))
            elif oc in (BLOQUEADA, FALLIDA):
                m = r["motivo"].strip()
                entry["motivo"] = m
                log_line(f"{oc.upper()} {entry['id']} | {entry['empresa']} — {entry['titulo']} | plataforma={plat} | "
                         f"motivo={m} | detalle={r.get('detalle') or '-'} | NO enviada")
                nxt = ("Bloqueada por " + m + ": revisar la sesión guardada del navegador y reintentar con "
                       "--reintentar") if oc == BLOQUEADA else f"Envío fallido ({m}); reintentar con --reintentar"
                crm.append(crm_entry(entry["vacante_id"], entry["url"], entry["titulo"], entry["empresa"],
                                     status="nueva", notes=f"Auto-apply {day} ({plat}) {oc}: {m}. "
                                     f"{r.get('detalle') or ''}".strip(), nextStep=nxt,
                                     cvVersion=p["cv_version"], cvFile=p["cv_file"]))
            elif oc == YA_APLICADA:
                log_line(f"YA-APLICADA {entry['id']} | {entry['empresa']} — {entry['titulo']} | no se volvió a aplicar | "
                         f"{r.get('detalle') or ''}")
            done.append(f"{entry['id']} → {oc}")
        save_queue(QUEUE_PATH, queue)
    if crm:
        path = write_crm_sync(crm, day)
        print(f"Sync CRM: {path}")
    for d in done:
        print("OK  " + d)
    for e in errors:
        print("ERROR " + e, file=sys.stderr)
    return 1 if errors else 0


def status(config: dict) -> int:
    queue = load_queue(QUEUE_PATH)
    counts: dict[str, int] = {}
    for e in queue["entries"].values():
        counts[e["estado"]] = counts.get(e["estado"], 0) + 1
    print(f"Cola {QUEUE_PATH}: {counts or 'vacía'}")
    print(f"Cupo diario: {applied_today(queue, today())}/{config['limites']['diario']} usado hoy ({today()})")
    for e in sorted(queue["entries"].values(), key=lambda e: e["actualizado"], reverse=True):
        extra = e.get("pregunta_alex") or e.get("motivo") or ""
        print(f"  {e['id']:>6} {e['estado']:<26} {e['empresa']} — {e['titulo']} [{e['plataforma']}] {extra[:90]}")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    config = load_config()
    ap = argparse.ArgumentParser(description="Fase 3: cola + paquetes de auto-apply (el envío lo hace el operador).")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--run-queue", action="store_true", help="entrega al operador las siguientes tareas pendientes")
    mode.add_argument("--report", metavar="ARCHIVO", help="registra resultado(s) JSON (objeto o lista; '-' = stdin)")
    mode.add_argument("--status", action="store_true", help="resumen de la cola")
    ap.add_argument("--dry-run", action="store_true", help="construye la cola y valida CVs sin escribir ni enviar")
    ap.add_argument("--limit", type=int, default=config["limites"]["por_corrida_default"],
                    help="N mejores vacantes distintas (encolar) / N tareas (run-queue). Default 5")
    ap.add_argument("--vacante-id", type=int, action="append", help="solo esta(s) vacante(s) (repetible)")
    ap.add_argument("--orden", choices=["estrellas", "score"], default=config.get("orden", "estrellas"),
                    help="estrellas DESC, score DESC (default, igual que cvgen) o score DESC")
    ap.add_argument("--reintentar", action="store_true", help="re-encola vacantes bloqueadas/fallidas")
    ap.add_argument("--db", default=str(BASE_DIR / db.DEFAULT_DB))
    args = ap.parse_args(argv)

    if args.dry_run and (args.run_queue or args.report):
        ap.error("--dry-run solo aplica al encolado")
    if args.status:
        return status(config)
    if args.run_queue:
        return run_queue(config, args)
    conn = db.connect(args.db)
    try:
        if args.report:
            raw = sys.stdin.read() if args.report == "-" else Path(args.report).read_text(encoding="utf-8")
            data = json.loads(raw)
            return record(conn, Path(args.db), data if isinstance(data, list) else [data])
        profile = cvgen.load_profile()
        return enqueue(conn, profile, config, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
