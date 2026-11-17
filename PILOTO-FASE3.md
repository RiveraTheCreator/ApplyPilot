# Piloto Fase 3 — UNA aplicación real (lo ejecuta el agente principal)

Preparado por Claude Code (Opus 5.5) el 2026-11-16. Esta sesión no tiene navegador ni acceso a
crm-empleo: el piloto lo corre el agente principal con su tarea de navegador y la sesión guardada.
**Solo esta vacante.** No correr `--run-queue` sin `--vacante-id 200` hasta cerrar el piloto.

## Vacante
- **JD Power — Commercial Operations Analyst** · id 200 · 5★ · score 48 · 100% remoto (México) ·
  publicado $450,000–$500,000 MXN/año.
- Principal: LinkedIn https://www.linkedin.com/jobs/view/4482001404
- Alternativa (misma vacante, mismo CV): Indeed id 207 https://mx.indeed.com/viewjob?jk=418e29385efe5452
  — usar solo si LinkedIn no ofrece Easy Apply o queda bloqueada. **Nunca aplicar en las dos.**
- CV: `/home/hatch/workspace/cv/cv-jd-power-commercial-operations-analyst.docx` (v1 2026-11-16,
  validado por cvgen; cvFile existe en el CRM).
- Entrada en `apply-queue.json › entries.v200` (estado `pendiente`, paquete completo).
- Por qué no SUNKSSD (1197): pide aplicar por correo con video/texto y pretensión salarial; quedó
  `pendiente-respuesta-Alex` en la cola.

## Pasos
1. Entregar la tarea:
   ```bash
   cd ~/workspace/empleo-pipeline && .venv/bin/python autoapply.py --run-queue --vacante-id 200 --limit 1
   ```
   Devuelve JSON con `instrucciones_navegador`, `paquete` y `reporte_plantilla` (v200 pasa a `en_curso`).
2. CRM: `listVacancies` → fila con url `https://www.linkedin.com/jobs/view/4482001404` (y la de
   Indeed). Si alguna no está en `nueva`: no aplicar, reportar `ya-aplicada`.
3. Lanzar la tarea de navegador con el texto de `instrucciones_navegador` tal cual (sesión guardada;
   sin login, sin contraseñas; CAPTCHA/login/verificación ⇒ detener y reportar `bloqueada`;
   pregunta fuera del paquete ⇒ no enviar y reportar `pendiente-respuesta-Alex` con la pregunta exacta).
   Preguntas probables en JD Power: años de experiencia en operaciones comerciales (pide 2-5),
   Salesforce, pretensión salarial, autorización de trabajo ⇒ **todas son solo-Alex**: detener.
4. Guardar el resultado y registrarlo, p. ej.:
   ```bash
   cat > /tmp/resultado-v200.json <<'JSON'
   {"queue_id": "v200", "outcome": "aplicada",
    "url_usada": "https://www.linkedin.com/jobs/view/4482001404",
    "confirmation_text": "<texto literal del sitio>", "folio": null,
    "detalle": "<lo observado>", "cv_subido": "cv-jd-power-commercial-operations-analyst.docx",
    "observado": "<ISO 8601 -06:00>"}
   JSON
   .venv/bin/python autoapply.py --report /tmp/resultado-v200.json
   ```
5. Aplicar `logs/apply-crm-sync-<fecha>.json` en crm-empleo: `updateVacancy` de las entradas 200
   y 207 (status, appliedDate, cvVersion, cvFile, nextStep, notes) y `uploadCv` con `cv_path`.
   Ese archivo también trae la entrada de SUNKSSD (1197, nextStep "Pregunta para Alex: …").
6. Verificar: `.venv/bin/python autoapply.py --status` (v200 `aplicada`), línea `APLICADA v200` en
   `logs/apply-<fecha>.log` con confirmación y folio o "sin folio", y
   `sqlite3 vacantes.db "select id,estado from vacantes where id in (200,207)"` → `aplicada`.

Si la tarea se cae a mitad de camino sin saber si se envió: NO reintentar; revisar
LinkedIn › Mis empleos / Indeed › Mis empleos y reportar `aplicada` (con el texto que aparezca ahí)
o `fallida`.
