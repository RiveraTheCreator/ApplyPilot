# Auditoría ApplyPilot — 2026-11-20/21 (Opus, 2 sesiones)

**Fecha:** 2026-11-20 (sesión 1, auditoría) y 2026-11-21 ~00:00 (sesión 2, cableado reapunte)
**Auditor/implementador:** Claude Code Opus 5.5 (2 sesiones `-p --model opus`)
**Coordinación:** subagente de auditoría (reporta al coordinador; sin contacto con Alex)
**Repo:** `~/workspace/empleo-pipeline/` (ApplyPilot)
**Informes de las sesiones:** `/tmp/opus-audit-2026-11-20.md` (sesión 1),
`/tmp/opus-wiring-2026-11-20.md` (sesión 2); simulación de B: `/tmp/sim_reapunte.py`;
scripts de verificación de la sesión 2: `/tmp/wiring/verify.py`, `/tmp/wiring/dedupe.py`

## 0. Línea base verificada (2026-11-20/21)

- DB `vacantes.db`: 2.429 filas (2.396 `nueva`, 33 `aplicada`; md5
  `e74e8283aed90142b6b809dc8f81dfcd`, idéntico al backup pre-reapunte de B —
  la DB real **no se movió** entre la medición de B y esta auditoría).
- Ranker 2.0.0 (re-rank fresco): pasan 2.318 (95,4%), descartadas 111 (100%
  presencial); 5★ 19 · 4★ 218 · 3★ 589 · 2★ 728 · 1★ 764; 4★+ total 237;
  4★+ en `nueva` 205 (151 dedup empresa+título); 5★ en `nueva` 8 (6 dedup).
- Cola `apply-queue.json`: 70 entradas (24 aplicadas, 15 pendientes-respuesta-Alex,
  31 bloqueadas).
- Embudo (monitor 2026-11-20, confianza alta, desde CRM): 29 aplicaciones,
  **7 respuestas (las 7 son rechazos; 0 respuestas vivas/positivas)**,
  0 entrevistas. Conversión 4★+→aplicada **25/175 = 14,3%** (dedup).
  Umbrales cruzados: sin respuesta 22/15 CRÍTICO; sin entrevista 29/25 CRÍTICO.
- Scraper 20-nov: 55 filas nuevas (LinkedIn 41, Firecrawl 14); Indeed /
  Glassdoor / ZipRecruiter 0 por segundo día; 48 búsquedas omitidas por circuit
  breaker; 9 búsquedas fallidas (Glassdoor 2×403, Indeed 2×403, LinkedIn 2×429,
  Zip 2×403, Torre 1 sin parseables).
- Firecrawl: ~1.469 créditos conservadores restantes, expiran 2026-12-02;
  gasto observado ~31 créditos/día; rendimiento ~14 filas nuevas/día.
- Baseline de tests antes de la auditoría: `test_monitor.py` 21/21 PASS,
  `test_covergen.py` 29 OK, **`test_autoapply.py` FALLABA** (KeyError `v495`:
  la vacante 495 ya estaba `aplicada` y el test asumía que era encolable).

## 1. Hallazgos ordenados por impacto

### H1 — CRÍTICO · El circuit breaker apagaba LinkedIn aunque sí entregaba vacantes
`scraper.py`, `run_search` + `CircuitBreaker.record_failure`.
JobSpy reporta el 429 en `df.attrs["errors"]` cuando falla una página posterior
de la paginación, pero el código trataba "filas + error" como fallo total:
sumaba 1 al breaker por intento y reintentaba. Con umbral de 5 intentos, el
breaker se abrió en el perfil 13 (20-nov: perfil 12 devolvió 10 filas con 429;
perfil 13 devolvió 28 filas + 7 nuevas también con error). Resultado: 24
búsquedas de LinkedIn omitidas, cuando LinkedIn aportó 41 de las 55 filas
nuevas del día. Agravante: cada reintento reemplazaba las filas del intento
anterior — un reintento peor podía borrar filas ya obtenidas.

### H2 — CRÍTICO · Doble aplicación posible: vacante ya aplicada reaparece con otra URL y se re-encola
`autoapply.py`, `build()` / `ranked_groups()`. Solo tomaba filas
`estado='nueva'` y buscaba en la cola solo por id. Si la misma vacante
(misma empresa y título) reaparece con otra URL, sus filas nuevas formaban un
grupo sin ids en la cola y se volvían a encolar. 4 casos reales en copia:
EPAM Chief (96 aplicada; 3693, 3743 en nueva), EPAM Lead (145; 1687, 3698),
Viva Talent Sr RevOps Manager (195; 1529, 3598 — en el top-15 del día),
Crystalia CRM Sales Ops (292; 2700). Efecto secundario: `cvgen` se niega a
generar CV para esos grupos ("ya enviada"), así que `autoapply` los reportaba
como "pendiente de Fase 2" y **ocupaban lugares de la ventana `--limit`** en
cada corrida sin poder aplicarse nunca.

### H3 — ALTO · Indeed, Glassdoor y ZipRecruiter bloqueados; el retry quemaba ~2,5 min por sitio
Indeed y ZipRecruiter responden 403 también a través de los proxies Squid
(IPs de datacenter de DigitalOcean). Glassdoor responde
`curl: (7) CONNECT tunnel failed, response 403`: es el **proxy** (Squid/egress)
el que rechaza el CONNECT, no el sitio — problema de ACL del proxy, no del
sitio. Cada sitio hacía 4 intentos con esperas de 10/30/90 s antes de abrir el
breaker. Perfiles afectados: 17 Indeed + 7 Glassdoor + 2 Zip, casi 45% del plan
de `search-profiles.json`.

### H4 — ALTO · Embudo "CRÍTICO" por 0 respuestas: medición sobre aplicaciones inmaduras
`monitor.py`, `build_funnel`. El monitor ya calculaba `maduras` (≥7 días) pero
el umbral se evaluaba sobre el total. Snapshot 20-nov: 0 de 24 aplicaciones
con ≥7 días (la más vieja, 4 días; 9 del 16-nov, 12 del 19-nov, 3 del 20-nov).
Con una tasa de respuesta típica de ~3%, P(0 respuestas en 24) ≈ 48%: todavía
no es señal. Además `crm-state-2026-11-20.json` se escribió después del monitor
(~22:04 CST), así que el embudo se armó con "confianza baja" ese día.

### H5 — ALTO · Conversión 4★+→aplicada ~14%: dónde se pierde (205 grupos 4★+ en `nueva`)
| Causa | Tamaño | Estado |
|---|---|---|
| Sin CV (no se puede aplicar; `build()` no sustituye sin CV dentro de la ventana) | 116 de 205 (57%) | Recomendación: `cvgen --top N` antes de encolar en el cron |
| Ventana desperdiciada por grupos ya enviados | ver H2 | **Arreglado (sesión 1)** |
| Cola bloqueada | 31/70 (44%): 10 `vacante_cerrada`, 7 cuenta externa, 4 sitio bloqueado, 3 login, 3 captcha, 2 verificación, 2 otro | Podar las 10 cerradas; 1 reintento a las demás |
| Latencia scrape→aplicación | 10 `vacante_cerrada` se intentaron 2–11 días después de publicadas; mediana scrape→encolado 2 días, máx 4. **El cron de aplicación (07:00) corre ANTES del scrape (07:51)**: lo nuevo nunca se aplica el mismo día | Recomendación: mover aplicación a ~09:30, tras scrape + cvgen |
| Firecrawl casi no aplicable | Plataformas `otro` + Wellfound: 0 aplicadas de 10 intentos (WWR y GetOnBoard piden cuenta o tienen Cloudflare) | Redirigir créditos a `empresas` (páginas de carrera/ATS) |
| Preguntas para Alex | 15/70 (21%) | Ver H6 |

### H6 — ALTO · 15 vacantes atoradas en "pendiente-respuesta-Alex": no había transición soportada
`autoapply.py`: `STICKY` incluye `PREGUNTA` y `--reintentar` solo reabría
`bloqueada`/`fallida`. Historial de v4377: dos transiciones `PREGUNTA→en_curso`
hechas **a mano** en `apply-queue.json`, con timestamps **sin zona horaria**,
sin línea de log `EN_CURSO` y sin sumar `intentos`. `monitor._parse_dt`
interpretaba esos timestamps sin zona como UTC: 6 h de desfase al medir la
antigüedad. Composición de las 15: 2 piden aplicar por correo (la más vieja
4,9 días); 3 piden texto libre o carta (250–400 palabras); 7 son preguntas de
filtro recurrentes (candidatas a `respuestas_conocidas` con fuente y
confirmación de Alex); 1 pide un número de identificación (nunca se automatiza).

### H7 — MEDIO · `filter_rank` reescribía score/estrellas/pasa_filtro de todas las filas sin backup
La convención de AGENTS.md (respaldar antes de re-rankear) era manual y el cron
no la cumplía. El CLI suelto `filter_rank.py` sigue sin backup propio.

### H8 — MEDIO · El log diario usaba fecha UTC y el monitor lo buscaba con fecha de México
`run_daily.setup_logging`. Host en UTC: después de las 18:00 CST el archivo
caía en "mañana" y `monitor.parse_failures` no lo encontraba (mismo problema
ya documentado para filter-health).

### H9 — MEDIO · El cupo diario cuenta `en_curso` de cualquier día
`autoapply.applied_today`. Las `en_curso` olvidadas de días anteriores consumen
el cupo de hoy indefinidamente. Hoy hay 0 `en_curso`: no afecta ahora.

### H10 — MEDIO · Las filas `vacante_cerrada` siguen en `nueva` en la DB
Siguen rankeando, inflan el denominador de 4★+ del monitor y `cvgen` puede
generarles CV. Falta un estado de negocio nuevo (p. ej. `cerrada`): decisión de
esquema + CRM.

### H11 — BAJO · `--reintentar` con coincidencia solo por clave puede dejar dos entradas por grupo
Si cambia la fila primaria (otra id): la vieja queda `bloqueada` y la nueva
`pendiente`. Caso raro; documentado, no implementado.

### H12 — BAJO · El top del log diario repetía vacantes
`format_report` no deduplicaba por empresa+título (la DB deduplica por URL).
"AI Trainer…" ×2, "Viva Talent…" ×2. Cosmético, pero gastaba lugares del
top-15 del review. **Arreglado en sesión 2** (ver §3).

### H13 — BAJO · Firecrawl: créditos alcanzan, rendimiento no
~1.469 créditos hasta el 2026-12-02 con gasto de hasta 100/día: alcanza. Pero
el rendimiento es de ~14 filas nuevas/día y la mayoría viene de fuentes donde
no se puede aplicar sin cuenta (ver H5). Conviene redirigir los créditos más
que ahorrarlos.

### H14 — (Sesión 2) La config 2.1.0 era INERTE sin cableado — hallazgo crítico de B, confirmado
`filter_rank.py:compute_stars()` solo leía `stars.base` / salary caps /
friction cap; no existía ninguna llave de seniority ni de región, y el score
solo ordena dentro de un tier (`autoapply` ordena por `estrellas DESC, score
DESC`; `cvgen` solo genera CV para `estrellas >= 4`). Una penalización solo de
score **no cambia ni la cola ni la elegibilidad de CV**. La penalización tenía
que ser un **cap de estrellas**, y eso exigía cableado. **Cableado hecho en
sesión 2 (ver §2).** Lo mismo aplica a `cv-prompt.md`: `cvgen.py` no lo lee en
runtime (solo lo cita en un comentario, línea 14); los 6 edits de B son
especificación hasta que se cableen (ver §4).

## 2. Cableado del reapunte 2.1.0 (sesión 2) — HECHO y verificado en copia

Spec: `docs/reapunte-2026-11-20.md` §4; config propuesta:
`docs/filter-config-2.1.0-proposed.json` (NO aplicada al `filter-config.json`
real, que sigue en 2.0.0 — territorio de B/coordinador).

### 2.1 Qué se cableó (todo en `filter_rank.py`)
| Qué | Dónde |
|---|---|
| `build_seniority_regex(fcfg)`: compila `seniority.title_re` (re.I); None si falta | `filter_rank.py:284` |
| `build_region_matcher(fcfg)`: compila `region.excluded_title_location_re` y normaliza `excluded_location_countries`; vacíos si faltan | `filter_rank.py:290` |
| `is_senior_title(title_norm, re)`: solo título | `filter_rank.py:298` |
| `is_region_excluded(title_norm, loc_norm, re, países)`: regex sobre `f"{title_norm} {loc_norm}"` + países por subcadena en ubicación; la descripción NO entra | `filter_rank.py:303` |
| `compute_stars(..., senior=False, region_excluded=False)`: caps DESPUÉS de todas las demás reglas; solo si la config trae `stars.seniority_title_cap` / `stars.region_excluded_cap` | `filter_rank.py:315`, `:337-340` |
| `score_row()` paso 8: `parts["seniority"]` y `parts["region"]` (−`weights.*_penalty`, con `seniority/region.score_penalty` como respaldo); solo si la config trae la sección | `filter_rank.py:424-438` |
| Score acotado a `[0, 100]` | `filter_rank.py:441` |
| Flags `senior_title` y `region_excluded` | `filter_rank.py:457-459` |
| `group_ranked()` + `format_report()`: top del reporte agrupa por empresa+título (ver §3) | `filter_rank.py:559`, `:568` |

Desviaciones de la spec de B, todas a propósito y documentadas en
`/tmp/opus-wiring-2026-11-20.md`:
- Las penalizaciones se calculan antes de sumar el score (la spec las ponía
  "después de `friction`", pero `friction` se calcula después del score; el
  resultado es el mismo).
- Piso de 0 en el score (`max(0, …)`). Con 2.0.0 no cambia nada (verificado:
  0 filas distintas). Costo: entre filas penalizadas con score base <20
  (todas ≤3★) se pierde el orden fino por empates en 0; ningún consumidor
  depende de scores negativos.
- Regex sobre el texto normalizado (sin acentos): "Sênior" (portugués) también
  dispara como senior (+9 filas solo bajan score, ver §2.3).

**Compatibilidad hacia atrás:** sin `seniority`/`region` en la config no hay
partes nuevas, ni caps, ni flags. Verificado: código HEAD vs código nuevo con
config 2.0.0 real → **0 de 2.429 filas con diferencia**. No se tocaron
`classify_modality`, `is_onsite_only`, `ONSITE_TEST_CASES` ni `filter_health.py`.

### 2.2 Verificación en copia — reproduce la simulación de B (verificación independiente del coordinador: idéntica)
Método sesión 2: tres copias idénticas, `evaluate(all_rows=True,
write_health=False)` con código viejo + 2.0.0, código nuevo + 2.0.0 y código
nuevo + 2.1.0 propuesta; además CLI
`filter_rank.py --db <copia> --config docs/filter-config-2.1.0-proposed.json --all --no-health --top 15`.
El coordinador repitió la corrida 2.0.0 vs 2.1.0 en copias frescas en
`/tmp/verify-coord/` y obtuvo **exactamente los mismos números**.

| Métrica | Base 2.0.0 | Con 2.1.0 | Δ medido | Δ esperado (B) |
|---|---|---|---|---|
| Evaluadas | 2.429 | 2.429 | 0 | 0 |
| Pasan / Descartadas | 2.318 / 111 | 2.318 / 111 | sin cambio | sin cambio |
| 5★ | 19 | 7 | −12 | −12 |
| 4★ | 218 | 160 | −58 | −58 |
| 3★ | 589 | 631 | +42 | +42 |
| 2★ | 728 | 756 | +28 | +28 |
| 1★ | 764 | 764 | 0 | 0 |
| **4★+ total** | **237** | **167** | **−70 (−29,5%)** | −70 |
| 4★+ en `nueva` | 205 | 146 | −59 | −59 |
| 4★+ en `nueva`, dedup | 151 | 111 | −40 | −40 |
| 5★ en `nueva` (dedup) | 8 (6) | 3 (3) | −5 (−3) | −5 (−3) |
| 4★+ en `nueva` sin CV | 116 | 87 | −29 | −29 |
| Filas con cambio de estrellas | — | 87 | — | — |
| Filas tocadas (estrellas y/o score) que pasan | — | 604 | — | 595 |
| Filas tocadas, total | — | 623 | — | (B solo contó las que pasan) |

**Estrellas: 0 divergencias contra la simulación de B.**
Por qué 604 y no 595 (explicado, sin efecto en estrellas): +9 títulos
"Sênior" (v1692, 1703, 1705, 1715, 1717, 1722, 2834, 2837, 3753) que B no
cazaba con el regex sobre el título crudo; ya estaban en ≤3★, solo bajan
score. +19 filas descartadas (100% presencial) reciben penalización de score
pero siguen descartadas con estrellas NULL.

Casos puntuales (copia, verificación independiente del coordinador):
| Caso | Base → 2.1.0 | Resultado |
|---|---|---|
| Pearson v4377 (Poland, aplicada) | 4★ 32 → **2★** 22 | baja por región ✔ |
| v2096 AI Engineer, London, Ontario | 4★ 24 → 4★ 24 | NO penalizada ✔ |
| v2607 "…for Lead Generation Company" | 4★ 27 → 4★ 27 | NO penalizada ✔ |
| v254 "Sr. Associate…" | 4★ 27 → **3★** 17 | penalizada ✔ |
| v2243 n8n Senior, German Speaking | 4★ 22 → 2★ 2 | ambos flags ✔ |
| v1676 LawnStarter Staff | 4★ 36 → 3★ 26 | senior ✔ |

**Conclusión: el cableado está listo.** Faltan los pasos (b) aplicar la config
2.1.0 y (c) re-rank de la DB real — le tocan al coordinador (ver §6, checklist
de B §7). Como la DB no se ha movido, en el re-rank real deben salir
exactamente estos números (salvo decaimiento de recencia según el día).

### 2.3 Tests nuevos de la sesión 2 — `test_filter_rank.py` (16 tests)
Detector senior (Senior, Staff, Principal, Chief, Lead, "AI Engineering
Lead", `Sr. Associate`, Director, Head of, VP, "Engineer Senior" disparan;
"for Lead Generation Company", Account Manager, Technical Account Manager,
"Seniority-agnostic", "Leader" NO disparan). Detector de región (EMEA, India,
Pune, Polonia/Poland, "Warszawa, Polonia", Remote Europe, Asia Only,
"(Philippines)" disparan; London Ontario, "Remote" genérica, vacía, Mexico
City, Remote US NO disparan; países en la descripción se ignoran). Caps
después de las demás reglas (5→3, 5→2, ambos→2), nunca suben estrellas.
`score_row` end-to-end (ambos flags −20, piso de score). No-op con 2.0.0
(config propuesta sin claves nuevas y `filter-config.json` real). Agrupación
del top del reporte.

## 3. Dedupe: LawnStarter ×8 y el hueco sistémico (sesión 2 + contraste del coordinador)

- LawnStarter "Staff Software Engineer, Product": 8 filas, todas LinkedIn,
  4★, `nueva` (ids 1676, 1677, 2357, 2358, 2709, 2911, 2985, 3638),
  scrapeadas 16–18 nov con job IDs distintos.
- **Origen (sesión 2): no es un error del scraper.** LinkedIn publica el mismo
  rol una vez por país, con job IDs distintos (LawnStarter: Río, São Paulo,
  México ×2, CDMX, Colombia ×2, Perú, Uruguay). De los 184 grupos: 157 de una
  sola fuente, 27 multi-fuente; 133 del mismo día, 51 de varios días; solo 28
  tienen alguna URL con query. `UNIQUE(empresa, titulo, url)` incluye la URL,
  así que la DB no los puede unir — y es correcto que no lo haga: son
  publicaciones distintas.
- **Escala real (mayor que el caso LawnStarter):** 184 grupos empresa+título
  con >1 fila en `nueva` que pasan el filtro; 349 filas fantasma (coordinador,
  conteo SQL crudo sobre toda `nueva`: 351; diferencia de 2 por filas
  descartadas; en 4★+ el coordinador contó 54 fantasmas vs 55 de la sesión 2
  por agrupación normalizada vs cruda — mismo orden de magnitud).
  4★+: 205 filas / 150 grupos (55 fantasmas; con 2.1.0: 146/110, 36 fantasmas).
  Top: FullStack "Forward Deployed Engineer (TypeScript + AI)" ×39 y BairesDev
  "Full-stack Python AI Engineer" ×19 (ambos 2–3★), Resolv Global ×9,
  SecurityScorecard ×9, Cloudbeds ×8, LawnStarter ×8, InAir ×7 (4★).
- **Caminos ya cubiertos (sin riesgo de doble aplicación):** `autoapply`
  agrupa por `cvgen.dedup_key` (sesión 1 añadió dedupe contra enviadas y
  contra la cola); `cvgen` genera 1 CV por grupo y el resto queda `duplicado`
  (LawnStarter 1+7; InAir 1+6); el monitor deduplica la conversión por `gkey`.
- **Huecos (solo visualización/métricas):** el top del log diario listaba por
  fila (6 de 15 líneas repetidas); `cobertura_cv_top` / `top_nueva_sin_cv` /
  `nueva_top_sin_tocar` cuentan filas (116 "sin CV" son 92 vacantes distintas);
  los conteos de estrellas de `filter_health` son por fila.
- **Arreglo implementado (seguro, solo salida):** `filter_rank.group_ranked()`
  y `format_report()` agrupan por empresa+título normalizados; cada vacante
  sale una vez con `(+N publicaciones más del mismo rol: ids …)`; nueva línea
  "Vacantes distintas (empresa+título) que pasan: N | publicaciones repetidas:
  X en Y grupos" (en copia con 2.1.0: 1.963 distintas, 355 repetidas en 186
  grupos). No se cambian filas, score, estrellas, esquema ni claves de `db.py`.
- **Recomendación (no implementada):** en el monitor, mostrar junto a
  `top_nueva_sin_cv` y `nueva_top_sin_tocar` el nº de vacantes distintas
  además de filas (~5 líneas reusando `gkey`). Toca texto de checks que lee el
  agente principal: decisión del coordinador. **Dedupe en el ranker: NO
  recomendado** (fusionar/anular filas pierde las URLs por país; LinkedIn
  puede aceptar la aplicación solo en alguna de ellas).

## 4. Chequeo de región en tiempo de scrapeo: NO implementar (sesión 2, con números)
- Pearson v4377 **no es un caso de "ubicación ausente"**: entró por LinkedIn
  el 2026-11-20 14:07Z ya con `ubicacion='Poland'`. Faltaba la regla, no el
  dato. Con la 2.1.0 activa, `run_daily` corre el ranker en la misma corrida
  que el scrape, antes de cualquier tanda de aplicación: el cap llega a tiempo.
- Filas con región excluida: **135 de 2.429** (LinkedIn 72,
  `firecrawl:empresas` 59, Wellfound 2, WWR 2). Por estado: 134 `nueva`,
  1 aplicada (v4377). Solo **11** habrían sido 4★+ con la 2.0.0.
- Firecrawl: 63 filas con región excluida, **52 de la página de carrera de n8n**
  (n8n contrata casi solo en EMEA: 52 de sus 69 filas) y 7 de Deliverect. Solo
  **~4 de las 63** consumieron un scrape de detalle (1 crédito c/u) en 5 días;
  el resto viene de la página de listado (1 crédito por página, sin importar
  cuántas vacantes traiga). **Ahorro potencial: ~4 créditos en 5 días
  (<1/día).** El tope de detalle es fijo (`DEFAULT_LIMIT = 5` por corrida):
  el pre-filtro no ahorraría créditos, solo reasignaría ~1 slot de detalle
  cada 1–2 días.
- Riesgos: 124 filas sin ubicación (todas Firecrawl) y 74 con "Remote"
  genérico — un chequeo temprano no las ve (región desconocida, neutra); si se
  usara la descripción saldrían falsos positivos (24 filas no marcadas
  mencionan emea/europe only/poland en su descripción).
- **Recomendación: no añadir chequeo de región al scraper.** Alternativas
  baratas, en orden: (1) aplicar la 2.1.0; (2) revisar si la página de n8n en
  `TARGET_COMPANY_PAGES` sigue valiendo 1 crédito/corrida con 75% de filas
  EU-only; (3) opcional: en `firecrawl_sources._pick_for_detail`, mandar al
  final de la cola los candidatos cuya ubicación de tarjeta ya case con
  `region.*` (~10 líneas, no cambia qué se inserta; valor bajo).
- No se implementó nada en `scraper.py` ni `firecrawl_sources.py`.

## 5. Hallazgo `cv-prompt.md` (sesión 2, solo hallazgo — NO implementado)
`cvgen.py` es determinista (selecciona/reordena con `python-docx`, no llama a
ningún LLM) y no lee `cv-prompt.md` en runtime. Estado de los 6 edits de B
frente al código actual:
| Edit | Ya existe en cvgen | Qué faltaría | Esfuerzo / riesgo |
|---|---|---|---|
| 1 Espejo de keywords (ATS) | Parcial: `skill_rank` ordena skills por match; `expand_acronyms` hace "Nombre (ACR)" | Garantizar top-5 keywords presentes en `profile.json` textuales en Core Skills y ≥1 viñeta (solo seleccionando viñetas con ese tag, nunca redactando) | Medio (~1 día con tests). Riesgo medio: cambia selección/orden de viñetas en todos los CVs nuevos |
| 2 Título espejo | No: titular fijo de la familia de rol (`fam["headline"]`) | Elegir entre titulares/cargos reales del perfil el más cercano al título de la vacante | Bajo–medio. Riesgo: cambia titulares; requiere variantes en `profile.json` con fuente |
| 3 Logros cuantificados arriba | No explícito | Priorizar viñetas cuantificadas (tag o regex de cifras) en las 2 primeras del rol más reciente | Bajo. Riesgo bajo |
| 4 Prohibido texto oculto | **Sí**: `validate_docx` lo comprueba en cada CV | Nada en código; solo texto del prompt | Nulo |
| 5 Corrección de etiqueta | N/A (texto) | Nada | Nulo |
| 6 Verificación de cobertura | Parcial: `coverage_report` ya registra cubierto/omitido/confirmar | Añadir (c) años no afirmados y marcar "pendiente de revisión" si (a) está vacío | Bajo |
Recomendación de la sesión 2 al coordinador: aplicar los edits a `cv-prompt.md`
como documentación cuando toque (territorio de B); si se cablea algo, empezar
por 3 y 6 (bajo riesgo); cablear 1 y 2 solo **después** de la Palanca 1 de B
(generar los ~85 CVs que faltan con el generador actual), o los CVs del backlog
saldrían con dos lógicas distintas y no se podría comparar nada con n≈30.
Ninguno de estos edits mueve el 14% (B §6).

## 6. Qué implementó Opus (archivos cambiados + tests)

### Sesión 1 — auditoría
| Archivo | Cambio |
|---|---|
| `scraper.py` | `classify_error` (rate_limit/blocked/proxy/other); éxito parcial (filas+error = ok, resetea breaker, registra `PARCIAL`, conserva la mejor tanda); breaker que se entreabre por 429 tras 300 s (`RATE_LIMIT_COOLDOWN_SECONDS`, enfriamiento que se duplica); una sola prueba directa por sitio y corrida para 403/proxy (`ProxyRoute`, registra `RUTA <sitio>: responde sin proxies`); 403/CONNECT rechazado no se reintenta con la misma ruta; estadísticas `partial` y `direct_sites` |
| `autoapply.py` | Dedupe por empresa+título en `build()` contra enviadas (`cvgen.SENT_STATES` + `cvgen.dedup_key`) y contra la cola (índice por empresa+título); grupo ya enviado se omite como "ya enviada" **sin consumir ventana**; comando `--reanudar vNNN --nota "<respuesta y fuente>"` + `resume()` (PREGUNTA→en_curso con historial con zona horaria, suma intento, log `REANUDADA`, respeta cupo, entrega con `respuesta_alex`; no envía nada ni toca `apply-config.json`); docstring |
| `run_daily.py` | `backup_before_rank()` (API `backup` de SQLite en modo solo lectura → `backups/vacantes-pre-rank-YYYYmmdd-HHMMSS.db`, rotación últimas 7; si falla, se registra y la corrida sigue); nombre del log diario con fecha `America/Mexico_City` |
| `monitor.py` | Contexto de madurez en el mensaje del umbral y en el JSON (`umbrales.*.maduras`); estado y umbrales sin cambio |
| `test_autoapply.py` | Reescrito: fixture determinista con filas sintéticas en la copia (empresas `ZZ Sandbox *`); casos nuevos de dedupe (H2) y `--reanudar` (H6); exit code ≠ 0 si algo falla (antes siempre 0) |
| `test_scraper.py` | **Nuevo**: 10 tests sin red, incl. replay de la secuencia del log del 20-nov (`Replay20261120`) |

### Sesión 2 — cableado reapunte
| Archivo | Cambio |
|---|---|
| `filter_rank.py` | Cableado seniority/región (§2.1) + agrupación del top del reporte por empresa+título (§3) |
| `test_filter_rank.py` | **Nuevo**: 16 tests (§2.3) |

### Tests — resultado final (verificado por el coordinador, `.venv/bin/python`, tras el último cambio)
| Suite | Resultado |
|---|---|
| `test_monitor.py` | **21/21 PASS** |
| `test_covergen` (unittest) | **29 tests OK** |
| `test_autoapply.py` | **24/24 PASS** (antes: KeyError `v495`) |
| `test_scraper` (unittest, nuevo) | **10 tests OK** |
| `test_filter_rank` (unittest, nuevo) | **16 tests OK** |
| `filter_rank.py --test-onsite` | **0/10 fallos** |
| doctests de `scraper.py` | **3/3 passed** |
| `run_daily.py` con parches en `/tmp` (sesión 1) | rc=0 (copia de DB; sin JobSpy, scraper simulado, Firecrawl forzado a excepción → la corrida sigue; filter-health, monitor, log y backup escritos en `/tmp`) |

### Verificaciones de integridad (coordinador)
- `git status`: solo cambian `autoapply.py`, `filter_rank.py`, `monitor.py`,
  `run_daily.py`, `scraper.py`, `test_autoapply.py` (modificados) y
  `test_filter_rank.py`, `test_scraper.py` (nuevos). `README.md` ya venía
  modificado antes de la auditoría (no lo tocó Opus). `covergen.py`,
  `test_covergen.py`, `docs/`, `sunkssd/` ya venían nuevos/sin seguimiento.
- **Ningún archivo protegido tocado**: `filter-config.json` (sigue 2.0.0,
  mtime 2026-11-16), `filter-changelog.md`, `cv-prompt.md`, `ranking-rules.md`,
  `apply-config.json`, `apply-queue.json`, `profile.json`, `proxy-config.json`,
  `cuentas-creadas.md`, `vacantes.db` (md5 `e74e8283aed90142b6b809dc8f81dfcd`
  sin cambio; mtime 2026-11-21 03:51 sin cambio).
- Diff revisado sin secretos (único match de patrón en el diff total: la frase
  benigna "token por token" en el `README.md` preexistente).
- Cero créditos Firecrawl gastados en pruebas; cero cuentas creadas; cero
  logins; **cero commits**.
- Nota: Opus intentó anotar convenciones en `~/AGENTS.md` en ambas sesiones y
  el sistema le negó el permiso de escritura; quedan en sus informes. Convenciones
  a anotar por el coordinador: (a) "filas + error 429" = éxito parcial, no fallo;
  (b) dedupe empresa+título en `build()`; (c) backup antes de re-rank con rotación 7;
  (d) log diario con fecha MX; (e) caps 2.1.0 cableados en `filter_rank.py`,
  config 2.0.0 = no-op.

## 7. Investigación: cómo batir los 403/429 (tabla con costes)

Precios en USD, verificados 2026-11-20 contra páginas de vendor / revisiones
de Oct–Nov 2026. "Cuenta" = crear cuenta con email; ningún servicio de esta
tabla se contrató ni se probó con datos de Alex (research only).

### 7.1 Diagnóstico (por qué falla JobSpy — coincide con los síntomas)
- LinkedIn usa el endpoint guest sin autenticar
  (`/jobs-guest/jobs/api/seeMoreJobPostings/search`): throttling por IP tras
  ~10–25 páginas → 429/999. ApplyPilot pone `linkedin_fetch_description=True`
  (`scraper.py:640,669`) → **N+1**: 100 jobs = 100+ GETs extra; quema la IP
  en menos de un minuto.
- Indeed/Glassdoor: Cloudflare Bot Management + fingerprint TLS/JA3 bloquean
  ASN de datacenter (DigitalOcean incluido). El `requests` plano de Python se
  fingerprinta al instante aunque los headers y la IP sean buenos.
- Indeed Publisher API pausada desde oct 2022: no hay vía oficial gratuita.
  Sponsored Jobs API restringida a clientes de pago.
- LinkedIn oficial: NO hay API pública de búsqueda de empleo. Job Posting API
  exige partnership Talent Solutions, y LinkedIn **no acepta nuevos
  partnerships** para esa API (redirige a Apply Connect). Sin vía self-serve.

### 7.2 Opciones gratuitas sin cuenta ni llave (coste $0, ~1–2 h de setup cada una)
| Opción | Qué es | Coste | Qué arregla | Esfuerzo | Cuenta/login/datos |
|---|---|---|---|---|---|
| RemoteOK API (`remoteok.com/api`) | JSON keyless, ~99 jobs más recientes, filtro `?tag=` | $0 | Fuente remota sin scraping; sustituye parte de Firecrawl | Bajo: 1 GET + normalizar | No. Términos: link back + mencionar fuente; elemento [0] es aviso legal; quitar la frase de tracking de las descripciones (contiene la IP del que lee, en base64) |
| Remotive API (`remotive.com/api/remote-jobs?search=&limit=100`) | JSON keyless con búsqueda en servidor | $0 | Fuente remota con filtro por keyword | Bajo | No |
| Arbeitnow (`arbeitnow.com/api/job-board-api`) | JSON keyless | $0 | Fuente remota (filtrar `remote=true`; el feed trae on-site) | Bajo | No |
| Jobicy (`jobicy.com/api/v2/remote-jobs`) | JSON documentado, hasta 200/pág, últimos 7 días | $0 | Fuente remota | Bajo | No; piden crédito + link |
| Himalayas (`himalayas.app/jobs/api?limit=100&offset=0`) | JSON keyless paginado, 100k+ listings | $0 | Fuente remota con volumen | Bajo | No |
| We Work Remotely RSS | RSS por categoría (sigue expuesto) | $0 | Fuente remota sin JS | Bajo | No |
| ATS públicas JSON (Greenhouse `boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true`; Lever `api.lever.co/v0/postings/{slug}?mode=json` (+ `api.eu.lever.co`); Ashby `api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true` — ¡salario estructurado!; Workable; SmartRecruiters; Recruitee; Workday POST sin auth) | El mismo JSON que alimenta la página de carrera de cada empresa | $0 | **Bypass total del anti-bot**: sin scraping, sin proxy, sin llave; ideal para lista de empresas objetivo | Medio: lista de slugs + 1 normalizador por ATS | No. 1 req por empresa cada pocas horas basta; User-Agent real |
| Indeed RSS (`rss.indeed.com/rss?q=..&l=..`, también `{país}.indeed.com/rss`) | RSS por búsqueda, sin auth; título/empresa/link/snippet (sin descripción completa) | $0 | Vía de escape a Indeed sin Cloudflare | Bajo | No. **Pero hay reportes de bloqueo actual** (un proyecto lo sustituyó por RemoteOK tras bloqueo). Probar y medir, no fiable |

### 7.3 APIs de empleo con free tier (cuenta + API key, sin tarjeta)
| Opción | Qué es | Coste | Qué arregla | Esfuerzo | Cuenta/login/datos |
|---|---|---|---|---|---|
| Adzuna API (`developer.adzuna.com`) | Agregador con API REST, 16 países, rangos salariales (`salary_is_predicted`) | $0 key al registrarse; límites 25/min, 250/día, 1.000/sem, 2.500/mes (algunas cuentas 250/mes) | Fuente agregada oficial, sin scraping | Bajo (~1 h) | Sí: cuenta + key (email). **OJO términos**: uso comercial = trial 14 días, luego puede exigir licencia; requiere atribución "Jobs by Adzuna"; descripciones son snippets; hay que borrar datos si termina el acuerdo. Cobertura más fuerte UK/Europa occidental — probar MX/US |
| JSearch (OpenWeb Ninja, vía RapidAPI o directo) | Envuelve Google for Jobs → Indeed/ZipRecruiter/Glassdoor/LinkedIn en un JSON, descripciones completas | Free 200 req/mes sin tarjeta; Pro $25/mes 10k req; Ultra $75/mes 50k; Mega $150/mes 200k; PAYG $0,005/req | **Backdoor a los 4 boards bloqueados a la vez** | Bajo (~1 h) | Sí: cuenta RapidAPI (email) + key. Salario en texto libre sin parsear; 10 jobs/pág → la paginación quema cuota rápido |
| SerpApi Google Jobs (`engine=google_jobs`) | Devuelve el panel de Google Jobs como JSON (LinkedIn, Indeed, Lever, Zip, Workday…) | Free 250 búsquedas/mes (50/h); Starter $25/mes 1.000; Developer $75/mes 5.000; Production $150/mes 15.000 | Google Jobs como fuente única incl. boards bloqueados | Bajo (~1 h) | Sí: cuenta + key (email). 10 resultados/pág; se paga por búsqueda, no por job |
| JobPostings API | JSON directo de 434 ATS (Greenhouse/Lever/Ashby/Workday…) | 300 créditos gratis al registrarse, sin tarjeta; Starter $19/mes 25.000 jobs ($0,76/1k); Scale $199/mes 1M ($0,199/1k). 1 crédito = 1 job; 0 créditos si vacío/404 | Sustituto de JobSpy sin proxies | Bajo (~1 h) | Sí: cuenta + key (email) |

### 7.4 Scraping APIs / unblockers (cuenta + key; free tier o trial sin tarjeta)
| Opción | Qué es | Coste | Qué arregla | Esfuerzo | Cuenta/login/datos |
|---|---|---|---|---|---|
| ZenRows | API de fetch con rotación de proxies, CAPTCHA y JS rendering | **Free 5.000 créditos/mes recurrente, sin tarjeta**; Build $19/mes 45k; Launch $69/mes 250k; Growth $199/mes 1,2M. 1 crédito plano, 5 JS, 10 premium proxy, 25 JS+premium (tope 25). Fallos gratis | **Mejor free tier recurrente para desbloquear Indeed/Glassdoor/LinkedIn** | Bajo (~30 min, 1 GET) | Sí: cuenta + key (email). Sin tarjeta en free |
| ScraperAPI | API de scraping con proxies rotativos y rendering | Free 1.000 créditos/mes + trial 7 días 5.000, sin tarjeta; Hobby $49/mes 100k. Plano 1, Amazon 5, Google 25, **LinkedIn 30**, +10 tras Cloudflare (hasta 75 rendered ultra-premium) | Desbloqueo genérico | Bajo (~30 min) | Sí: cuenta + key. OJO: LinkedIn a 30 créditos/req → 1.000 gratis ≈ 33 págs/mes |
| ScrapingBee | API de scraping (Oxylabs) | **Sin free plan**: 1.000 créditos una vez (trial), sin tarjeta; Hobby $19/mes 75k; Freelance $49/mes 250k; Startup $99/mes 1M. 1 crédito plano (JS off — por defecto ON = 5), premium 10/25, stealth 75. Créditos caducan mensualmente | Desbloqueo genérico | Bajo | Sí: cuenta + key |
| Zyte API | Fetch gestionado por éxito (equipo de Scrapy) | $5 crédito gratis, trial 30 días, sin suscripción; PAYG HTTP $0,13–$1,27/1.000 respuestas exitosas según dificultad; browser $1,01–$16,08/1.000; compromisos $100/$200/$500 bajan tarifa. **Solo se paga por éxito** | Desbloqueo por dificultad de sitio, barato | Medio (~1 h) | Sí: cuenta + key (email) |
| Bright Data Web Scraper API / Web Unlocker / SERP | Scrapers pre-construidos + unlocker, 1.200+ sitios | Free 5.000 records/mes (pool compartido), sin tarjeta; PAYG $1,50/1.000 records; Scale $499/mes 384k. Proxies residenciales $8/GB PAYG | Desbloqueo enterprise | Medio | Sí: cuenta + key. **BLOQUEO: desde 2026 el acceso residencial exige revisión KYC manual, empresa registrada, email laboral y depósito; tiers desde $499/mes/producto. Para uso individual, efectivamente vetado** |
| Oxylabs Web Scraper API | Scraper API + red de proxies | $5 crédito bienvenida; Starter $15/mes (algunas páginas listan $30/mes Web API — verificar); general $0,50–$1,00/1.000 resultados | Desbloqueo gestionado barato | Bajo–medio | Sí: cuenta + key |
| Decodo (ex-Smartproxy) Scraping API | Scraping API | Free plan, luego $19/$49/$99/mes; desde $0,15/1.000 req (más barato en tests Proxyway para pág. protegidas) | Desbloqueo barato | Bajo | Sí: cuenta + key. Verificar en su web |

### 7.5 Proxies (cuenta + pago; credenciales SOLO en `proxy-config.json`)
| Opción | Qué es | Coste | Qué arregla | Esfuerzo | Cuenta/login/datos |
|---|---|---|---|---|---|
| DataImpulse (**fallback ya conocido**) | Residenciales rotativos, 90M+ IPs | Residencial **$1/GB PAYG**, intro $5 (5 GB), tráfico **que no caduca**; datacenter $0,50/GB; móvil $2/GB; premium residencial $5/GB. Targeting estado/ciudad/ZIP/ASN a 2× | IP residencial real para Indeed/Glassdoor/LinkedIn | Bajo (~30 min: comprar intro, añadir endpoint a `proxy-config.json`) | Sí: cuenta + pago con tarjeta. Sin free trial. Nota: algunas IPs US testearon como no-residenciales; Trustpilot suspendido (disputa por reseñas falsas) |
| Webshare | Datacenter + residencial estático (ISP) + rotativo | **Free para siempre: 10 proxies datacenter + 1 GB/mes, SIN tarjeta, sin caducidad**. Datacenter desde $2,99/mes (100 proxies); **residencial estático desde $6/mes (20 proxies, $0,30 c/u)**; rotativo $3,50/GB (1 GB) → $1,40/GB a 3 TB | Free tier: test $0 de si datacenter pasa (probablemente no en Indeed/Glassdoor); residencial $6/mes = **entrada residencial más barata encontrada** | Bajo (~30 min) | Sí: cuenta (email). Free sin tarjeta |
| Setup actual: Squid en 2 droplets DO vía Tailscale | Proxies datacenter propios | Coste droplets (existente) | Nada contra Cloudflare: **el ASN de DigitalOcean es justo lo que Cloudflare marca** | — | — |

### 7.6 Arreglos a nivel de código (sin cuenta, sin coste)
| Opción | Qué es | Coste | Qué arregla | Esfuerzo | Cuenta/login/datos |
|---|---|---|---|---|---|
| `curl_cffi` | Cliente HTTP que imita el TLS/JA3 + HTTP/2 de Chrome real (`from curl_cffi import requests; requests.get(url, impersonate="chrome")`) | $0, MIT, v0.16.x, ~6,5k estrellas, mantenido (sep 2026). Usar ≥0.15.0 (parche CVE-2026-33752) | **403 por fingerprint TLS** donde headers+IP están bien. Cambio de 1 línea de import | Bajo: instalar + probar por sitio; funciona con proxies (incl. DataImpulse) | No. NO ejecuta JS → no pasa Turnstile. JobSpy usa `requests`/`tls-client` por dentro: parchear JobSpy es más difícil; mejor usar `curl_cffi` para fetches directos (ATS/RSS/descripciones) y JobSpy solo donde aún funciona (LinkedIn con description fetch OFF) |
| `jobdrop` (PyPI, MIT) | Fork de JobSpy (kbwhodat), 23 fuentes, anti-bot, Indeed GraphQL, Greenhouse/Ashby/Workday directos, servidor MCP | $0 | Sustituto directo de JobSpy con más fuentes y manejo anti-bot | Bajo: cambiar import, probar en copia | No |
| Higiene LinkedIn 429 (sin vendor) | Apagar `linkedin_fetch_description` (traer descripciones luego, solo para el shortlist 4★+, vía Firecrawl/`curl_cffi`); tope `results_wanted` ≤25–50; más pausa entre búsquedas; repartir búsquedas en el día; rotar proxy por búsqueda; 429 = enfriar horas, no reintentar en minutos | $0 | **Causa raíz del 429 de LinkedIn (N+1)** | Bajo | No |
| Playwright + stealth (`patchright`/`playwright-stealth`) | Navegador real | $0 (licencias) | Turnstile/JS challenges en pocas páginas de alto valor | Alto (RAM/CPU por request, lento) | No. Último recurso, no para scrape masivo |

### 7.7 Orden recomendado (para el coordinador)
1. **Código, gratis, hoy:** `linkedin_fetch_description=False` + descripciones solo
   para el shortlist; `curl_cffi` para fetches directos (ATS/RSS); 1 reintento
   máx. en 403/429 y circuit-break por horas (parte ya hecha en sesión 1).
2. **Feeds gratuitos, esta semana:** RemoteOK/Remotive/Arbeitnow/Jobicy/
   Himalayas/WWR-RSS + ATS públicas JSON para una lista de empresas objetivo —
   módulo nuevo estilo `firecrawl_sources.py`, sin llaves ni cuentas.
3. **Free tiers (decisión de Alex — cuenta+email+key):** ZenRows 5.000 créd./mes
   (mejor unblocker gratis) para probar Indeed/Glassdoor/LinkedIn; Adzuna key
   gratis (probar cobertura MX/US); JSearch free 200/mes (backdoor Google for
   Jobs a los 4 boards a la vez).
4. **Pago (decisión de Alex — $):** DataImpulse intro $5 (5 GB residencial, no
   caduca) como fallback de proxy; Webshare residencial estático $6/mes como
   test residencial más barato; Apify LinkedIn Jobs Scraper (`valig`,
   $0,28–$0,40/1.000 jobs; plan free $5 uso/mes) si LinkedIn debe seguir siendo
   fuente primaria.
5. **NO recomendado:** Bright Data residencial (KYC/empresa, $499/mes);
   LinkedIn API oficial (partnerships cerrados); ScrapingBee (sin free
   recurrente; stealth 75 créditos/req caro); ScraperAPI para LinkedIn
   (30 créditos/req).

## 8. Qué necesita decisión de Alex (opción exacta) y qué le toca al coordinador

### 8.1 Decisiones de Alex (no se hace nada sin su sí)
| # | Decisión | Opción exacta |
|---|---|---|
| A1 | Cuentas con su email para free tiers (ZenRows / Adzuna / JSearch / SerpApi) | ¿Autoriza crear cuenta con su email en ZenRows (5.000 créd./mes gratis, sin tarjeta), Adzuna (key gratis) y JSearch (200 req/mes gratis)? Cada una: solo email + API key. Sin tarjeta, sin datos personales más allá del email |
| A2 | Gasto en proxy residencial | ¿$5 intro DataImpulse (5 GB, no caduca) como fallback, o $6/mes Webshare residencial estático (20 IPs)? ¿O ninguno y se sigue solo con feeds gratis + ZenRows free? |
| A3 | Apify para LinkedIn (si LinkedIn debe seguir primario) | ¿Cuenta Apify (plan free $5 uso/mes) + actor `valig/linkedin-jobs-scraper` $0,28–$0,40/1.000 jobs? |
| A4 | 15 preguntas pendientes (varias de 1 línea; 2 por correo que solo él puede enviar: SUNKSSD v1197, Veracode v4210) | Responder en bloque. Nota: 6 de las 15 bajan a 3★ con la 2.1.0 (v1676, v3629, v1576, v1905, v1951, v254) — el coordinador debe decidir antes si siguen siendo aplicables (B §8.3 Q1), o no tiene sentido pedirle esas 6 respuestas |
| A5 | Umbrales del embudo 15/25 | ¿Evaluar el umbral sobre aplicaciones **maduras** (≥7 días) en vez de sobre el total? Hoy 0/24 son maduras; el CRÍTICO actual es prematuro (H4). Decisión de negocio, umbrales acordados con él |
| A6 | Veto al reapunte 2.1.0 | La 2.1.0 se reporta en el review diario (regla del changelog) y Alex puede vetarla. Efecto: 4★+ 237→167; 5★ en `nueva` 8→3 (quedan SUNKSSD, Samsara TAM México, Interfell) |

### 8.2 Pendiente del coordinador (sin Alex, en orden — checklist B §7 + auditoría)
1. [ ] **Cableado: HECHO (sesión 2, §2).** No falta nada de código para la 2.1.0.
2. [ ] **Backup:** copiar `vacantes.db` a `backups/` con marca pre-reapunte justo
   antes de aplicar (ya existe `backups/vacantes-2026-11-20-2203-pre-reapunte.db`
   de B; hacer uno nuevo por si la DB cambió; md5 actual
   `e74e8283aed90142b6b809dc8f81dfcd`).
3. [ ] **Config:** reemplazar `filter-config.json` por
   `docs/filter-config-2.1.0-proposed.json`; quitar la marca "PROPUESTA —
   pendiente…" de `updated_by` (poner quién aplica y la hora real).
4. [ ] **Changelog:** appendear la entrada del §3 de B a `filter-changelog.md`
   con fecha/hora reales y el nombre del agente de código.
5. [ ] **Re-rank real:** `filter_rank.py --all` contra la DB real. Esperado:
   5★ 7 · 4★ 160 · 3★ 631 · 2★ 756 · 1★ 764; 4★+ 167 (± filas nuevas desde el
   21-nov 03:51 y decaimiento de recencia).
6. [ ] **CV-prompt:** aplicar los Edits 1–6 del §5 de B a `cv-prompt.md`
   (solo texto; no cambian CVs generados hasta el cableado en `cvgen.py`).
7. [ ] **Cobertura CV (Palanca 1 de B, la mayor):** `cvgen.py --all
   --min-estrellas 4 --confirmo-todo` (tras el reapunte: ~85 CVs nuevos; sin
   reapunte serían 116). Cero créditos Firecrawl (local).
8. [ ] **Cola:** podar las 10 bloqueadas `vacante_cerrada` de
   `apply-queue.json`. No tocar las 15 pendientes (son de Alex).
9. [ ] **Cron de aplicación:** mover de 07:00 a ~09:30, tras scrape (07:51) +
   `cvgen --top N` antes de encolar (H5; lo decide quien maneja los crons).
10. [ ] **Perfiles bloqueados:** revisar ACL Squid para `glassdoor.*:443`
    (H3: el proxy rechaza el CONNECT); decidir si se sacan de
    `search-profiles.json` los 17 Indeed + 7 Glassdoor + 2 Zip (45% del plan)
    o se consigue proxy residencial (A2).
11. [ ] **Firecrawl:** redirigir créditos a `empresas` (páginas de carrera/ATS);
    no encolar WWR/GetOnBoard/Wellfound sin cuenta; revisar la página de n8n
    en `TARGET_COMPANY_PAGES` (75% filas EU-only, 1 crédito/corrida).
12. [ ] **Monitor:** mostrar vacantes distintas junto a `top_nueva_sin_cv` /
    `nueva_top_sin_tocar` (~5 líneas, `gkey`) — §3.
13. [ ] **Cupo diario:** decidir si las `en_curso` cuentan solo si se
    entregaron hoy (H9; AGENTS.md define el contador actual).
14. [ ] **CRM:** exportar el CRM **antes** del review diario (H4: el embudo
    del 20-nov se armó con confianza baja por llegar tarde).
15. [ ] **cv-prompt cableado:** si se cablea algo en `cvgen.py`, empezar por
    edits 3 y 6 (bajo riesgo); 1 y 2 solo después de generar los ~85 CVs
    pendientes (§5).
16. [ ] **AGENTS.md:** anotar las convenciones de la §6 (Opus no tuvo permiso
    de escritura): éxito parcial 429, dedupe empresa+título, backup pre-rank
    con rotación 7, log con fecha MX, caps 2.1.0 cableados (2.0.0 = no-op).

### 8.3 Preguntas abiertas de B para el coordinador (no para Alex)
1. Pendientes que bajan a 3★ (6 de 15): ¿siguen siendo aplicables cuando Alex
   responda, o quedan aparcadas? (ver A4).
2. v4185 Interfell (5★ restante): en cola como `bloqueada / sitio_bloqueado`.
   ¿Un reintento con el navegador o se deja?
3. Dedupe: respondido en §3 (no fusionar filas; métrica en monitor).
4. Región en scrapeo: respondido en §4 (no implementar).
