#!/usr/bin/env python3
"""Pruebas de monitor.py en sandbox (/tmp): DB sintética + cola/health/coverage/CRM sintéticos.

Nunca toca vacantes.db, apply-queue.json ni logs/ reales (solo lee schema.sql y keywords.json).
Uso: python3 test_monitor.py   → exit 0 si todo pasa.
"""
import contextlib
import hashlib
import io
import json
import shutil
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import monitor as mon  # noqa: E402

DAY = date(2026, 11, 16)
NOW = datetime(2026, 11, 16, 20, 0, tzinfo=mon.TZ)
ROOT = Path(tempfile.mkdtemp(prefix="monitor-test-"))
res = []


def ok(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    res.append(bool(cond))
    return cond


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


class Env:
    """Un sandbox: DB, cola, logs y configs propios."""

    def __init__(self, name):
        self.dir = ROOT / name
        self.logs = self.dir / "logs"
        self.logs.mkdir(parents=True)
        self.db = self.dir / "vacantes.db"
        self.queue = self.dir / "apply-queue.json"
        self.apply_cfg = self.dir / "apply-config.json"
        self.filter_cfg = self.dir / "filter-config.json"
        self.profiles = self.dir / "search-profiles.json"
        self.apply_cfg.write_text(json.dumps({"limites": {"diario": 10}}))
        self.filter_cfg.write_text(json.dumps({"health": {"expected_sources": ["linkedin", "indeed", "glassdoor"]}}))
        self.profiles.write_text(json.dumps([{"site_name": s} for s in ("linkedin", "indeed", "glassdoor")]))
        conn = sqlite3.connect(self.db)
        conn.executescript((BASE / "schema.sql").read_text())
        conn.commit()
        conn.close()
        self.n = 0
        self.entries = {}

    def add(self, **kw) -> int:
        self.n += 1
        i = self.n
        row = {"titulo": f"Rol {i}", "empresa": f"Emp {i}", "url": f"https://jobs.example/{i}",
               "fuente": "linkedin", "aristas": "[1]", "keywords_matched": "[]", "descripcion": "texto",
               "estado": "nueva", "estrellas": 3, "pasa_filtro": 1, "fecha_scrapeo": f"{DAY}T18:00:00+00:00"}
        row.update(kw)
        conn = sqlite3.connect(self.db)
        cur = conn.execute(f"INSERT INTO vacantes ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                           list(row.values()))
        conn.commit()
        conn.close()
        return cur.lastrowid

    def cv(self, vid, estado="generado", cv_file="cv.docx"):
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO cv_generaciones (vacante_id, estado, cv_file) VALUES (?,?,?)", (vid, estado, cv_file))
        conn.commit()
        conn.close()

    def qentry(self, vid, estado, since: datetime, dups=(), applied_date=None, motivo=None):
        self.entries[f"v{vid}"] = {"id": f"v{vid}", "vacante_id": vid, "estado": estado, "empresa": f"Emp {vid}",
                                   "titulo": f"Rol {vid}", "duplicados": list(dups), "applied_date": applied_date,
                                   "motivo": motivo, "actualizado": since.isoformat(),
                                   "historial": [{"t": (since - timedelta(minutes=5)).isoformat(), "estado": "pendiente"},
                                                 {"t": since.isoformat(), "estado": estado}]}

    def save_queue(self):
        self.queue.write_text(json.dumps({"version": 1, "entries": self.entries}))

    def run(self, write=True):
        if not self.queue.exists():
            self.save_queue()
        return mon.run(self.db, self.queue, self.logs, day=DAY, now=NOW, write=write,
                       apply_config=self.apply_cfg, filter_config=self.filter_cfg, profiles_file=self.profiles)


def chk(snap, name):
    for st in snap["stages"].values():
        for c in st.get("checks", []):
            if c["check"] == name:
                return c
    return None


def applied_env(name, n, statuses=None, crm=False):
    """n aplicaciones; statuses = lista de estados CRM (por defecto todas 'aplicada')."""
    e = Env(name)
    statuses = statuses or ["aplicada"] * n
    crm_rows = []
    for i, st in enumerate(statuses):
        vid = e.add(estado=st if not crm else "aplicada", estrellas=4)
        crm_rows.append({"vacante_id": vid, "externalKey": f"https://jobs.example/{vid}", "status": st,
                         "appliedDate": "2026-11-01", "title": f"Rol {vid}", "company": f"Emp {vid}"})
    if crm:
        (e.logs / f"crm-state-{DAY}.json").write_text(json.dumps({"vacancies": crm_rows}))
    return e


# 1–2) Umbral 15 sin respuesta y aviso a las 12 (sin CRM: vacantes.db + cola)
s = applied_env("t15", 15).run()
c = chk(s, "umbral_sin_respuesta")
ok(c["status"] == "critico" and s["funnel"]["umbrales"]["sin_respuesta"]["progreso"] == "15/15"
   and any(a["check"] == "umbral_sin_respuesta" and a["severity"] == "critical" for a in s["alerts"]),
   "15 aplicaciones sin respuesta → umbral cumplido (crítico) con alerta")
s = applied_env("t12", 12).run()
ok(chk(s, "umbral_sin_respuesta")["status"] == "atencion" and chk(s, "umbral_sin_entrevista")["status"] == "ok",
   "12 aplicaciones → aviso de 15 (atención); 25 sigue ok")

# 3) Rechazo = respuesta (para 15) y = sin entrevista (para 25), con export del CRM
s = applied_env("trech", 15, ["aplicada"] * 12 + ["rechazada"] * 3, crm=True).run()
u = s["funnel"]["umbrales"]
ok(u["sin_respuesta"]["valor"] == 12 and u["sin_entrevista"]["valor"] == 15 and s["funnel"]["confianza"] == "alta"
   and chk(s, "crm_export")["status"] == "ok",
   "rechazo cuenta como respuesta (12/15) y como sin entrevista (15/25); CRM leído")

# 4) Umbral 25 sin entrevista (con respuestas) y aviso a las 20
s = applied_env("t25", 25, ["respuesta"] * 25, crm=True).run()
ok(chk(s, "umbral_sin_entrevista")["status"] == "critico" and chk(s, "umbral_sin_respuesta")["status"] == "ok",
   "25 aplicaciones con respuesta y sin entrevista → umbral 25 cumplido")
s = applied_env("t20", 21, ["respuesta"] * 20 + ["entrevista"], crm=True).run()
ok(chk(s, "umbral_sin_entrevista")["status"] == "atencion" and s["funnel"]["entrevistas"] == 1
   and s["funnel"]["conversiones"]["respuesta_a_entrevista"]["tasa"] == round(1 / 21, 4),
   "20 sin entrevista → aviso; 1 entrevista cuenta en la conversión respuesta→entrevista")

# 5) en_curso viejo (> 24 h) = crítico; reciente = atención
e = Env("encurso")
v1, v2 = e.add(estrellas=5), e.add(estrellas=4)
e.qentry(v1, "en_curso", NOW - timedelta(hours=30))
e.qentry(v2, "en_curso", NOW - timedelta(hours=3))
s = e.run()
c = chk(s, "en_curso_viejo")
ok(c["status"] == "critico" and f"v{v1}" in c["message"] and f"v{v2}" not in c["message"]
   and s["alerts"][0]["severity"] == "critical"
   and s["optimizer"]["top_movimientos"][0]["movimiento"].startswith("Verificar en Mis empleos"),
   "en_curso > 24 h → crítico, primera alerta y movimiento #1")
e = Env("encurso2")
e.qentry(e.add(), "en_curso", NOW - timedelta(hours=2))
ok(chk(e.run(), "en_curso_viejo")["status"] == "atencion", "en_curso < 24 h → atención")

# 6) pendiente-respuesta-Alex envejeciendo
e = Env("pregunta")
e.qentry(e.add(), "pendiente-respuesta-Alex", NOW - timedelta(days=4))
c = chk(e.run(), "pregunta_alex_envejece")
ok(c["status"] == "critico" and c["evidence"][0]["dias"] == 4.0, "pregunta para Alex de 4 días → crítico con días")

# 7) Tabla por arista + recomendación con números; muestra insuficiente
e = Env("aristas")
crm_rows = []
for i in range(6):
    vid = e.add(aristas="[1]", estado="aplicada", estrellas=4)
    crm_rows.append({"vacante_id": vid, "status": "respuesta" if i < 3 else "aplicada", "appliedDate": "2026-11-01"})
for i in range(6):
    vid = e.add(aristas="[2]", estado="aplicada", estrellas=4)
    crm_rows.append({"vacante_id": vid, "status": "rechazada" if i == 0 else "aplicada", "appliedDate": "2026-11-01"})
vid = e.add(aristas="[3]", estado="aplicada")
crm_rows.append({"vacante_id": vid, "status": "aplicada", "appliedDate": "2026-11-01"})
(e.logs / f"crm-state-{DAY}.json").write_text(json.dumps(crm_rows))
s = e.run()
t = {r["arista"]: r for r in s["optimizer"]["por_arista"]}
moves = " | ".join(m["movimiento"] + " :: " + m["por_que"] for m in s["optimizer"]["top_movimientos"])
ok(t[1]["aplicadas"] == 6 and t[1]["respuestas"] == 3 and t[1]["tasa_respuesta"] == 0.5
   and t[2]["tasa_respuesta"] == round(1 / 6, 4) and t[3]["muestra"] == "muestra insuficiente"
   and t[1]["muestra"] == "ok" and "arista 1" in moves and "3/6" in moves and "1/6" in moves,
   "tabla por arista (tasas, muestra insuficiente) y movimiento 'sube arista 1' con 3/6 vs 1/6")

# 8) Keywords muertas (≥ 10 aplicaciones, 0 respuestas)
e = applied_env("kw", 0)
for i in range(10):
    e.add(estado="aplicada", keywords_matched='["n8n", "RevOps"]')
e.add(estado="respuesta", keywords_matched='["RevOps"]')
s = e.run()
dead = {d["keyword"] for d in s["optimizer"]["keywords_muertas"]}
ok(dead == {"n8n"}, "keyword con 10 aplicaciones y 0 respuestas = muerta; con 1 respuesta no")

# 9) Solo lectura: DB y cola idénticas (hash) y el único archivo escrito es monitor-YYYY-MM-DD.json
e = Env("ro")
vid = e.add(estrellas=5)
e.cv(vid)
e.qentry(vid, "aplicada", NOW - timedelta(hours=5), applied_date=str(DAY))
e.save_queue()
h_db, h_q = sha(e.db), sha(e.queue)
before = set(p.name for p in e.dir.rglob("*"))
s = e.run()
after = set(p.name for p in e.dir.rglob("*"))
ok(sha(e.db) == h_db and sha(e.queue) == h_q and after - before == {f"monitor-{DAY}.json"},
   "no escribe en la DB ni en la cola (hash igual); solo crea logs/monitor-YYYY-MM-DD.json")

# 10) JSON con las claves pedidas y resumen humano de 5–10 líneas
data = json.loads((e.logs / f"monitor-{DAY}.json").read_text())
summary = mon.format_summary(s)
lines = summary.splitlines()
ok({"date", "totals", "stages", "funnel", "optimizer", "alerts"} <= set(data)
   and set(data["stages"]) == {"scraper", "ranker", "cvs", "apply", "embudo"}
   and 5 <= len(lines) <= 10 and lines[0].startswith(f"Monitor {DAY}: entrevistas"),
   f"JSON con date/totals/stages/funnel/optimizer/alerts y resumen de {len(lines)} líneas")

# 11) filter-health del día: sus alerts se reutilizan; cv-coverage leído
e = Env("health")
e.add()
(e.logs / f"filter-health-{DAY}.json").write_text(json.dumps({"totals": {"evaluated": 1}, "alerts": [
    {"check": "pass_rate", "severity": "critical", "message": "Tasa de pase 10%", "probable_cause": "detector",
     "suggested_action": "revisar", "metric": 0.1}]}))
(e.logs / f"cv-coverage-{DAY}.json").write_text(json.dumps({"entries": [{"vacante_id": 1, "confirmar": ["x"]}]}))
s = e.run()
a = next((a for a in s["alerts"] if a["check"] == "filter_health:pass_rate"), None)
ok(a and a["severity"] == "critical" and a["probable_cause"] == "detector"
   and s["stages"]["cvs"]["metrics"]["cv_coverage_del_dia"]["con_datos_por_confirmar"] == 1,
   "alertas de filter-health reutilizadas (severidad y causa intactas); cv-coverage leído")

# 11b) filter-health con fecha UTC (corrida después de las 18:00 CST) gana si es del mismo día local
e = Env("health_utc")
e.add()
(e.logs / f"filter-health-{DAY}.json").write_text(json.dumps(
    {"generated_at": "2026-11-16T14:00:00+00:00", "alerts": [{"check": "vieja", "severity": "warning"}]}))
(e.logs / f"filter-health-{DAY + timedelta(days=1)}.json").write_text(json.dumps(
    {"generated_at": "2026-11-17T01:30:00+00:00", "alerts": [{"check": "nueva", "severity": "warning"}]}))
s = e.run()
checks_fh = {a["check"] for a in s["alerts"] if a["check"].startswith("filter_health:")}
(e.logs / f"filter-health-{DAY + timedelta(days=1)}.json").write_text(json.dumps(
    {"generated_at": "2026-11-17T20:00:00+00:00", "alerts": [{"check": "otro_dia", "severity": "warning"}]}))
s2 = e.run()
checks_fh2 = {a["check"] for a in s2["alerts"] if a["check"].startswith("filter_health:")}
ok(checks_fh == {"filter_health:nueva"} and checks_fh2 == {"filter_health:vieja"},
   "filter-health UTC de la noche se usa; el del día siguiente local no")

# 12) Fuentes faltantes → sin_datos y exit 0; sin DB ni cola → exit 2
e = Env("faltan")
e.add()
out = io.StringIO()
with contextlib.redirect_stdout(out):
    rc = mon.main(["--db", str(e.db), "--queue", str(e.dir / "no-existe.json"), "--logs-dir", str(e.logs),
                   "--date", str(DAY), "--resumen"])
snap_f = json.loads((e.logs / f"monitor-{DAY}.json").read_text())
ok(rc == 0 and snap_f["fuentes"]["apply-queue.json"]["estado"] == "sin_datos"
   and snap_f["stages"]["apply"]["status"] == "sin_datos" and snap_f["fuentes"]["log diario"]["estado"] == "sin_datos",
   "cola y logs faltantes → sin_datos, exit 0")
err = io.StringIO()
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
    rc = mon.main(["--db", str(e.dir / "nada.db"), "--queue", str(e.dir / "nada.json"),
                   "--logs-dir", str(e.dir / "logs2"), "--resumen"])
ok(rc == 2, "sin DB ni cola → exit 2")

# 13) Sin export del CRM: atención + dedup por duplicados de la cola + conversión 4★+
e = Env("nocrm")
a1 = e.add(estado="aplicada", estrellas=5, titulo="TAM", empresa="Samsara")
a2 = e.add(estado="aplicada", estrellas=5, titulo="Technical Account Mgr", empresa="Samsara", fuente="indeed")
for _ in range(3):
    e.add(estrellas=4)
e.add(estrellas=2)
e.qentry(a1, "aplicada", NOW - timedelta(hours=8), dups=[a2], applied_date=str(DAY))
s = e.run()
conv = s["funnel"]["conversiones"]["nueva_a_aplicada_4plus"]
ok(chk(s, "crm_export")["status"] == "atencion" and s["funnel"]["aplicaciones"] == 1
   and conv["num"] == 1 and conv["den"] == 4 and s["funnel"]["confianza"] == "baja",
   "sin crm-state → atención; LinkedIn+Indeed = 1 aplicación; 4★+→aplicada = 1/4")

# 14) 4★+ en 'nueva' > 7 días sin tocar; las encoladas no cuentan
e = Env("stale")
old = f"{DAY - timedelta(days=9)}T18:00:00+00:00"
s1, s2 = e.add(estrellas=5, fecha_scrapeo=old), e.add(estrellas=4, fecha_scrapeo=old)
e.add(estrellas=3, fecha_scrapeo=old)
e.add(estrellas=5)
e.qentry(s2, "pendiente", NOW - timedelta(hours=1))
c = chk(e.run(), "nueva_top_sin_tocar")
ok(c["status"] == "atencion" and c["evidence"]["ids"] == [s1], "4★+ > 7 días sin tocar = solo la no encolada")

# 15) Scraper: fuente en 0 dos días, búsquedas fallidas y créditos Firecrawl desde los logs
e = Env("scraper")
yday = f"{DAY - timedelta(days=1)}T18:00:00+00:00"
e.add(fuente="linkedin"), e.add(fuente="indeed"), e.add(fuente="linkedin", fecha_scrapeo=yday)
(e.logs / f"{DAY}.log").write_text(
    "2026-11-16 08:00:00,000 INFO === Corrida diaria: aristas=TODAS ===\n"
    "2026-11-16 08:00:01,000 WARNING FALLO glassdoor | 'n8n' | response status code 403 (blocked: use a proxy)\n"
    "2026-11-16 08:00:02,000 WARNING FALLO glassdoor | 'CRM' | Failed to perform, curl: (56) Connection closed\n"
    "2026-11-16 08:00:02,500 WARNING FALLO linkedin (429): rate | reintento 1/3\n"
    "2026-11-16 08:00:03,000 INFO Créditos Firecrawl: antes 1500 | después 1480 | delta del contador 20 | "
    "suma de creditsUsed reportados por las llamadas: 20\n")
(e.logs / f"firecrawl-real-run-{DAY}.log").write_text(  # misma corrida repetida en otro log: no duplica
    "2026-11-16 08:00:03,000 INFO Créditos Firecrawl: antes 1500 | después 1480 | delta del contador 20 | "
    "suma de creditsUsed reportados por las llamadas: 20\n")
s = e.run()
fails = s["stages"]["scraper"]["metrics"]["busquedas_fallidas"]["por_sitio"]
cred = s["stages"]["scraper"]["metrics"]["firecrawl_creditos"]
ok(chk(s, "fuente_cero:glassdoor")["status"] == "atencion" and chk(s, "fuente_cero:indeed") is None
   and fails == {"glassdoor": {"total": 2, "causas": {"403 bloqueado": 1, "curl 56": 1}}}
   and cred["usados_reportados_hoy"] == 20 and cred["restantes_segun_contador"] == 1480,
   "fuente del plan en 0 dos días → atención; fallos por sitio/causa (sin reintentos); créditos del log")

# 16) CVs: cobertura 4★+, pendientes por descripción (LinkedIn) y CVs sin vincular al CRM
e = Env("cvs")
c1, c2, c3 = e.add(estrellas=5), e.add(estrellas=4, fuente="linkedin", descripcion=None), e.add(estrellas=4, fuente="indeed")
e.cv(c1)
e.cv(c2, "pendiente_descripcion", None)
e.cv(c3)
(e.logs / f"cv-crm-sync-{DAY}.json").write_text(json.dumps({"entries": [{"vacante_id": c1, "updateVacancy": {"cvFile": "cv.docx"}}]}))
s = e.run()
m = s["stages"]["cvs"]["metrics"]
ok(m["top_con_cv"] == 2 and m["top"] == 3 and m["pendientes_descripcion_linkedin"] == 1
   and m["top_sin_descripcion_linkedin"] == 1 and m["cv_sin_vincular_crm"] == [c3],
   "CVs: 2/3 4★+ con CV, 1 pendiente por descripción de LinkedIn, 1 CV sin vincular al CRM")

# 17) Una fuente corrupta no tumba el monitor (degradación elegante)
e = Env("corrupto")
e.add()
(e.logs / f"filter-health-{DAY}.json").write_text("{no es json")
(e.logs / f"crm-state-{DAY}.json").write_text(json.dumps({"vacancies": "basura"}))
try:
    s = e.run()
    good = s["fuentes"]["filter-health del día"]["estado"] == "sin_datos" and s["funnel"] is not None
except Exception as ex:  # noqa: BLE001
    good = False
    print("   excepción:", ex)
ok(good, "JSON corrupto / CRM con formato raro → sin_datos, el monitor termina")

print(f"\n{sum(res)}/{len(res)} PASS  (sandbox {ROOT})")
if all(res):
    shutil.rmtree(ROOT, ignore_errors=True)
sys.exit(0 if all(res) else 1)
