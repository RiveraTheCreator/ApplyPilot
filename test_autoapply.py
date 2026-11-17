"""Sandbox: copia de vacantes.db + cola/logs/backups temporales. No toca archivos reales."""
import io, json, shutil, sys, tempfile, contextlib
from pathlib import Path
BASE = Path("/home/hatch/workspace/empleo-pipeline")
sys.path.insert(0, str(BASE))
import autoapply as aa, sqlite3

tmp = Path(tempfile.mkdtemp(prefix="aa-"))
dbp = tmp / "vacantes.db"; shutil.copy2(BASE / "vacantes.db", dbp)
aa.QUEUE_PATH = tmp / "apply-queue.json"; aa.LOG_DIR = tmp / "logs"; aa.BACKUP_DIR = tmp / "backups"
D = ["--db", str(dbp)]
def run(argv, stdin=None):
    out = io.StringIO()
    if stdin is not None: sys.stdin = io.StringIO(json.dumps(stdin))
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        rc = aa.main(argv + D)
    return rc, out.getvalue()
def q(): return json.loads(aa.QUEUE_PATH.read_text())["entries"]
def estado(vid): return sqlite3.connect(dbp).execute("select estado from vacantes where id=?", (vid,)).fetchone()[0]
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m) or c
res = []

# 1) encolar itD (495, dup 510) y entregarla
rc, o = run(["--vacante-id", "495"]); res.append(ok(rc == 0 and q()["v495"]["estado"] == "pendiente", "encolar 495 → pendiente"))
rc, o = run(["--run-queue", "--vacante-id", "495"]); res.append(ok(q()["v495"]["estado"] == "en_curso", "run-queue → en_curso"))
# 2) aplicada sin confirmación → rechazado
rc, o = run(["--report", "-"], {"queue_id": "v495", "outcome": "aplicada", "confirmation_text": ""})
res.append(ok(rc == 1 and q()["v495"]["estado"] == "en_curso", "aplicada sin confirmation_text se rechaza"))
# 3) PRUEBA DE BLOQUEO: el formulario pide algo solo-Alex
PREG = "What are your salary expectations (monthly, USD)? *"
rc, o = run(["--report", "-"], {"queue_id": "v495", "outcome": "pendiente-respuesta-Alex", "pregunta": PREG,
                                "url_usada": "https://www.linkedin.com/jobs/view/4488612672"})
e = q()["v495"]
res.append(ok(rc == 0 and e["estado"] == "pendiente-respuesta-Alex" and e["pregunta_alex"] == PREG, "pregunta solo-Alex → pendiente-respuesta-Alex"))
res.append(ok(estado(495) == "nueva" and estado(510) == "nueva", "vacantes.estado sigue 'nueva' (no se envió)"))
sync = json.loads((aa.LOG_DIR / f"apply-crm-sync-{aa.today()}.json").read_text())["entries"]
s = next(x for x in sync if x["vacante_id"] == 495)["updateVacancy"]
res.append(ok(s["status"] == "nueva" and s["nextStep"] == f"Pregunta para Alex: {PREG}" and PREG in s["notes"] and "appliedDate" not in s,
              "CRM sync: status nueva, nextStep 'Pregunta para Alex: <exacta>', sin appliedDate"))
log = (aa.LOG_DIR / f"apply-{aa.today()}.log").read_text()
res.append(ok("PREGUNTA-ALEX v495" in log and "NO enviada" in log, "log registra la pregunta y que no se envió"))
# 4) idempotencia: re-encolar no la duplica ni la reactiva
rc, o = run(["--vacante-id", "495"]); res.append(ok(q()["v495"]["estado"] == "pendiente-respuesta-Alex" and "ya en cola" in o, "re-encolar 495 se omite"))
rc, o = run(["--vacante-id", "510"]); res.append(ok("v510" not in q(), "encolar el duplicado 510 también se omite"))
# 5) bloqueo por sesión/captcha (JD Power 200 en sandbox)
run(["--vacante-id", "200"]); run(["--run-queue", "--vacante-id", "200"])
rc, o = run(["--report", "-"], {"queue_id": "v200", "outcome": "bloqueada", "motivo": "captcha"})
res.append(ok(rc == 0 and q()["v200"]["estado"] == "bloqueada" and estado(200) == "nueva", "captcha → bloqueada, no fatal, estado nueva"))
rc, o = run(["--vacante-id", "200"]); res.append(ok(q()["v200"]["estado"] == "bloqueada", "bloqueada no se re-encola sin --reintentar"))
rc, o = run(["--vacante-id", "200", "--reintentar"]); res.append(ok(q()["v200"]["estado"] == "pendiente", "--reintentar la re-encola"))
# 6) aplicada (simulada, sandbox) vía alternativa Indeed → DB y dup marcados, log y CRM
run(["--run-queue", "--vacante-id", "200"])
rc, o = run(["--report", "-"], {"queue_id": "v200", "outcome": "aplicada", "confirmation_text": "Tu postulación se ha enviado",
                                "folio": None, "url_usada": "https://mx.indeed.com/viewjob?jk=418e29385efe5452"})
e = q()["v200"]
res.append(ok(rc == 0 and e["estado"] == "aplicada" and estado(200) == "aplicada" and estado(207) == "aplicada", "aplicada → queue + vacantes 200 y 207 'aplicada'"))
sync = {x["vacante_id"]: x["updateVacancy"] for x in json.loads((aa.LOG_DIR / f"apply-crm-sync-{aa.today()}.json").read_text())["entries"]}
u = sync[200]
res.append(ok(u["status"] == "aplicada" and u["appliedDate"] == aa.today() and u["cvFile"] and u["cvVersion"]
              and u["nextStep"] == "Esperar respuesta de JD Power; revisar Indeed/LinkedIn y correo" and "sin folio" in u["notes"]
              and sync[207]["status"] == "aplicada", "CRM sync aplicada: appliedDate, cvVersion, cvFile, nextStep, 'sin folio'"))
log = (aa.LOG_DIR / f"apply-{aa.today()}.log").read_text()
res.append(ok("APLICADA v200" in log and "plataforma=indeed" in log and "folio=sin folio" in log and "Tu postulación se ha enviado" in log, "log de aplicación con plataforma, confirmación y 'sin folio'"))
res.append(ok(any(aa.BACKUP_DIR.glob("vacantes-pre-autoapply-*.db")), "backup de la DB antes de escribir"))
# 7) no se registra dos veces
rc, o = run(["--report", "-"], {"queue_id": "v200", "outcome": "aplicada", "confirmation_text": "x"})
res.append(ok(rc == 1 and "ya está aplicada" in o, "segunda 'aplicada' rechazada (idempotente)"))
# 8) url ajena rechazada; cupo diario
run(["--vacante-id", "591"])  # sin CV: no se encola
res.append(ok("v591" not in q(), "vacante sin CV no se encola"))
aa_cfg = aa.load_config(); aa_cfg["limites"]["diario"] = 1
import argparse
ns = argparse.Namespace(limit=5, vacante_id=None)
q0 = json.loads(aa.QUEUE_PATH.read_text()); q0["entries"]["v200"]["estado"] = "aplicada"
out = io.StringIO()
with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out): aa.run_queue(aa_cfg, ns)
res.append(ok('"entregadas": 0' in out.getvalue(), "cupo diario agotado → 0 tareas entregadas"))
print(f"\n{sum(res)}/{len(res)} PASS  (sandbox {tmp})")
