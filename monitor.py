#!/usr/bin/env python3
"""Monitor del pipeline completo (cierra el ciclo): ``logs/monitor-YYYY-MM-DD.json`` + resumen.

Vigila de punta a punta scraper → ranker → CVs → apply → embudo/CRM y dice dónde
optimizar el embudo. La métrica que manda es entrevistas, no volumen.

Reglas:
  * SOLO LECTURA sobre ``vacantes.db`` (URI ``mode=ro``) y ``apply-queue.json``.
    El único archivo que escribe es ``logs/monitor-YYYY-MM-DD.json`` (la última
    corrida del día sobrescribe).
  * Ningún número sin su fuente: cada check trae ``fuente`` y ``evidence``.
  * Si una fuente falta se anota como ``sin_datos`` y la corrida sigue. El estado
    del CRM se lee de ``logs/crm-state-YYYY-MM-DD.json`` (lo exporta el agente
    principal); si no existe, se marca ``atencion`` (sin export del CRM) y el
    embudo se arma con ``vacantes.db`` + cola. Nunca se inventa el estado del CRM.
  * stdlib únicamente. No llama a Firecrawl ni a ningún servicio externo.

Uso:
    python3 monitor.py                       # hoy: JSON + resumen
    python3 monitor.py --date 2026-11-16     # reprocesa un día (día de referencia)
    python3 monitor.py --resumen             # solo el resumen humano (review diario)
    python3 monitor.py --db otra.db --queue otra-cola.json --logs-dir /tmp/logs

Exit code: 0 siempre que el módulo corra (aunque falten fuentes); 2 solo si no
puede leer ni la DB ni la cola.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "vacantes.db"
QUEUE_PATH = BASE_DIR / "apply-queue.json"
LOG_DIR = BASE_DIR / "logs"
APPLY_CONFIG = BASE_DIR / "apply-config.json"
FILTER_CONFIG = BASE_DIR / "filter-config.json"
PROFILES_FILE = BASE_DIR / "search-profiles.json"
KEYWORDS_FILE = BASE_DIR / "keywords.json"
TZ = ZoneInfo("America/Mexico_City")

OK, ATENCION, CRITICO, SIN_DATOS = "ok", "atencion", "critico", "sin_datos"
_STATUS_RANK = {SIN_DATOS: 0, OK: 0, ATENCION: 1, CRITICO: 2}
_SEVERITY = {ATENCION: "warning", CRITICO: "critical"}

# Umbrales del monitor. 15/25 (y avisos 12/20) acordados con Alex (DESIGN.md ›
# Optimización); el resto son umbrales operativos del monitor, documentados en README.
UMBRALES = {
    "sin_respuesta_revisar": 15, "sin_respuesta_aviso": 12,
    "sin_entrevista_estrategia": 25, "sin_entrevista_aviso": 20,
    "en_curso_horas_critico": 24,
    "pregunta_alex_dias_atencion": 1, "pregunta_alex_dias_critico": 3,
    "estrellas_top": 4,
    "nueva_top_dias_sin_tocar": 7,
    "muestra_minima": 5,
    "keyword_muerta_min_aplicadas": 10,
    "arista_ventaja_factor": 2.0,
    "cv_cobertura_top_min": 0.5,
    "aplicacion_madura_dias": 7,
    "firecrawl_creditos_atencion": 300, "firecrawl_creditos_critico": 100,
}

APPLIED_STATES = ("aplicada", "respuesta", "entrevista", "rechazada")
QUEUE_STATES = ("pendiente", "en_curso", "aplicada", "bloqueada", "fallida",
                "pendiente-respuesta-Alex", "ya-aplicada")
_APP_RANK = {"aplicada": 1, "respuesta": 2, "rechazada": 2, "entrevista": 3}


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------

def _norm(text) -> str:
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", text.lower()).strip()


def _group_key(empresa, titulo) -> tuple:
    return (_norm(empresa), _norm(titulo))


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ if len(str(value)) <= 10 else timezone.utc)
    return dt


def _local_day(value) -> str | None:
    dt = _parse_dt(value)
    return dt.astimezone(TZ).date().isoformat() if dt else None


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def _pct(r: float | None) -> str:
    return "s/d" if r is None else f"{r:.0%}"


def _json_list(value) -> list:
    try:
        out = json.loads(value) if isinstance(value, str) else (value or [])
        return out if isinstance(out, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def _worst(statuses) -> str:
    statuses = list(statuses)
    if not statuses:
        return SIN_DATOS
    worst = max(statuses, key=lambda s: _STATUS_RANK.get(s, 0))
    if _STATUS_RANK.get(worst, 0) == 0:
        return OK if OK in statuses else SIN_DATOS
    return worst


class Ctx:
    """Contexto de una corrida: rutas, día de referencia, fuentes leídas y checks."""

    def __init__(self, db_path, queue_path, log_dir, day: date, now: datetime,
                 apply_config=APPLY_CONFIG, filter_config=FILTER_CONFIG,
                 profiles_file=PROFILES_FILE, keywords_file=KEYWORDS_FILE):
        self.db_path, self.queue_path, self.log_dir = Path(db_path), Path(queue_path), Path(log_dir)
        self.apply_config, self.filter_config = Path(apply_config), Path(filter_config)
        self.profiles_file, self.keywords_file = Path(profiles_file), Path(keywords_file)
        self.day, self.now = day, now
        self.d = day.isoformat()
        self.sources: dict[str, dict] = {}
        self.alerts: list[dict] = []

    def note_source(self, name: str, path, estado: str, detalle: str = "") -> None:
        self.sources[name] = {"path": str(path), "estado": estado, "detalle": detalle}

    def load_json(self, name: str, path: Path):
        if not path.exists():
            self.note_source(name, path, SIN_DATOS, "no existe")
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as ex:
            self.note_source(name, path, SIN_DATOS, f"ilegible: {ex}")
            return None
        self.note_source(name, path, OK)
        return data


def check(checks: list, ctx: Ctx, name: str, status: str, message: str, fuente: str,
          evidence=None, cause: str = "", action: str = "") -> dict:
    """Registra un check y, si está en atención/crítico, su alerta."""
    entry = {"check": name, "status": status, "message": message, "fuente": fuente}
    if evidence is not None:
        entry["evidence"] = evidence
    checks.append(entry)
    if status in _SEVERITY:
        ctx.alerts.append({"check": name, "severity": _SEVERITY[status], "message": message,
                           "probable_cause": cause, "suggested_action": action,
                           "fuente": fuente, "evidence": evidence})
    return entry


# --------------------------------------------------------------------------
# Lectura (solo lectura)
# --------------------------------------------------------------------------

def open_db_ro(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("SELECT 1 FROM vacantes LIMIT 1").fetchall()
    return conn


def read_db(ctx: Ctx) -> dict | None:
    try:
        conn = open_db_ro(ctx.db_path)
    except Exception as ex:  # noqa: BLE001 - degradación elegante
        ctx.note_source("vacantes.db", ctx.db_path, SIN_DATOS, f"no se pudo abrir en solo lectura: {ex}")
        return None
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, titulo, empresa, url, fuente, aristas, keywords_matched, estado, estrellas, "
            "pasa_filtro, fecha_scrapeo, "
            "(descripcion IS NOT NULL AND length(trim(descripcion)) > 0) AS con_desc FROM vacantes")]
        try:
            cvs = [dict(r) for r in conn.execute("SELECT vacante_id, estado, cv_file FROM cv_generaciones")]
        except sqlite3.OperationalError:
            cvs = None
    finally:
        conn.close()
    for r in rows:
        r["dia"] = _local_day(r["fecha_scrapeo"])
        r["aristas_l"] = _json_list(r["aristas"])
        r["keywords_l"] = _json_list(r["keywords_matched"])
        r["gkey"] = _group_key(r["empresa"], r["titulo"])
    ctx.note_source("vacantes.db", ctx.db_path, OK, f"{len(rows)} filas (mode=ro)"
                    + ("" if cvs is not None else "; sin tabla cv_generaciones"))
    return {"rows": rows, "by_id": {r["id"]: r for r in rows}, "cvs": cvs}


def read_queue(ctx: Ctx) -> dict | None:
    data = ctx.load_json("apply-queue.json", ctx.queue_path)
    if data is None:
        return None
    if not isinstance(data.get("entries"), dict):
        ctx.note_source("apply-queue.json", ctx.queue_path, SIN_DATOS, "sin 'entries'")
        return None
    ctx.sources["apply-queue.json"]["detalle"] = f"{len(data['entries'])} entradas"
    return data


def queue_group_ids(entry: dict) -> set:
    return {entry.get("vacante_id")} | set(entry.get("duplicados") or [])


def last_entered(entry: dict, estado: str) -> datetime | None:
    """Fecha de la última entrada del historial con ese estado."""
    ts = [_parse_dt(h.get("t")) for h in entry.get("historial") or [] if h.get("estado") == estado]
    ts = [t for t in ts if t]
    return max(ts) if ts else None


# --------------------------------------------------------------------------
# Etapa: scraper
# --------------------------------------------------------------------------

_FALLO_RE = re.compile(r"^(\S+ \S+) WARNING FALLO (.+)$")
_CREDITS_RE = re.compile(r"Créditos Firecrawl: antes (\d+) \| después (\d+) \| delta del contador (-?\d+)"
                         r"(?: \| suma de creditsUsed reportados por las llamadas: (\d+))?")


def classify_cause(text: str) -> str:
    t = text.lower()
    for needle, label in (("403", "403 bloqueado"), ("curl: (56)", "curl 56"), ("429", "429 rate limit"),
                          ("timeout", "timeout"), ("timed out", "timeout")):
        if needle in t:
            return label
    return text.strip()[:80]


def parse_failures(log_path: Path) -> dict | None:
    """Búsquedas fallidas del log diario: por sitio y causa (sin contar reintentos intermedios)."""
    if not log_path.exists():
        return None
    by_site: dict[str, Counter] = defaultdict(Counter)
    corridas = breaker = 0
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=== Corrida diaria" in line:
            corridas += 1
        if "Omitidas por circuit breaker" in line and "WARNING" in line:
            m = re.search(r"\((\d+)\)", line)
            breaker += int(m.group(1)) if m else 0
        m = _FALLO_RE.match(line)
        if not m or "| reintento" in m.group(2):
            continue
        parts = m.group(2).split(" | ")
        site = parts[0].split(" (")[0].strip()
        by_site[site][classify_cause(parts[-1])] += 1
    return {"corridas_en_log": corridas, "omitidas_circuit_breaker": breaker,
            "por_sitio": {s: {"total": sum(c.values()), "causas": dict(c.most_common())}
                          for s, c in sorted(by_site.items())}}


def parse_credits(log_dir: Path, d: str) -> dict | None:
    """Créditos Firecrawl desde los logs del día (nunca consulta la API)."""
    files = [log_dir / f"{d}.log", *sorted(log_dir.glob(f"firecrawl-*{d}*.log"))]
    runs = []
    for f in files:
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            m = _CREDITS_RE.search(line)
            if m:
                runs.append({"t": line[:23], "archivo": f.name, "antes": int(m.group(1)),
                             "despues": int(m.group(2)),
                             "reportados": int(m.group(4)) if m.group(4) else None})
    if not runs:
        return None
    # La misma corrida puede estar en el log diario y en firecrawl-*.log: se cuenta una vez.
    runs = list({(r["t"], r["antes"], r["despues"], r["reportados"]): r for r in runs}.values())
    runs.sort(key=lambda r: r["t"])
    used = sum(r["reportados"] or 0 for r in runs)
    last = runs[-1]
    return {"corridas": len(runs), "usados_reportados_hoy": used,
            "restantes_segun_contador": last["despues"],
            "restantes_conservador": max(0, runs[0]["antes"] - used),
            "nota": "el contador de la cuenta puede ir atrasado; 'conservador' = contador al inicio del día − "
                    "creditsUsed reportados", "archivos": sorted({r["archivo"] for r in runs})}


def plan_sources(ctx: Ctx, rows: list[dict]) -> list[str]:
    """Fuentes del plan: JobSpy (filter-config ∩ search-profiles) + Firecrawl vistas en la base."""
    jobspy = []
    try:
        fcfg = json.loads(ctx.filter_config.read_text(encoding="utf-8"))
        jobspy = list(fcfg["health"].get("expected_sources", []))
        planned = {p.get("site_name") for p in json.loads(ctx.profiles_file.read_text(encoding="utf-8"))}
        jobspy = [s for s in jobspy if s in planned]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        pass
    firecrawl = sorted({r["fuente"] for r in rows if (r["fuente"] or "").startswith("firecrawl:")})
    return jobspy + firecrawl


def stage_scraper(ctx: Ctx, db: dict | None) -> dict:
    checks: list = []
    metrics: dict = {}
    log_path = ctx.log_dir / f"{ctx.d}.log"
    if db is None:
        check(checks, ctx, "scraper_datos", SIN_DATOS, "Sin vacantes.db: no se miden filas nuevas.", "vacantes.db")
    else:
        rows = db["rows"]
        yday = (ctx.day - timedelta(days=1)).isoformat()
        today_c = Counter(r["fuente"] or "desconocida" for r in rows if r["dia"] == ctx.d)
        yday_c = Counter(r["fuente"] or "desconocida" for r in rows if r["dia"] == yday)
        plan = plan_sources(ctx, rows)
        metrics["filas_nuevas_hoy"] = sum(today_c.values())
        metrics["filas_nuevas_ayer"] = sum(yday_c.values())
        metrics["por_fuente_hoy"] = {s: today_c.get(s, 0) for s in sorted(set(plan) | set(today_c))}
        metrics["fuentes_del_plan"] = plan
        src = "vacantes.db (fecha_scrapeo, día local MX)"
        if not today_c:
            check(checks, ctx, "filas_nuevas_hoy", ATENCION, f"0 filas nuevas el {ctx.d}.", src,
                  {"filas_nuevas_hoy": 0}, "No hubo corrida hoy, todas las fuentes fallaron o todo era duplicado.",
                  f"Revisar logs/{ctx.d}.log (RESUMEN y FALLO).")
        else:
            check(checks, ctx, "filas_nuevas_hoy", OK, f"{sum(today_c.values())} filas nuevas hoy.", src,
                  dict(today_c))
        for s in plan:
            if today_c.get(s, 0):
                continue
            if not yday_c:
                check(checks, ctx, f"fuente_cero:{s}", SIN_DATOS,
                      f"{s}: 0 nuevas hoy; ayer no hubo scrapeo para comparar (se necesitan 2 días).", src,
                      {"hoy": 0, "ayer": None})
            elif yday_c.get(s, 0) == 0 and today_c:
                check(checks, ctx, f"fuente_cero:{s}", ATENCION, f"{s}: 0 vacantes nuevas dos días seguidos.",
                      src, {"hoy": 0, "ayer": 0},
                      "Bloqueo persistente (403 / curl 56 / 429) o perfiles de esa fuente inválidos.",
                      f"Buscar '{s}' en logs/{ctx.d}.log; cubrir con Firecrawl o proxies (README › Proxies).")
            else:
                check(checks, ctx, f"fuente_cero:{s}", OK, f"{s}: 0 hoy, {yday_c.get(s, 0)} ayer (1 día).",
                      src, {"hoy": 0, "ayer": yday_c.get(s, 0)})
    fails = parse_failures(log_path)
    if fails is None:
        ctx.note_source("log diario", log_path, SIN_DATOS, "no existe")
        check(checks, ctx, "busquedas_fallidas", SIN_DATOS, f"No existe logs/{ctx.d}.log.", str(log_path))
    else:
        ctx.note_source("log diario", log_path, OK)
        metrics["busquedas_fallidas"] = fails
        total = sum(v["total"] for v in fails["por_sitio"].values())
        resumen = ", ".join(f"{s} {v['total']} ({', '.join(f'{c} {n}' for c, n in v['causas'].items())})"
                            for s, v in fails["por_sitio"].items())
        check(checks, ctx, "busquedas_fallidas", ATENCION if total else OK,
              f"{total} búsquedas fallidas en {fails['corridas_en_log']} corrida(s): {resumen or 'ninguna'}.",
              f"logs/{ctx.d}.log (líneas FALLO, sin reintentos)", fails["por_sitio"],
              "Fuentes bloqueadas por el sitio (sin proxy no se fuerzan).",
              "Si persiste, proxies (README › Proxies) o cubrir con Firecrawl; no reintentar en caliente.")
    cred = parse_credits(ctx.log_dir, ctx.d)
    if cred is None:
        check(checks, ctx, "creditos_firecrawl", SIN_DATOS, "Sin líneas 'Créditos Firecrawl' en los logs del día.",
              f"logs/{ctx.d}.log, logs/firecrawl-*{ctx.d}*.log")
    else:
        metrics["firecrawl_creditos"] = cred
        rest = min(cred["restantes_segun_contador"], cred["restantes_conservador"])
        st = (CRITICO if rest < UMBRALES["firecrawl_creditos_critico"] else
              ATENCION if rest < UMBRALES["firecrawl_creditos_atencion"] else OK)
        check(checks, ctx, "creditos_firecrawl", st,
              f"Firecrawl: {cred['usados_reportados_hoy']} créditos usados hoy (reportados), "
              f"restantes ≈ {rest} (contador {cred['restantes_segun_contador']}).",
              ", ".join(f"logs/{a}" for a in cred["archivos"]), cred,
              "Gasto de créditos cerca de la reserva.", "Bajar limit/max_credits de Firecrawl en run_daily.py.")
    return {"status": _worst(c["status"] for c in checks), "metrics": metrics, "checks": checks}


# --------------------------------------------------------------------------
# Etapa: ranker
# --------------------------------------------------------------------------

def filter_health_path(ctx: Ctx) -> Path:
    """filter_health.py nombra su archivo con la fecha UTC; el monitor usa el día local (MX).

    Después de las 18:00 CST la corrida escribe filter-health-<mañana UTC>.json: se toma el
    más reciente (generated_at) entre el del día local y el del día UTC del momento de referencia.
    """
    utc_d = ctx.now.astimezone(timezone.utc).date().isoformat()
    best, best_t = ctx.log_dir / f"filter-health-{ctx.d}.json", ""
    for d in dict.fromkeys((ctx.d, utc_d)):
        p = ctx.log_dir / f"filter-health-{d}.json"
        try:
            t = json.loads(p.read_text(encoding="utf-8")).get("generated_at") or ""
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
        if d != ctx.d and _local_day(t) != ctx.d:
            continue  # el archivo UTC de "mañana" solo cuenta si se generó durante el día local
        if t >= best_t:
            best, best_t = p, t
    return best


def stage_ranker(ctx: Ctx, db: dict | None) -> dict:
    checks: list = []
    metrics: dict = {}
    if db is not None:
        rows = db["rows"]
        evaluated = [r for r in rows if r["pasa_filtro"] is not None]
        passed = [r for r in evaluated if r["pasa_filtro"] == 1]
        stars = Counter(r["estrellas"] for r in passed)
        metrics.update(evaluadas=len(evaluated), pasan=len(passed), sin_evaluar=len(rows) - len(evaluated),
                       tasa_pase=_rate(len(passed), len(evaluated)),
                       estrellas={str(s): stars.get(s, 0) for s in (5, 4, 3, 2, 1)}, fuente="vacantes.db")
        if metrics["sin_evaluar"]:
            check(checks, ctx, "sin_evaluar", ATENCION, f"{metrics['sin_evaluar']} filas sin rankear (pasa_filtro NULL).",
                  "vacantes.db", {"sin_evaluar": metrics["sin_evaluar"]},
                  "filter_rank no corrió tras el último scrapeo o falló.", "python3 filter_rank.py (con backup previo).")
        else:
            check(checks, ctx, "sin_evaluar", OK, "Todas las filas están rankeadas.", "vacantes.db", {"sin_evaluar": 0})
    health_path = filter_health_path(ctx)
    fh_src = f"logs/{health_path.name}"
    health = ctx.load_json("filter-health del día", health_path)
    if health is None:
        check(checks, ctx, "filter_health", SIN_DATOS, f"No hay {fh_src}.", str(health_path))
    else:
        metrics["filter_health_totals"] = health.get("totals")
        metrics["filter_health_archivo"] = fh_src
        fh_alerts = health.get("alerts") or []
        check(checks, ctx, "filter_health", OK,
              f"filter-health del día leído: {len(fh_alerts)} alerta(s) reutilizada(s).",
              fh_src, {"alertas": len(fh_alerts), "generated_at": health.get("generated_at")})
        for a in fh_alerts:  # se reutilizan tal cual (no se recalculan)
            st = CRITICO if a.get("severity") == "critical" else ATENCION
            check(checks, ctx, f"filter_health:{a.get('check')}", st, a.get("message", ""),
                  fh_src, a.get("metric"), a.get("probable_cause", ""), a.get("suggested_action", ""))
    return {"status": _worst(c["status"] for c in checks), "metrics": metrics, "checks": checks}


# --------------------------------------------------------------------------
# Etapa: CVs
# --------------------------------------------------------------------------

def crm_sync_ids(ctx: Ctx) -> set | None:
    """vacante_id con CV ya puesto en un sync del CRM (logs/cv-crm-sync-*.json hasta el día)."""
    files = [f for f in sorted(ctx.log_dir.glob("cv-crm-sync-*.json")) if f.stem[-10:] <= ctx.d]
    if not files:
        return None
    ids = set()
    for f in files:
        try:
            for e in json.loads(f.read_text(encoding="utf-8")).get("entries", []):
                if (e.get("updateVacancy") or {}).get("cvFile"):
                    ids.add(e.get("vacante_id"))
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
    return ids


def stage_cvs(ctx: Ctx, db: dict | None, crm: dict | None) -> dict:
    checks: list = []
    metrics: dict = {}
    cov = ctx.load_json("cv-coverage del día", ctx.log_dir / f"cv-coverage-{ctx.d}.json")
    if cov is not None:
        entries = cov.get("entries") or []
        metrics["cv_coverage_del_dia"] = {
            "cvs_reportados": len(entries),
            "con_datos_por_confirmar": sum(1 for e in entries if e.get("confirmar")),
            "fuente": f"logs/cv-coverage-{ctx.d}.json"}
    if db is None or db["cvs"] is None:
        check(checks, ctx, "cobertura_cv", SIN_DATOS, "Sin vacantes.db / cv_generaciones.", "vacantes.db")
        return {"status": SIN_DATOS, "metrics": metrics, "checks": checks}
    top = UMBRALES["estrellas_top"]
    by_id = db["by_id"]
    cv_state = {c["vacante_id"]: c for c in db["cvs"]}
    with_cv = {vid for vid, c in cv_state.items() if c["estado"] in ("generado", "duplicado") and c["cv_file"]}
    passed = [r for r in db["rows"] if r["pasa_filtro"] == 1]
    top_rows = [r for r in passed if (r["estrellas"] or 0) >= top]
    top_open = [r for r in top_rows if r["estado"] == "nueva"]
    pend_desc = [vid for vid, c in cv_state.items() if c["estado"] == "pendiente_descripcion"]
    pend_desc_li = [v for v in pend_desc if (by_id.get(v, {}).get("fuente") or "") == "linkedin"]
    top_sin_desc = [r for r in top_rows if not r["con_desc"]]
    metrics.update(
        pasan=len(passed), pasan_con_cv=sum(1 for r in passed if r["id"] in with_cv),
        pct_pasan_con_cv=_rate(sum(1 for r in passed if r["id"] in with_cv), len(passed)),
        top_estrellas_min=top, top=len(top_rows), top_con_cv=sum(1 for r in top_rows if r["id"] in with_cv),
        pct_top_con_cv=_rate(sum(1 for r in top_rows if r["id"] in with_cv), len(top_rows)),
        top_nueva_sin_cv=sorted((r["id"] for r in top_open if r["id"] not in with_cv),
                                key=lambda i: (-(by_id[i]["estrellas"] or 0), i)),
        pasan_sin_descripcion=sum(1 for r in passed if not r["con_desc"]),
        top_sin_descripcion=len(top_sin_desc),
        top_sin_descripcion_linkedin=sum(1 for r in top_sin_desc if r["fuente"] == "linkedin"),
        pendientes_descripcion=len(pend_desc), pendientes_descripcion_linkedin=len(pend_desc_li),
        cv_estados=dict(Counter(c["estado"] for c in db["cvs"])),
        fuente="vacantes.db (vacantes + cv_generaciones)")
    n_top_sin = len(metrics["top_nueva_sin_cv"])
    pct_top = metrics["pct_top_con_cv"]
    st = OK if pct_top is not None and pct_top >= UMBRALES["cv_cobertura_top_min"] else ATENCION
    check(checks, ctx, "cobertura_cv_top", st,
          f"{metrics['top_con_cv']}/{metrics['top']} vacantes {top}★+ con CV ({_pct(pct_top)}); "
          f"{n_top_sin} {top}★+ en 'nueva' sin CV. Total que pasan con CV: "
          f"{metrics['pasan_con_cv']}/{metrics['pasan']} ({_pct(metrics['pct_pasan_con_cv'])}).",
          metrics["fuente"], {k: metrics[k] for k in ("top", "top_con_cv", "pct_top_con_cv", "pasan",
                                                      "pasan_con_cv", "pct_pasan_con_cv")},
          "cvgen.py solo corrió sobre el top inicial; sin CV autoapply no aplica.",
          f"cvgen.py --top N (min 4★) o --vacante-id N; empezar por {metrics['top_nueva_sin_cv'][:5]}.")
    if pend_desc:
        check(checks, ctx, "cv_pendiente_descripcion", ATENCION,
              f"{len(pend_desc)} CVs pendientes por falta de descripción ({len(pend_desc_li)} de LinkedIn, "
              "donde Firecrawl no ayuda).", metrics["fuente"],
              {"pendientes": len(pend_desc), "linkedin": len(pend_desc_li)},
              "La fuente no trajo descripción.",
              "Las que no son de LinkedIn: scrapear detalle con el helper de Firecrawl; LinkedIn: abrir con el navegador.")
    else:
        check(checks, ctx, "cv_pendiente_descripcion", OK, "0 CVs pendientes por falta de descripción.",
              metrics["fuente"], {"pendientes": 0})
    # Vinculación al CRM: export del CRM si existe; si no, los syncs generados (proxy).
    if crm is not None:
        linked = {a["vacante_id"] for a in crm["entries"] if a.get("cvFile") and a.get("vacante_id")}
        link_src = crm["path"]
    else:
        linked = crm_sync_ids(ctx)
        link_src = "logs/cv-crm-sync-*.json (proxy: sync generado, sin confirmación del CRM)"
    if linked is None:
        check(checks, ctx, "cv_sin_vincular_crm", SIN_DATOS, "Sin syncs de CV al CRM para comparar.",
              "logs/cv-crm-sync-*.json")
    else:
        generated = {vid for vid, c in cv_state.items() if c["estado"] == "generado" and c["cv_file"]}
        unlinked = sorted(generated - linked)
        metrics["cv_sin_vincular_crm"] = unlinked
        check(checks, ctx, "cv_sin_vincular_crm", ATENCION if unlinked else OK,
              f"{len(unlinked)} de {len(generated)} CVs generados sin vincular al CRM.", link_src,
              {"generados": len(generated), "sin_vincular": unlinked[:20]},
              "cvgen generó CVs después del último sync o el sync no se aplicó.",
              "Que el agente principal aplique logs/cv-crm-sync-*.json en crm-empleo; si un CV no aparece en ningún "
              "sync, revisar su fila en cv_generaciones.")
    return {"status": _worst(c["status"] for c in checks), "metrics": metrics, "checks": checks}


# --------------------------------------------------------------------------
# Etapa: apply
# --------------------------------------------------------------------------

def stage_apply(ctx: Ctx, queue: dict | None) -> dict:
    checks: list = []
    if queue is None:
        check(checks, ctx, "cola", SIN_DATOS, "Sin apply-queue.json.", str(ctx.queue_path))
        return {"status": SIN_DATOS, "metrics": {}, "checks": checks}
    src = "apply-queue.json"
    entries = list(queue["entries"].values())
    counts = Counter(e.get("estado") for e in entries)
    cfg = ctx.load_json("apply-config.json", ctx.apply_config) or {}
    limit = (cfg.get("limites") or {}).get("diario")
    used = sum(1 for e in entries if e.get("estado") == "en_curso"
               or (e.get("estado") == "aplicada" and e.get("applied_date") == ctx.d))
    blocked = Counter((e.get("motivo") or "sin motivo") for e in entries if e.get("estado") == "bloqueada")
    ages = []
    for e in entries:
        since = last_entered(e, e.get("estado"))
        ages.append({"id": e.get("id"), "estado": e.get("estado"), "empresa": e.get("empresa"),
                     "desde": since.isoformat() if since else None,
                     "horas": round((ctx.now - since).total_seconds() / 3600, 1) if since else None})
    metrics = {"por_estado": {s: counts.get(s, 0) for s in QUEUE_STATES},
               "cupo_diario": limit, "cupo_usado_hoy": used,
               "cupo_restante_hoy": (max(0, limit - used) if isinstance(limit, int) else None),
               "bloqueadas_por_motivo": dict(blocked), "antiguedad": ages,
               "fuente": "apply-queue.json (historial) + apply-config.json › limites"}
    otros = {s: n for s, n in counts.items() if s not in QUEUE_STATES}
    if otros:
        metrics["estados_desconocidos"] = otros
    check(checks, ctx, "cupo_diario", OK if limit is not None else SIN_DATOS,
          f"Cupo diario {used}/{limit if limit is not None else 's/d'} usado ({ctx.d}).",
          "apply-queue.json + apply-config.json", {"usado": used, "limite": limit})
    # en_curso sin reporte (prioridad).
    en_curso = [a for a in ages if a["estado"] == "en_curso"]
    old = [a for a in en_curso if (a["horas"] or 0) > UMBRALES["en_curso_horas_critico"]]
    if old:
        check(checks, ctx, "en_curso_viejo", CRITICO,
              f"{len(old)} tarea(s) en_curso con más de {UMBRALES['en_curso_horas_critico']} h sin reporte "
              f"(la más vieja {max(a['horas'] for a in old):.0f} h): {', '.join(a['id'] for a in old)}.",
              src, old, "El operador no reportó el resultado (se entregaron y no se cerraron).",
              "Verificar en LinkedIn/Indeed › Mis empleos si se enviaron y cerrarlas con autoapply.py --report "
              "(no se re-entregan solas).")
    elif en_curso:
        check(checks, ctx, "en_curso_viejo", ATENCION,
              f"{len(en_curso)} tarea(s) en_curso sin reporte (la más vieja {max(a['horas'] or 0 for a in en_curso):.0f} h"
              f" < {UMBRALES['en_curso_horas_critico']} h): {', '.join(a['id'] for a in en_curso)}.",
              src, en_curso, "Tareas entregadas al operador aún sin reporte.",
              "Verificar en Mis empleos y reportar con autoapply.py --report.")
    else:
        check(checks, ctx, "en_curso_viejo", OK, "0 tareas en_curso sin reporte.", src, {"en_curso": 0})
    # Preguntas para Alex envejeciendo.
    preg = [dict(a, dias=round((a["horas"] or 0) / 24, 1)) for a in ages if a["estado"] == "pendiente-respuesta-Alex"]
    if preg:
        oldest = max(p["dias"] for p in preg)
        st = (CRITICO if oldest >= UMBRALES["pregunta_alex_dias_critico"] else
              ATENCION if oldest >= UMBRALES["pregunta_alex_dias_atencion"] else OK)
        check(checks, ctx, "pregunta_alex_envejece", st,
              f"{len(preg)} vacante(s) esperando respuesta de Alex; la más vieja {oldest:.1f} días.",
              src, preg, "Pregunta solo-Alex sin contestar (salario, disponibilidad, correo, etc.).",
              "Pedir la respuesta a Alex en el review diario; si es recurrente, agregarla a "
              "respuestas_conocidas con fuente y confirmación.")
    else:
        check(checks, ctx, "pregunta_alex_envejece", OK, "0 vacantes esperando respuesta de Alex.", src, {"n": 0})
    if blocked:
        check(checks, ctx, "bloqueadas", ATENCION,
              f"{sum(blocked.values())} bloqueada(s): {', '.join(f'{m} {n}' for m, n in blocked.items())}.",
              src, dict(blocked), "Login/CAPTCHA/ATS externo o decisión de Alex.",
              "Revisar la sesión guardada; re-encolar con --reintentar solo si el motivo se resolvió.")
    return {"status": _worst(c["status"] for c in checks), "metrics": metrics, "checks": checks}


# --------------------------------------------------------------------------
# Embudo / CRM
# --------------------------------------------------------------------------

_RESP_KEYS = ("responseDate", "respondedAt", "fechaRespuesta", "response_date")
_INTV_KEYS = ("interviewDate", "fechaEntrevista", "interview_date")
_REJ_KEYS = ("rejectedDate", "fechaRechazo", "rejected_date")


def _first(d: dict, keys) -> str | None:
    return next((d[k] for k in keys if d.get(k)), None)


def read_crm(ctx: Ctx, db: dict | None) -> dict | None:
    """Export del CRM del día (logs/crm-state-YYYY-MM-DD.json). Formato en README › Monitoreo."""
    path = ctx.log_dir / f"crm-state-{ctx.d}.json"
    data = ctx.load_json("crm-state del día", path)
    if data is None:
        return None
    raw = data if isinstance(data, list) else (data.get("vacancies") or data.get("entries") or [])
    by_url = {r["url"]: r["id"] for r in (db["rows"] if db else []) if r["url"]}
    entries = []
    for v in raw:
        if not isinstance(v, dict):
            continue
        url = v.get("externalKey") or v.get("url") or ""
        vid = v.get("vacante_id") or by_url.get(url)
        entries.append({"vacante_id": vid, "url": url, "status": _norm(v.get("status")),
                        "empresa": v.get("company") or v.get("empresa") or "",
                        "titulo": v.get("title") or v.get("titulo") or "",
                        "appliedDate": v.get("appliedDate"), "responseDate": _first(v, _RESP_KEYS),
                        "interviewDate": _first(v, _INTV_KEYS), "rejectedDate": _first(v, _REJ_KEYS),
                        "cvFile": v.get("cvFile")})
    ctx.sources["crm-state del día"]["detalle"] = f"{len(entries)} vacantes"
    return {"path": f"logs/crm-state-{ctx.d}.json", "entries": entries}


def _new_app(status, rows, applied=None, resp=None, intv=None, rej=None) -> dict:
    aristas = sorted({a for r in rows for a in r["aristas_l"]})
    keywords = sorted({k for r in rows for k in r["keywords_l"]})
    return {"status": status, "ids": sorted(r["id"] for r in rows), "aristas": aristas, "keywords": keywords,
            "estrellas": max((r["estrellas"] or 0) for r in rows) if rows else None,
            "appliedDate": applied, "responseDate": resp, "interviewDate": intv, "rejectedDate": rej,
            "has_response": status in ("respuesta", "entrevista", "rechazada") or bool(resp or intv or rej),
            "has_interview": status == "entrevista" or bool(intv)}


def apps_from_crm(crm: dict, db: dict | None) -> list[dict]:
    by_id = db["by_id"] if db else {}
    groups: dict[tuple, list] = defaultdict(list)
    for e in crm["entries"]:
        if e["status"] not in APPLIED_STATES:
            continue
        if e["status"] == "rechazada" and not e["appliedDate"]:
            continue  # rechazada sin fecha de aplicación: no se cuenta como aplicación enviada
        row = by_id.get(e["vacante_id"])
        key = row["gkey"] if row else _group_key(e["empresa"], e["titulo"])
        groups[key].append((e, row))
    apps = []
    for items in groups.values():
        best = max(items, key=lambda it: _APP_RANK.get(it[0]["status"], 0))[0]
        rows = [r for _, r in items if r]
        pick = lambda k: min((it[0][k] for it in items if it[0][k]), default=None)  # noqa: E731
        apps.append(_new_app(best["status"], rows, pick("appliedDate"), pick("responseDate"),
                             pick("interviewDate"), pick("rejectedDate")))
    return apps


def apps_from_db(db: dict | None, queue: dict | None) -> list[dict]:
    """Sin export del CRM: aplicaciones = grupos de la cola en 'aplicada' + filas de la DB en estado aplicado."""
    by_id = db["by_id"] if db else {}
    apps, covered = [], set()
    for e in (queue or {}).get("entries", {}).values():
        if e.get("estado") != "aplicada":
            continue
        ids = queue_group_ids(e)
        rows = [by_id[i] for i in ids if i in by_id]
        covered |= ids
        db_states = [r["estado"] for r in rows if r["estado"] in APPLIED_STATES] or ["aplicada"]
        apps.append(_new_app(max(db_states, key=lambda s: _APP_RANK[s]), rows, e.get("applied_date")))
    loose: dict[tuple, list] = defaultdict(list)
    for r in (db["rows"] if db else []):
        if r["estado"] in APPLIED_STATES and r["id"] not in covered:
            loose[r["gkey"]].append(r)
    for rows in loose.values():
        apps.append(_new_app(max((r["estado"] for r in rows), key=lambda s: _APP_RANK[s]), rows))
    return apps


def _days_since(ctx: Ctx, values) -> int | None:
    days = [_parse_dt(v) for v in values if v]
    days = [d for d in days if d]
    return (ctx.day - max(days).astimezone(TZ).date()).days if days else None


def build_funnel(ctx: Ctx, db: dict | None, queue: dict | None, crm: dict | None) -> tuple[dict, dict]:
    checks: list = []
    if crm is not None:
        apps, fuente, confianza = apps_from_crm(crm, db), crm["path"], "alta"
        check(checks, ctx, "crm_export", OK, f"Estado del CRM leído de {crm['path']}.", crm["path"],
              {"vacantes": len(crm["entries"])})
    else:
        apps, fuente, confianza = apps_from_db(db, queue), "vacantes.db + apply-queue.json", "baja"
        check(checks, ctx, "crm_export", ATENCION,
              f"Sin export del CRM (logs/crm-state-{ctx.d}.json): el embudo se arma con vacantes.db + cola; "
              "respuestas/entrevistas registradas solo en el CRM no se ven.", fuente, {"crm_state": None},
              "El agente principal no exportó el estado del CRM hoy.",
              f"Exportar crm-empleo (listVacancies) a logs/crm-state-{ctx.d}.json (formato en README › Monitoreo).")
    top = UMBRALES["estrellas_top"]
    n_apps = len(apps)
    n_resp = sum(a["has_response"] for a in apps)
    n_intv = sum(a["has_interview"] for a in apps)
    status_c = Counter(a["status"] for a in apps)
    # 'nueva': CRM si hay export; si no, filas que pasan y siguen en 'nueva'.
    if crm is not None:
        nuevas = sum(1 for e in crm["entries"] if e["status"] == "nueva")
    else:
        nuevas = sum(1 for r in (db["rows"] if db else []) if r["estado"] == "nueva" and r["pasa_filtro"] == 1)
    counts = {"nueva": nuevas, "aplicada": status_c.get("aplicada", 0), "respuesta": status_c.get("respuesta", 0),
              "entrevista": status_c.get("entrevista", 0), "rechazada": status_c.get("rechazada", 0)}
    # Conversión nueva→aplicada sobre vacantes 4★+ (grupos empresa+título, como autoapply).
    top_groups = {r["gkey"] for r in (db["rows"] if db else []) if r["pasa_filtro"] == 1 and (r["estrellas"] or 0) >= top}
    # Una aplicación puede cubrir varios grupos (LinkedIn + Indeed con títulos distintos): cuenta una vez.
    by_id = db["by_id"] if db else {}
    app_top = [{by_id[i]["gkey"] for i in a["ids"] if i in by_id and by_id[i]["gkey"] in top_groups} for a in apps]
    top_applied = [g for g in app_top if g]
    top_den = len(top_groups) - sum(len(g) - 1 for g in top_applied)
    mature = [a for a in apps if a["appliedDate"] and _parse_dt(a["appliedDate"])
              and (ctx.day - _parse_dt(a["appliedDate"]).astimezone(TZ).date()).days >= UMBRALES["aplicacion_madura_dias"]]
    conversions = {
        "nueva_a_aplicada_4plus": {"num": len(top_applied), "den": top_den,
                                   "tasa": _rate(len(top_applied), top_den),
                                   "nota": f"vacantes distintas (empresa+título; duplicados de una misma "
                                           f"aplicación cuentan una vez) con {top}★+ que pasan el filtro"},
        "aplicada_a_respuesta": {"num": n_resp, "den": n_apps, "tasa": _rate(n_resp, n_apps),
                                 "maduras": len(mature), "respuestas_maduras": sum(a["has_response"] for a in mature),
                                 "tasa_maduras": _rate(sum(a["has_response"] for a in mature), len(mature)),
                                 "nota": f"maduras = aplicadas hace ≥ {UMBRALES['aplicacion_madura_dias']} días"},
        "respuesta_a_entrevista": {"num": n_intv, "den": n_resp, "tasa": _rate(n_intv, n_resp)},
    }
    sin_resp = n_apps - n_resp
    sin_intv = n_apps - n_intv
    thresholds = {
        "sin_respuesta": {"valor": sin_resp, "aviso": UMBRALES["sin_respuesta_aviso"],
                          "umbral": UMBRALES["sin_respuesta_revisar"],
                          "progreso": f"{sin_resp}/{UMBRALES['sin_respuesta_revisar']}",
                          "accion": "Revisar CV y filtros (¿roles demasiado lejanos? ¿título no matchea?)",
                          "regla": "aplicaciones enviadas sin respuesta; un rechazo cuenta como respuesta"},
        "sin_entrevista": {"valor": sin_intv, "aviso": UMBRALES["sin_entrevista_aviso"],
                           "umbral": UMBRALES["sin_entrevista_estrategia"],
                           "progreso": f"{sin_intv}/{UMBRALES['sin_entrevista_estrategia']}",
                           "accion": "Cambio de estrategia: reordenar aristas, ajustar pesos del ranking, retirar "
                                     "keywords muertas, sumar nuevas",
                           "regla": "aplicaciones enviadas sin entrevista; un rechazo cuenta como sin entrevista"},
    }
    # Contexto de madurez (no cambia el estado del umbral, acordado con Alex): cuántas de esas
    # aplicaciones llevan ≥ N días; 0 respuestas sobre aplicaciones de 1–3 días no es señal todavía.
    n_mature = len(mature)
    thresholds["sin_respuesta"]["maduras"] = n_mature - sum(a["has_response"] for a in mature)
    thresholds["sin_entrevista"]["maduras"] = n_mature - sum(a["has_interview"] for a in mature)
    for name, t in thresholds.items():
        st = CRITICO if t["valor"] >= t["umbral"] else ATENCION if t["valor"] >= t["aviso"] else OK
        t["estado"] = st
        msg = (f"Umbral {name.replace('_', ' ')}: {t['progreso']}"
               + (" — CUMPLIDO" if st == CRITICO else " — aviso" if st == ATENCION else "")
               + f" [{t['maduras']} de {t['valor']} con ≥ {UMBRALES['aplicacion_madura_dias']} días de enviadas]"
               + ("" if confianza == "alta" else " (sin export del CRM: puede incluir aplicaciones que sí tuvieron respuesta)"))
        check(checks, ctx, f"umbral_{name}", st, msg, fuente, {"valor": t["valor"], "aviso": t["aviso"],
                                                             "umbral": t["umbral"]},
              "Las aplicaciones no están convirtiendo.", t["accion"] + " (DESIGN.md › Optimización).")
    # Días desde la última respuesta / entrevista (solo con fechas explícitas).
    last_resp = _days_since(ctx, [a["responseDate"] or a["interviewDate"] or a["rejectedDate"] for a in apps])
    last_intv = _days_since(ctx, [a["interviewDate"] for a in apps])
    # 4★+ en 'nueva' > 7 días sin tocar (ni en la cola).
    queued = set()
    for e in (queue or {}).get("entries", {}).values():
        queued |= queue_group_ids(e)
    stale = []
    for r in (db["rows"] if db else []):
        if r["pasa_filtro"] == 1 and (r["estrellas"] or 0) >= top and r["estado"] == "nueva" and r["id"] not in queued:
            dt = _parse_dt(r["fecha_scrapeo"])
            if dt and (ctx.day - dt.astimezone(TZ).date()).days > UMBRALES["nueva_top_dias_sin_tocar"]:
                stale.append(r["id"])
    if db is None:
        check(checks, ctx, "nueva_top_sin_tocar", SIN_DATOS, "Sin vacantes.db.", "vacantes.db")
    else:
        check(checks, ctx, "nueva_top_sin_tocar", ATENCION if stale else OK,
              f"{len(stale)} vacantes {top}★+ en 'nueva' con más de {UMBRALES['nueva_top_dias_sin_tocar']} días "
              "sin tocar (fuera de la cola).", "vacantes.db (fecha_scrapeo) + apply-queue.json",
              {"n": len(stale), "ids": stale[:20]}, "El top se acumula sin CV ni aplicación.",
              "Generar CV y encolar las mejores (cvgen.py → autoapply.py) o revisar si siguen abiertas.")
    funnel = {"fuente": fuente, "confianza": confianza, "aplicaciones": n_apps, "respuestas": n_resp,
              "entrevistas": n_intv, "conteos": counts,
              "conteos_nota": "aplicada/respuesta/entrevista/rechazada = aplicaciones únicas (empresa+título); "
                              "nueva = vacantes que pasan el filtro" + ("" if crm else " (vacantes.db)"),
              "conversiones": conversions, "umbrales": thresholds,
              "dias_desde_ultima_respuesta": last_resp, "dias_desde_ultima_entrevista": last_intv,
              "dias_nota": None if (last_resp is not None or crm) else
              "sin fechas de respuesta/entrevista (requieren export del CRM)",
              "top_nueva_sin_tocar": len(stale), "_apps": apps}
    stage = {"status": _worst(c["status"] for c in checks), "checks": checks}
    return funnel, stage


# --------------------------------------------------------------------------
# Optimizador del embudo
# --------------------------------------------------------------------------

def arista_names(ctx: Ctx) -> dict:
    try:
        cfg = json.loads(ctx.keywords_file.read_text(encoding="utf-8"))
        return {a["id"]: a.get("name", str(a["id"])) for a in cfg.get("aristas", [])}
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return {}


def build_optimizer(ctx: Ctx, funnel: dict, stages: dict) -> dict:
    apps = funnel["_apps"]
    names = arista_names(ctx)
    mmin = UMBRALES["muestra_minima"]
    table = []
    for aid in sorted(set(names) | {a for app in apps for a in app["aristas"]}):
        mine = [a for a in apps if aid in a["aristas"]]
        n, r, i = len(mine), sum(a["has_response"] for a in mine), sum(a["has_interview"] for a in mine)
        table.append({"arista": aid, "nombre": names.get(aid, ""), "aplicadas": n, "respuestas": r,
                      "entrevistas": i, "tasa_respuesta": _rate(r, n), "tasa_entrevista": _rate(i, n),
                      "muestra": "ok" if n >= mmin else "muestra insuficiente"})
    kw = defaultdict(lambda: [0, 0])
    for a in apps:
        for k in a["keywords"]:
            kw[k][0] += 1
            kw[k][1] += a["has_response"]
    dead = [{"keyword": k, "aplicadas": n, "respuestas": r} for k, (n, r) in sorted(kw.items())
            if n >= UMBRALES["keyword_muerta_min_aplicadas"] and r == 0]
    conv = funnel["conversiones"]
    trans = [("nueva→aplicada (4★+)", conv["nueva_a_aplicada_4plus"]["num"], conv["nueva_a_aplicada_4plus"]["den"]),
             ("aplicada→respuesta (maduras)", conv["aplicada_a_respuesta"]["respuestas_maduras"],
              conv["aplicada_a_respuesta"]["maduras"]),
             ("respuesta→entrevista", conv["respuesta_a_entrevista"]["num"], conv["respuesta_a_entrevista"]["den"])]
    valid = [(t, n, d) for t, n, d in trans if d >= mmin]
    leak = None
    if valid:
        t, n, d = min(valid, key=lambda x: x[1] / x[2])
        leak = {"transicion": t, "num": n, "den": d, "tasa": _rate(n, d)}
    skipped = [f"{t} ({n}/{d})" for t, n, d in trans if d < mmin]
    return {"fuente": funnel["fuente"], "confianza": funnel["confianza"],
            "por_arista": table, "por_arista_nota": "una aplicación cuenta en cada arista que matchea "
                                                    "(aristas de la vacante); < 5 aplicadas = muestra insuficiente",
            "keywords_muertas": dead,
            "keywords_muertas_nota": f"keywords con ≥ {UMBRALES['keyword_muerta_min_aplicadas']} aplicaciones y "
                                     f"0 respuestas; hoy la que más tiene: "
                                     + (f"{max(kw.items(), key=lambda x: x[1][0])[0]} ({max(v[0] for v in kw.values())})"
                                        if kw else "ninguna"),
            "mayor_fuga": leak,
            "mayor_fuga_nota": ("transiciones sin muestra suficiente (< 5): " + ", ".join(skipped)) if skipped else None,
            "top_movimientos": recommend(ctx, funnel, stages, table, dead, leak)}


def recommend(ctx: Ctx, funnel: dict, stages: dict, table: list, dead: list, leak: dict | None) -> list[dict]:
    """Top 3 movimientos, cada uno con los números que lo sostienen. Orden: riesgo primero, luego embudo."""
    moves = []
    add = lambda prio, mov, porque, fuente: moves.append(  # noqa: E731
        {"prioridad": prio, "movimiento": mov, "por_que": porque, "fuente": fuente})
    ap = stages.get("apply", {}).get("metrics", {})
    cv = stages.get("cvs", {}).get("metrics", {})
    ages = ap.get("antiguedad", [])
    en_curso = [a for a in ages if a["estado"] == "en_curso"]
    lim = UMBRALES["en_curso_horas_critico"]
    if en_curso:
        oldest = max(a["horas"] or 0 for a in en_curso)
        add(1 if oldest > lim else 4,
            f"Verificar en Mis empleos y reportar las {len(en_curso)} tareas en_curso ({', '.join(a['id'] for a in en_curso)})",
            f"{len(en_curso)} sin reporte, la más vieja {oldest:.0f} h (crítico > {lim} h); ocupan "
            f"{len(en_curso)} del cupo diario y no se re-entregan solas", "apply-queue.json")
    for name, t in funnel["umbrales"].items():
        if t["estado"] == CRITICO:
            add(2, t["accion"], f"{name.replace('_', ' ')} = {t['progreso']} (umbral cumplido)", funnel["fuente"])
        elif t["estado"] == ATENCION:
            add(9, f"Preparar: {t['accion']}", f"{name.replace('_', ' ')} = {t['progreso']} (aviso en {t['aviso']})",
                funnel["fuente"])
    preg = [a for a in ages if a["estado"] == "pendiente-respuesta-Alex"]
    if preg:
        dias = max((a["horas"] or 0) / 24 for a in preg)
        add(3 if dias >= UMBRALES["pregunta_alex_dias_critico"] else 7,
            f"Pedir a Alex las respuestas pendientes ({', '.join(a['id'] for a in preg)})",
            f"{len(preg)} vacante(s) detenidas esperando a Alex, la más vieja {dias:.1f} días", "apply-queue.json")
    ok = [r for r in table if r["muestra"] == "ok" and r["tasa_respuesta"] is not None]
    if len(ok) >= 2:
        best = max(ok, key=lambda r: r["tasa_respuesta"])
        worst = min(ok, key=lambda r: r["tasa_respuesta"])
        if best["tasa_respuesta"] > 0 and (worst["tasa_respuesta"] == 0 or
                                           best["tasa_respuesta"] >= UMBRALES["arista_ventaja_factor"] * worst["tasa_respuesta"]):
            add(5, f"Subir una semana la prioridad de la arista {best['arista']} ({best['nombre']}) y bajar la "
                   f"{worst['arista']} ({worst['nombre']})",
                f"arista {best['arista']}: {best['respuestas']}/{best['aplicadas']} respuestas "
                f"({_pct(best['tasa_respuesta'])}) vs arista {worst['arista']}: {worst['respuestas']}/"
                f"{worst['aplicadas']} ({_pct(worst['tasa_respuesta'])})", funnel["fuente"])
    if dead:
        add(6, f"Retirar keywords muertas: {', '.join(d['keyword'] for d in dead[:5])} "
               "(filter-config.json › keywords.disabled + filter-changelog.md)",
            "; ".join(f"{d['keyword']}: {d['aplicadas']} aplicadas, 0 respuestas" for d in dead[:5])
            + ("" if funnel["confianza"] == "alta" else " (sin export del CRM: confirmar antes)"), funnel["fuente"])
    sin_cv = cv.get("top_nueva_sin_cv") or []
    conv = funnel["conversiones"]["nueva_a_aplicada_4plus"]
    if sin_cv:
        restante = ap.get("cupo_restante_hoy")
        add(5 if leak and leak["transicion"].startswith("nueva") else 8,
            f"Generar CV y encolar las {min(len(sin_cv), 10)} mejores 4★+ sin CV "
            f"(cvgen.py --top {min(len(sin_cv), 10)} → autoapply.py --limit {min(len(sin_cv), 10)})",
            f"conversión 4★+→aplicada {conv['num']}/{conv['den']} ({_pct(conv['tasa'])}); {len(sin_cv)} 4★+ en "
            f"'nueva' sin CV (autoapply no aplica sin CV); cupo restante hoy "
            f"{restante if restante is not None else 's/d'}/{ap.get('cupo_diario', 's/d')}",
            "vacantes.db + cv_generaciones + apply-queue.json")
    if funnel.get("top_nueva_sin_tocar"):
        add(8, "Aplicar o descartar las 4★+ que llevan > 7 días en 'nueva'",
            f"{funnel['top_nueva_sin_tocar']} vacantes 4★+ sin tocar > 7 días", "vacantes.db")
    if funnel["confianza"] != "alta":
        add(10, f"Exportar el estado del CRM a logs/crm-state-{ctx.d}.json",
            f"sin export, respuestas/entrevistas = {funnel['respuestas']}/{funnel['entrevistas']} según vacantes.db "
            "(el CRM puede tener más)", "monitor")
    for c in stages.get("scraper", {}).get("checks", []):
        if c["check"].startswith("fuente_cero:") and c["status"] == ATENCION:
            add(11, f"Cubrir {c['check'].split(':', 1)[1]} con Firecrawl o proxy", c["message"], c["fuente"])
    moves.sort(key=lambda m: m["prioridad"])
    return [{**m, "rank": i + 1} for i, m in enumerate(moves[:3])]


# --------------------------------------------------------------------------
# Snapshot, resumen y escritura
# --------------------------------------------------------------------------

def _safe_stage(ctx: Ctx, name: str, fn, *args) -> dict:
    """Una etapa que falla queda en sin_datos con el error; el monitor sigue."""
    try:
        return fn(*args)
    except Exception as ex:  # noqa: BLE001 - degradación elegante
        ctx.alerts.append({"check": f"{name}:error_interno", "severity": "warning",
                           "message": f"La etapa {name} falló dentro del monitor: {type(ex).__name__}: {ex}",
                           "probable_cause": "Formato inesperado en una fuente de datos.",
                           "suggested_action": "Revisar la fuente y monitor.py; el resto del snapshot es válido.",
                           "fuente": "monitor.py", "evidence": None})
        return {"status": SIN_DATOS, "metrics": {}, "checks": [], "error": f"{type(ex).__name__}: {ex}"}


def build_snapshot(ctx: Ctx) -> dict:
    db = read_db(ctx)
    queue = read_queue(ctx)
    crm = _safe_stage(ctx, "crm", read_crm, ctx, db) if (ctx.log_dir / f"crm-state-{ctx.d}.json").exists() else None
    if crm is None:
        ctx.note_source("crm-state del día", ctx.log_dir / f"crm-state-{ctx.d}.json", SIN_DATOS,
                        "sin export del CRM")
    elif "entries" not in crm:
        crm = None
    for name, path in (("apply-crm-sync del día", ctx.log_dir / f"apply-crm-sync-{ctx.d}.json"),
                       ("cv-crm-sync del día", ctx.log_dir / f"cv-crm-sync-{ctx.d}.json")):
        ctx.note_source(name, path, OK if path.exists() else SIN_DATOS, "" if path.exists() else "no existe")
    stages = {
        "scraper": _safe_stage(ctx, "scraper", stage_scraper, ctx, db),
        "ranker": _safe_stage(ctx, "ranker", stage_ranker, ctx, db),
        "cvs": _safe_stage(ctx, "cvs", stage_cvs, ctx, db, crm),
        "apply": _safe_stage(ctx, "apply", stage_apply, ctx, queue),
    }
    stages["apply"].setdefault("metrics", {})["aplicadas_reportadas_hoy"] = applied_reported_today(ctx, queue)
    built = _safe_stage(ctx, "embudo", lambda: dict(zip(("funnel", "stage"), build_funnel(ctx, db, queue, crm))))
    funnel = built.get("funnel")
    stages["embudo"] = built.get("stage") or built
    optimizer = _safe_stage(ctx, "optimizer", build_optimizer, ctx, funnel, stages) if funnel else {}
    if funnel:
        funnel.pop("_apps", None)
    order = {"critical": 0, "warning": 1}
    alerts = sorted(ctx.alerts, key=lambda a: order.get(a["severity"], 2))
    r = stages["ranker"].get("metrics", {})
    totals = {
        "vacantes": len(db["rows"]) if db else None, "pasan": r.get("pasan"),
        "top_4plus": stages["cvs"].get("metrics", {}).get("top"),
        "cola": len(queue["entries"]) if queue else None,
        "aplicaciones": funnel["aplicaciones"] if funnel else None,
        "respuestas": funnel["respuestas"] if funnel else None,
        "entrevistas": funnel["entrevistas"] if funnel else None,
        "alertas_criticas": sum(a["severity"] == "critical" for a in alerts),
        "alertas_atencion": sum(a["severity"] == "warning" for a in alerts),
        "fuente": "vacantes.db + apply-queue.json" + (" + crm-state" if crm else ""),
    }
    return {"date": ctx.d, "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
            "reference_time": ctx.now.isoformat(timespec="seconds"),
            "nota_reproceso": None if ctx.day == datetime.now(TZ).date() else
            "--date cambia solo el día de referencia; la DB y la cola son su estado actual.",
            "db": str(ctx.db_path), "fuentes": ctx.sources, "totals": totals, "stages": stages,
            "funnel": funnel, "optimizer": optimizer, "alerts": alerts,
            "_db_ok": db is not None, "_queue_ok": queue is not None}


def applied_reported_today(ctx: Ctx, queue: dict | None) -> dict:
    """Aplicaciones reportadas hoy: cola (applied_date) + log apply-YYYY-MM-DD.log + sync del CRM."""
    out = {"cola": sum(1 for e in (queue or {}).get("entries", {}).values()
                       if e.get("estado") == "aplicada" and e.get("applied_date") == ctx.d) if queue else None}
    log = ctx.log_dir / f"apply-{ctx.d}.log"
    ctx.note_source("apply log del día", log, OK if log.exists() else SIN_DATOS)
    out["log_lineas_APLICADA"] = (sum(1 for l in log.read_text(encoding="utf-8").splitlines() if " APLICADA " in l)
                                  if log.exists() else None)
    sync = ctx.log_dir / f"apply-crm-sync-{ctx.d}.json"
    try:
        out["crm_sync_aplicada"] = sum(1 for e in json.loads(sync.read_text(encoding="utf-8")).get("entries", [])
                                       if (e.get("updateVacancy") or {}).get("status") == "aplicada")
    except (OSError, json.JSONDecodeError, AttributeError):
        out["crm_sync_aplicada"] = None
    out["nota"] = "crm_sync_aplicada cuenta filas (incluye duplicados LinkedIn/Indeed de la misma vacante)"
    return out


def format_summary(snap: dict) -> str:
    """Resumen humano (5–10 líneas): entrevistas/respuestas, riesgos (críticos primero), 3 movimientos."""
    f = snap.get("funnel") or {}
    lines = []
    if f:
        crm_note = "" if f["confianza"] == "alta" else " — sin export del CRM, fuente vacantes.db+cola"
        lines.append(f"Monitor {snap['date']}: entrevistas {f['entrevistas']} | respuestas {f['respuestas']} | "
                     f"aplicaciones {f['aplicaciones']}{crm_note}")
        u, c = f["umbrales"], f["conversiones"]
        lines.append(f"Umbrales: sin respuesta {u['sin_respuesta']['progreso']} · sin entrevista "
                     f"{u['sin_entrevista']['progreso']} | 4★+→aplicada {c['nueva_a_aplicada_4plus']['num']}/"
                     f"{c['nueva_a_aplicada_4plus']['den']} ({_pct(c['nueva_a_aplicada_4plus']['tasa'])})")
    else:
        lines.append(f"Monitor {snap['date']}: embudo sin datos (ver JSON)")
    stages = snap.get("stages") or {}
    lines.append("Etapas: " + " · ".join(f"{k} {v.get('status', SIN_DATOS)}" for k, v in stages.items()))
    alerts = snap.get("alerts") or []
    n_crit = sum(a["severity"] == "critical" for a in alerts)
    shown = alerts[:3]
    lines.append(f"Riesgos: {n_crit} crítico(s) · {len(alerts) - n_crit} atención"
                 + (f" (se muestran {len(shown)}; resto en el JSON)" if len(alerts) > len(shown) else "")
                 + ("" if alerts else " — sin riesgos activos"))
    for a in shown:
        tag = "CRÍTICO" if a["severity"] == "critical" else "atención"
        lines.append(f"[{tag}] {a['message'][:170]}")
    moves = (snap.get("optimizer") or {}).get("top_movimientos", [])
    for m in moves:
        lines.append(f"Mov {m['rank']}: {m['movimiento']} — {m['por_que']}")
    if not moves:
        lines.append("Movimientos: ninguno con datos suficientes.")
    return "\n".join(lines[:10])


def write_snapshot(snap: dict, log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"monitor-{snap['date']}.json"
    data = {k: v for k, v in snap.items() if not k.startswith("_")}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def reference_now(day: date) -> datetime:
    now = datetime.now(TZ)
    return now if day >= now.date() else datetime.combine(day, time(23, 59, 59), TZ)


def run(db_path=DB_PATH, queue_path=QUEUE_PATH, log_dir=LOG_DIR, day: date | None = None,
        now: datetime | None = None, write: bool = True, **paths) -> dict:
    """Arma el snapshot y (por defecto) escribe logs/monitor-YYYY-MM-DD.json. Para run_daily.py y tests."""
    day = day or datetime.now(TZ).date()
    ctx = Ctx(db_path, queue_path, log_dir, day, now or reference_now(day), **paths)
    snap = build_snapshot(ctx)
    if write:
        try:
            snap["_path"] = str(write_snapshot(snap, Path(log_dir)))
        except OSError as ex:
            snap["_path"] = None
            snap["alerts"].append({"check": "escritura_json", "severity": "warning",
                                   "message": f"No se pudo escribir el JSON: {ex}", "probable_cause": "permisos/disco",
                                   "suggested_action": "Revisar logs/.", "fuente": "monitor.py", "evidence": None})
    return snap


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Monitor del pipeline completo (solo lectura).")
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--queue", default=str(QUEUE_PATH))
    ap.add_argument("--logs-dir", default=str(LOG_DIR))
    ap.add_argument("--date", help="YYYY-MM-DD (default: hoy, hora de México)")
    ap.add_argument("--resumen", action="store_true", help="imprime solo el resumen humano")
    args = ap.parse_args(argv)
    try:
        day = date.fromisoformat(args.date) if args.date else None
    except ValueError:
        print(f"--date inválida: {args.date!r} (usa YYYY-MM-DD)", file=sys.stderr)
        return 2
    snap = run(args.db, args.queue, args.logs_dir, day)
    if not args.resumen:
        print(json.dumps({k: v for k, v in snap.items() if not k.startswith("_")},
                         indent=2, ensure_ascii=False, default=str))
        print()
    print(format_summary(snap))
    if not args.resumen:
        print(f"\nJSON: {snap.get('_path')}")
    if not snap["_db_ok"] and not snap["_queue_ok"]:
        print("ERROR: no se pudo leer ni vacantes.db ni apply-queue.json.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
