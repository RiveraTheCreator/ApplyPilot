# ApplyPilot — Diseño del pipeline automatizado de búsqueda de empleo (Alex)

## Objetivo
Conseguir un rol 100% remoto, $35,000–$40,000 MXN/mes, contratación desde México, lo más rápido posible (meta feb 2027). El sistema scrapea vacantes a diario, filtra, rankea, genera CVs a medida (sin mentir), aplica automáticamente y da seguimiento en un CRM. A Alex solo le llegan entrevistas y pendientes.

## Fuente de verdad
- Estrategia: Notion "Estrategia de Búsqueda — Isaac Rivera — 2026-11-13" (11 aristas, keywords en `keywords.json`).
- Reglas de CV: `~/workspace/custom-skills/ats_cv_per_vacancy/SKILL.md` (releer antes de cualquier trabajo de CV) + plantilla LaTeX en Notion ("Plantilla CV ATS — Alejandro Rivera").
- El CV prompt lo da Alex cuando lleguemos a ese módulo — pedirlo entonces, no inventarlo.

## Módulos
1. **Scraper diario** — JobSpy (LinkedIn, Indeed, Glassdoor, ZipRecruiter) + Firecrawl (career pages de empresas, extracción de vacantes, redes sociales) + alertas de Indeed que ya llegan por Gmail. Fuentes por arista según Notion. Deduplicación por (empresa, título, URL). Guardar en SQLite: `vacantes` (título, empresa, ubicación, modalidad, sueldo, URL, fecha publicación, fuente, aristas/keywords que matchearon, estado). **Perfiles JobSpy en `search-profiles.json` (spec en `scraper-config-spec.md`, de Mi Primera Chamba): 20 base México (Indeed 8, LinkedIn 7, Glassdoor 5) + 39 regionales desde 2026-11-16 (LATAM + US/CA; ZipRecruiter solo US/CA) = 59. El scraper consume este archivo, no keywords sueltas.** Estado real 2026-11-15 19:06: JobSpy-LinkedIn e Indeed funcionan; Glassdoor y ZipRecruiter bloqueados (curl 56 / 403 proxy) — Firecrawl cubre career pages.
- **Búsqueda lo más amplia posible (directiva de Alex 2026-11-15 19:12):** combinaciones de plataformas × keywords (search-profiles.json) + plataformas nativas de las empresas (career pages vía Firecrawl: Wellfound, Get on Board, Torre, Remote OK, We Work Remotely, páginas de agencias y empresas objetivo). Ninguna fuente es opcional por diseño: si JobSpy bloquea una fuente, Firecrawl la cubre.

## Optimización — ciclo de retroalimentación (acordado con Alex 2026-11-15)
El embudo vive en el CRM: nueva → aplicada → respuesta → entrevista → rechazada. Umbrales que disparan revisión:
- 15 aplicaciones sin ninguna respuesta → revisar CV y filtros (¿roles demasiado lejanos? ¿título no matchea?).
- 25 aplicaciones sin entrevista → cambio de estrategia: reordenar aristas, ajustar pesos del ranking, retirar keywords muertas, sumar nuevas.
- Revisión semanal del embudo aunque no se cumplan umbrales: qué aristas dan respuesta, cuáles rechazos rápidos, cuáles silencio.
Señales y su lectura:
- Rechazo en horas = filtro duro (años, ubicación, requisito) → cambiar el tipo de rol al que se aplica.
- Silencio total = CV no pasa ATS o título no matchea → reescribir con las keywords exactas de la vacante.
- Arista con 0 respuestas en 10 aplicaciones baja de prioridad; arista con entrevistas sube y se duplican keywords parecidas.
El review diario vigila los umbrales y avisa cuando se cumple uno.

## CRM — panel de embudo + acceso a CVs (directiva de Alex 2026-11-15 19:30)
- Panel Embudo en crm-empleo: conteos por estado, conversiones, y alertas cuando se cumple un umbral (15 aplicaciones sin respuesta → revisar CV/filtros; 25 sin entrevista → cambio de estrategia; revisión semanal).
- Cada vacante aplicada muestra el CV enviado con enlace para abrir/descargar (campo cvFile). CVs existentes: Alianza Corp id 4 → cv-alianza-corp-arquitecto-soluciones-m365-ia.docx; Deliverect id 1 → cv-deliverect-implementation-consultant.pdf; Yalo TPM id 2 → cv-yalo-technical-pm.pdf; Yalo ASM id 3 → cv-yalo-agentic-success-manager.pdf. Fase 3 adjunta el CV al aplicar.
- Retro: cada vacante con respuesta/entrevista tiene sección de retro (qué preguntaron, qué funcionó, resultado); el review diario pide la retro cuando hay respuesta/entrevista nueva. Alex es la fuente de la retro de entrevistas; el sistema la pide, no la inventa.
- Prompt de build guardado en `.build-prompt-crm-funnel.md`; ejecución programada tras el reset de límites de Claude (dom 15 nov 23:50 CST) con runonce `crm-funnel-cv-resume` a las 23:55.

## Robustez — sistema anti-bugs (directiva de Alex 2026-11-15 19:12)
- Ningún fallo de una fuente tumba la corrida: reintentos con backoff, delays entre búsquedas, rotación de user-agent, degradación elegante (la fuente que falla queda en el log y la corrida sigue).
- ZipRecruiter 403 y Glassdoor 403/curl 56 (2026-11-15): resolver con Fable 5.2 + subagentes — proxy, reintentos, o Firecrawl como respaldo. LinkedIn 429: backoff y espaciado.
- Todo error queda en logs/YYYY-MM-DD.log con causa; la corrida nunca inventa datos ni marca éxito sin verificación.
2. **Filtro + ranking** — (v1, reemplazado por Ranker v2 el 2026-11-16: único descarte = 100% presencial, estrellas 1–5; ver README › Ranker v2.) Filtros duros v1: 100% remoto, elegible desde México, sueldo ≥ $35k MXN (o sin sueldo pero con señal de rango), alineación a aristas. Score: match de keywords, seniority intermedio (no senior 5+ años), velocidad de proceso (Easy Apply, "contratación inmediata", publicación < 7 días), boost sector hotelería. Salida: lista rankeada diaria.
3. **Generación de CV** — (FASE 2, pendiente del prompt de Alex) Base LaTeX + reglas por vacante. Un CV por vacante, sin mentir, con [confirmar] para lo no verificado. Compilar con pdflatex.
   - **CONSTRUIDO 2026-11-16 (Claude Code, Opus 5.5):** `cvgen.py` + `profile.json` (hechos verificados ES/EN; el módulo selecciona y reordena, no redacta hechos). .docx directo con python-docx (sin pdflatex). Lo no verificado no entra al CV: va a `confirmar` en `logs/cv-coverage-*.json`. Registro en tabla `cv_generaciones`; sync del CRM en `logs/cv-crm-sync-*.json` (lo aplica Kavi). Primeros 3 CVs: JD Power (200, dup 207), SUNKSSD (1197), itD (495, dup 510). Ver README › Fase 2.
4. **Auto-apply** — (FASE 3, pendiente de autorización explícita de Alex + credenciales) LinkedIn Easy Apply, Indeed, ATS (Greenhouse/Lever/Workable) vía browser o APIs. Registrar cada aplicación (fecha, plataforma, estado) en el CRM. Respetar CAPTCHAs y límites de cada sitio; no forzar accesos bloqueados.
   - **CONSTRUIDO 2026-11-16 (Claude Code, Opus 5.5):** `autoapply.py` + `apply-config.json` + `apply-queue.json`. El módulo selecciona (solo vacantes con CV de Fase 2; dedup LinkedIn/Indeed), arma el paquete (datos de `profile.json`, respuestas con fuente, lista solo-Alex) y registra resultados; el envío lo hace el agente principal con su tarea de navegador y la sesión guardada (`--run-queue` → `--report`). Login/CAPTCHA ⇒ bloqueada; pregunta fuera del paquete ⇒ pendiente-respuesta-Alex. Cupo 10/día, delays 60–180 s. Sync del CRM en `logs/apply-crm-sync-*.json`. Piloto (JD Power id 200) preparado en `PILOTO-FASE3.md`, pendiente de ejecutar por el agente principal. Ver README › Fase 3.
5. **CRM (artefacto web)** — Fullstack, visible para Alex en su sidebar: tablero de vacantes (nueva, aplicada, respuesta, entrevista, rechazada), filtros por arista/estado, detalle por vacante (descripción, score, CV enviado, mensajes). Actualización automática por el pipeline.
7. **Monitor del pipeline** — `monitor.py` (CONSTRUIDO 2026-11-16, Claude Code, Opus 5.5): vigila de punta a punta scraper → ranker → CVs → apply → embudo/CRM, solo lectura (DB `mode=ro`, cola), escribe `logs/monitor-YYYY-MM-DD.json` + resumen de 5–10 líneas para el review diario. Umbrales 15/25 (avisos 12/20; rechazo = respuesta para 15 y = sin entrevista para 25), conversión nueva→aplicada sobre 4★+, `en_curso` > 24 h = crítico, tabla por arista, keywords muertas, mayor fuga y top 3 movimientos con sus números. Estado del CRM desde `logs/crm-state-YYYY-MM-DD.json` (export del agente principal); sin export ⇒ atención y embudo con DB + cola. Lo llama `run_daily.py` al final en try/except (nunca cambia el exit code). Ver README › Monitoreo.
6. **Notificaciones y seguimiento** — Revisión diaria de Indeed/LinkedIn/Gmail buscando respuestas y entrevistas; notificar a Alex solo: entrevista (cuándo), qué falta (documentos, pruebas), respuestas relevantes. Resumen diario corto en chat.

## Stack
- Python (JobSpy), Firecrawl (MCP/CLI; API key solo vía variable de entorno, nunca en archivos ni en chat), Fable 5.2 vía Claude Code (`~/.npm-global/bin/claude --model fable`, login claude.ai vigente hasta 2026-12-10) para construir el código; subagentes para scraper, ranking y CRM.
- Datos en `~/workspace/empleo-pipeline/` (keywords.json, vacantes.db, logs). Cron diario para el scrapeo; artefacto CRM en sidebar.
- Ventana Fable 5.2: 48h desde dom 15 nov 2026 18:41 CST (hasta mar 17 nov ~18:41 CST).

## Reglas
- No mentir en CVs ni aplicaciones; lo no verificado va con [confirmar] y se pregunta a Alex.
- No mover dinero. **Auto-apply AUTORIZADO por Alex 2026-11-15: aplicar directo en su nombre y actualizar el CRM.** Credenciales (LinkedIn, Indeed) por Secure Vault; nunca extraer contraseñas crudas a código o archivos.
- **Ranking (actualizado 2026-11-15): los años de experiencia son el criterio de MENOR prioridad; nunca filtrar ni descartar por años. Lo que importa es llegar a las entrevistas correctas (alineación de rol, 100% remoto, sueldo, proceso rápido).** Ver `ranking-rules.md`.
- Prompt de CV recibido 2026-11-15, guardado en `cv-prompt.md` (Fase 2). Entrega .docx según el prompt, con base en la plantilla .tex.

## Fases
- Fase 1 (ahora, 48h Fable): scraper + filtro/ranking + CRM scaffold + cron diario.
- Fase 2 (DESBLOQUEADA 2026-11-15, prompt en cv-prompt.md): generación de CVs por vacante, .docx, reglas ATOP, cero mentiras, habilidades transferibles.
- Fase 3 (AUTORIZADA 2026-11-15): auto-apply directo + seguimiento de bandejas (Indeed/LinkedIn). Credenciales LinkedIn/Indeed solicitadas por Secure Vault.
- Fase 4 (REGISTRADA 2026-11-15 21:23, PRIORIDAD BAJA — solo después de que Fase 2 y 3 estén corriendo): ranker mixto. (1) Base cuantitativa: deduplicar por empresa+título antes de rankear; calibrar pesos con resultados reales del embudo (lo que convierte sube, lo que no baja). (2) Segunda pasada cualitativa con LLM solo al top 30-50: extraer datos estructurados (letra chica del remoto, requisitos escondidos, encaje transferible, porqué corto), nunca un número mágico sin auditar. (3) Loop de aprendizaje con la retro del embudo (respuestas/entrevistas/rechazos rápidos) + exploración ocasional de vacantes de score bajo pero interesantes. No frenar el pipeline actual por esto.
- **Ranker v2 (ORDENADO por Alex 2026-11-15 21:29, build en el reset 23:50, ANTES de Fase 2): estrellas internas 1-5; ÚNICO descarte = 100% presencial; sueldo se conserva con menos peso (máx 8, desconocido = 4 neutro, nunca inventado); cobertura regional LATAM + América del Norte por país; autovigilancia del filtro (`logs/filter-health-YYYY-MM-DD.json` + alertas + `filter-config.json` versionado con changelog, corrección por Kavi/agentes reportada en el review diario). Spec en `.build-prompt-ranker-v2.md`, reglas en `ranking-rules.md` v2.**
  - **CONSTRUIDO 2026-11-16 (Claude Code, Opus 5.5):** `filter_rank.py` v2 + `filter_health.py` + `filter-config.json` 2.0.0 + `filter-changelog.md`; `search-profiles.json` 20 → 59 perfiles (`build_regional_profiles.py`). Re-rank de 767 filas: pasan 693, descartadas 74 (solo 100% presencial); 5★ 8 · 4★ 24 · 3★ 165 · 2★ 170 · 1★ 326. Sync del CRM pendiente de aplicar por Kavi desde `logs/crm-sync-ranker-v2.json` (sin acceso al artefacto desde Claude Code).
