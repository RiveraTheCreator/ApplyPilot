"""Sandbox: copia de vacantes.db + cola/logs/backups temporales. No toca archivos reales.

Robusto al estado de la DB real: antes dependía de que las vacantes 495/510/200/207 siguieran
'nueva' (falló con KeyError 'v495' cuando 495 se aplicó de verdad). Ahora el test PREPARA en la
copia sus propias filas sintéticas (empresa/título únicos, ids nuevos) y les asigna como CV un
.docx real ya generado por cvgen que valide contra profile.json (solo se lee). Determinista:
elige el donante de menor vacante_id que exista y valide.

Uso: .venv/bin/python test_autoapply.py
"""
import contextlib
import io
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import autoapply as aa  # noqa: E402
import cvgen  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="aa-"))
dbp = tmp / "vacantes.db"
shutil.copy2(BASE / "vacantes.db", dbp)
aa.QUEUE_PATH = tmp / "apply-queue.json"
aa.LOG_DIR = tmp / "logs"
aa.BACKUP_DIR = tmp / "backups"
D = ["--db", str(dbp)]


# --------------------------------------------------------------------------
# Fixture determinista en la COPIA
# --------------------------------------------------------------------------

def _conn():
    c = sqlite3.connect(dbp)
    c.row_factory = sqlite3.Row
    return c


def pick_cv_donor(conn, profile) -> sqlite3.Row:
    cvgen.ensure_table(conn)
    for r in conn.execute("SELECT vacante_id, cv_file, cv_version, idioma FROM cv_generaciones "
                          "WHERE estado = 'generado' AND cv_file IS NOT NULL ORDER BY vacante_id"):
        path = cvgen.CV_DIR / r["cv_file"]
        if path.is_file() and cvgen.validate_docx(path, profile)["ok"]:
            return r
    raise SystemExit("FAIL fixture: ningún CV generado existe y valida en cvgen.CV_DIR; no se puede probar")


def add_row(conn, empresa, titulo, url, fuente, estado="nueva"):
    cur = conn.execute(
        "INSERT INTO vacantes (titulo, empresa, url, fuente, estado, pasa_filtro, estrellas, score, "
        "modalidad_v2, descripcion, fecha_scrapeo) VALUES (?, ?, ?, ?, ?, 1, 5, 99.0, 'remoto', ?, "
        "'2026-11-20T00:00:00+00:00')",
        (titulo, empresa, url, fuente, estado, "Synthetic sandbox vacancy used only by test_autoapply."))
    return cur.lastrowid


def add_cv(conn, vid, donor, dup_of=None):
    conn.execute(
        "INSERT OR REPLACE INTO cv_generaciones (vacante_id, estado, cv_file, cv_version, version_n, idioma, "
        "duplicado_de, actualizado) VALUES (?, ?, ?, ?, 1, ?, ?, '2026-11-20T00:00:00+00:00')",
        (vid, "duplicado" if dup_of else "generado", donor["cv_file"], donor["cv_version"], donor["idioma"], dup_of))


profile = cvgen.load_profile()
with contextlib.closing(_conn()) as c:
    donor = pick_cv_donor(c, profile)
    # Grupo A (pregunta solo-Alex): LinkedIn + duplicado Indeed.
    A = add_row(c, "ZZ Sandbox Alpha", "Automation Test Engineer", "https://www.linkedin.com/jobs/view/990000001", "linkedin")
    A2 = add_row(c, "ZZ Sandbox Alpha", "Automation Test Engineer", "https://mx.indeed.com/viewjob?jk=zzsandboxa", "indeed")
    # Grupo B (bloqueo → reintento → aplicada vía alternativa Indeed).
    B = add_row(c, "ZZ Sandbox Beta", "Implementation Test Specialist", "https://www.linkedin.com/jobs/view/990000002", "linkedin")
    B2 = add_row(c, "ZZ Sandbox Beta", "Implementation Test Specialist", "https://mx.indeed.com/viewjob?jk=zzsandboxb", "indeed")
    # C: sin CV.
    C = add_row(c, "ZZ Sandbox Gamma", "No CV Test Analyst", "https://www.linkedin.com/jobs/view/990000003", "linkedin")
    # Grupo D: ya aplicada; reaparece con otra URL en 'nueva' (con CV) → no debe re-encolarse.
    D1 = add_row(c, "ZZ Sandbox Delta", "Revenue Ops Test Manager", "https://www.linkedin.com/jobs/view/990000004",
                 "linkedin", estado="aplicada")
    D2 = add_row(c, "ZZ Sandbox Delta", "Revenue Ops Test Manager", "https://www.linkedin.com/jobs/view/990000005", "linkedin")
    for primary, dup in ((A, A2), (B, B2)):
        add_cv(c, primary, donor)
        add_cv(c, dup, donor, dup_of=primary)
    add_cv(c, D2, donor)
    c.commit()
QA, QB, QD2 = aa.qid(A), aa.qid(B), aa.qid(D2)


def run(argv, stdin=None):
    out = io.StringIO()
    if stdin is not None:
        sys.stdin = io.StringIO(json.dumps(stdin))
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        rc = aa.main(argv + D)
    return rc, out.getvalue()


def q():
    return json.loads(aa.QUEUE_PATH.read_text())["entries"] if aa.QUEUE_PATH.exists() else {}


def estado(vid):
    with contextlib.closing(sqlite3.connect(dbp)) as c:
        return c.execute("select estado from vacantes where id=?", (vid,)).fetchone()[0]


def ok(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    return bool(cond)


res = []

# 1) encolar A (dup A2) y entregarla
rc, o = run(["--vacante-id", str(A)])
res.append(ok(rc == 0 and q().get(QA, {}).get("estado") == "pendiente", f"encolar {A} → pendiente"))
rc, o = run(["--run-queue", "--vacante-id", str(A)])
res.append(ok(q()[QA]["estado"] == "en_curso", "run-queue → en_curso"))
# 2) aplicada sin confirmación → rechazado
rc, o = run(["--report", "-"], {"queue_id": QA, "outcome": "aplicada", "confirmation_text": ""})
res.append(ok(rc == 1 and q()[QA]["estado"] == "en_curso", "aplicada sin confirmation_text se rechaza"))
# 3) PRUEBA DE BLOQUEO: el formulario pide algo solo-Alex
PREG = "What are your salary expectations (monthly, USD)? *"
rc, o = run(["--report", "-"], {"queue_id": QA, "outcome": "pendiente-respuesta-Alex", "pregunta": PREG,
                                "url_usada": "https://www.linkedin.com/jobs/view/990000001"})
e = q()[QA]
res.append(ok(rc == 0 and e["estado"] == "pendiente-respuesta-Alex" and e["pregunta_alex"] == PREG,
              "pregunta solo-Alex → pendiente-respuesta-Alex"))
res.append(ok(estado(A) == "nueva" and estado(A2) == "nueva", "vacantes.estado sigue 'nueva' (no se envió)"))
sync = json.loads((aa.LOG_DIR / f"apply-crm-sync-{aa.today()}.json").read_text())["entries"]
s = next(x for x in sync if x["vacante_id"] == A)["updateVacancy"]
res.append(ok(s["status"] == "nueva" and s["nextStep"] == f"Pregunta para Alex: {PREG}" and PREG in s["notes"]
              and "appliedDate" not in s, "CRM sync: status nueva, nextStep 'Pregunta para Alex: <exacta>', sin appliedDate"))
log = (aa.LOG_DIR / f"apply-{aa.today()}.log").read_text()
res.append(ok(f"PREGUNTA-ALEX {QA}" in log and "NO enviada" in log, "log registra la pregunta y que no se envió"))
# 4) idempotencia: re-encolar no la duplica ni la reactiva
rc, o = run(["--vacante-id", str(A)])
res.append(ok(q()[QA]["estado"] == "pendiente-respuesta-Alex" and "ya en cola" in o, f"re-encolar {A} se omite"))
rc, o = run(["--vacante-id", str(A2)])
res.append(ok(aa.qid(A2) not in q(), f"encolar el duplicado {A2} también se omite"))
# 4b) --reanudar: exige nota; PREGUNTA → en_curso con historial, intento y log
rc, o = run(["--reanudar", QA])
res.append(ok(rc == 2 and q()[QA]["estado"] == "pendiente-respuesta-Alex", "--reanudar sin --nota se rechaza"))
rc, o = run(["--reanudar", QA, "--nota", "Respuesta de prueba (sandbox)"])
e = q()[QA]
res.append(ok(rc == 0 and e["estado"] == "en_curso" and e["intentos"] == 2
              and "reanudada" in e["historial"][-1]["nota"] and e["historial"][-1]["t"][-6] in "+-",
              "--reanudar → en_curso, intento 2, historial con zona horaria"))
res.append(ok(f"REANUDADA {QA}" in (aa.LOG_DIR / f"apply-{aa.today()}.log").read_text(), "log registra REANUDADA"))
rc, o = run(["--reanudar", QB, "--nota", "x"])
res.append(ok(rc == 1 and "no está en la cola" in o, "--reanudar de una vacante fuera de la cola se rechaza"))
# 5) bloqueo por sesión/captcha
run(["--vacante-id", str(B)])
run(["--run-queue", "--vacante-id", str(B)])
rc, o = run(["--report", "-"], {"queue_id": QB, "outcome": "bloqueada", "motivo": "captcha"})
res.append(ok(rc == 0 and q()[QB]["estado"] == "bloqueada" and estado(B) == "nueva", "captcha → bloqueada, no fatal, estado nueva"))
rc, o = run(["--vacante-id", str(B)])
res.append(ok(q()[QB]["estado"] == "bloqueada", "bloqueada no se re-encola sin --reintentar"))
rc, o = run(["--vacante-id", str(B), "--reintentar"])
res.append(ok(q()[QB]["estado"] == "pendiente", "--reintentar la re-encola"))
# 6) aplicada (simulada, sandbox) vía alternativa Indeed → DB y dup marcados, log y CRM
run(["--run-queue", "--vacante-id", str(B)])
rc, o = run(["--report", "-"], {"queue_id": QB, "outcome": "aplicada", "confirmation_text": "Tu postulación se ha enviado",
                                "folio": None, "url_usada": "https://mx.indeed.com/viewjob?jk=zzsandboxb"})
e = q()[QB]
res.append(ok(rc == 0 and e["estado"] == "aplicada" and estado(B) == "aplicada" and estado(B2) == "aplicada",
              f"aplicada → queue + vacantes {B} y {B2} 'aplicada'"))
sync = {x["vacante_id"]: x["updateVacancy"]
        for x in json.loads((aa.LOG_DIR / f"apply-crm-sync-{aa.today()}.json").read_text())["entries"]}
u = sync[B]
res.append(ok(u["status"] == "aplicada" and u["appliedDate"] == aa.today() and u["cvFile"] and u["cvVersion"]
              and u["nextStep"] == aa.NEXT_STEP_APLICADA.format(empresa="ZZ Sandbox Beta") and "sin folio" in u["notes"]
              and sync[B2]["status"] == "aplicada", "CRM sync aplicada: appliedDate, cvVersion, cvFile, nextStep, 'sin folio'"))
log = (aa.LOG_DIR / f"apply-{aa.today()}.log").read_text()
res.append(ok(f"APLICADA {QB}" in log and "plataforma=indeed" in log and "folio=sin folio" in log
              and "Tu postulación se ha enviado" in log, "log de aplicación con plataforma, confirmación y 'sin folio'"))
res.append(ok(any(aa.BACKUP_DIR.glob("vacantes-pre-autoapply-*.db")), "backup de la DB antes de escribir"))
# 7) no se registra dos veces
rc, o = run(["--report", "-"], {"queue_id": QB, "outcome": "aplicada", "confirmation_text": "x"})
res.append(ok(rc == 1 and "ya está aplicada" in o, "segunda 'aplicada' rechazada (idempotente)"))
# 8) sin CV no se encola
run(["--vacante-id", str(C)])
res.append(ok(aa.qid(C) not in q(), "vacante sin CV no se encola"))
# 9) dedupe por empresa+título: una vacante ya aplicada que reaparece con otra URL no se re-encola
rc, o = run(["--vacante-id", str(D2)])
res.append(ok(QD2 not in q() and "ya enviada" in o, "duplicado nuevo de una vacante ya aplicada no se re-encola"))
# 10) cupo diario agotado (B quedó aplicada hoy y A en_curso → 2 usados)
import argparse  # noqa: E402
aa_cfg = aa.load_config()
aa_cfg["limites"]["diario"] = 2
out = io.StringIO()
with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
    aa.run_queue(aa_cfg, argparse.Namespace(limit=5, vacante_id=None))
res.append(ok('"entregadas": 0' in out.getvalue(), "cupo diario agotado → 0 tareas entregadas"))

print(f"\n{sum(res)}/{len(res)} PASS  (sandbox {tmp})")
sys.exit(0 if all(res) else 1)
