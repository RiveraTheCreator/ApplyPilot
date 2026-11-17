# ApplyPilot

**Piloto automático de búsqueda de empleo: una tool completa para agentes.**
Scrapea vacantes, las rankea, genera un CV a medida por vacante, aplica en
nombre del candidato, sigue el embudo en un CRM y se vigila sola para decir
dónde optimizar. **Objetivo del sistema: conseguir LA CHAMBA** — la métrica
que manda es entrevistas, no volumen de aplicaciones.

## Para agentes de IA (lee esto primero)

Si eres un agente (Muse, Claude, GPT u otro) y vas a operar o extender este
sistema, este es tu orden de lectura:

1. `DESIGN.md` — diseño general, módulos, reglas y fases.
2. `ranking-rules.md` — **autoridad sobre el ranking** (v2: único descarte =
   100% presencial; años de experiencia = criterio de menor prioridad, nunca
   filtran).
3. `cv-prompt.md` — reglas de generación de CVs (cero mentiras, sin texto
   oculto, ATOP, `.docx`).
4. `keywords.json` — estrategia: 11 aristas, 78 keywords.
5. `filter-config.json` — pesos, umbrales y alertas (versionado; cambios en
   `filter-changelog.md`).
6. Este README — manual de operación por fase.

### Mapa de módulos

| Módulo | Archivo | Qué hace |
|---|---|---|
| Scraper | `scraper.py` + `firecrawl_sources.py` | JobSpy (LinkedIn, Indeed, Glassdoor, ZipRecruiter) + Firecrawl (Wellfound, Remote OK); reintentos, circuit breaker, proxies opcionales |
| Ranker v2 | `filter_rank.py` + `filter_health.py` | Score 0–100 + estrellas 1–5; autovigilancia del filtro (`logs/filter-health-YYYY-MM-DD.json`) |
| CVs | `cvgen.py` | Un `.docx` a medida por vacante desde `profile.json`; se autovalida y se borra si falla una regla |
| Auto-apply | `autoapply.py` | Prepara y registra aplicaciones; el envío lo hace el operador con el navegador; cupo 10/día |
| Monitor | `monitor.py` | Vigila el pipeline completo, cierra el ciclo del CRM y dice dónde optimizar el embudo |
| Orquestador | `run_daily.py` | Corrida diaria: scrapeo → ranker → monitor |

### Configuración (empieza por los ejemplos)

```bash
cp profile.example.json profile.json          # TUS hechos verificados (nunca se sube)
cp apply-config.example.json apply-config.json  # límites + respuestas conocidas con fuente
cp proxy-config.example.json proxy-config.json  # solo si usas proxies (NUNCA se sube)
```

### Reglas no negociables

- **Cero mentiras en los CVs:** los hechos viven solo en `profile.json`;
  `cvgen.py` selecciona y reordena, nunca redacta hechos. Lo que el candidato
  no tiene se omite y se compensa con habilidad transferible.
- **Sin texto oculto ni instrucciones para IAs** en ningún documento generado.
- **Nunca se inventan sueldos, años de experiencia ni datos sensibles.**
  Lo desconocido va a `confirmar` y detiene la aplicación.
- **Sin credenciales en código, logs o archivos.** Firecrawl solo vía su
  helper MCP (nunca se lee ni se guarda su API key). Los logins del
  candidato solo los usa el operador con el flujo de login guardado.
- **Aplicar solo con autorización del candidato** y dentro del cupo diario.
- Los pesos y umbrales viven en `filter-config.json`, no en el código; todo
  cambio se anota en `filter-changelog.md` y se reporta en el review diario.
- Antes de re-rankear o tocar `vacantes.db`, copiarla a `backups/`.

### Objetivo original (caso de uso de referencia)

Rol 100% remoto, $35,000–$40,000 MXN/mes, contratación desde México.
Estrategia y plan de búsqueda en `search-profiles.json` (59 perfiles JobSpy).

**`ranking-rules.md` es la autoridad sobre el ranking.** Si este README o el
código la contradicen, manda `ranking-rules.md`. La estrategia (11 aristas,
78 keywords, filtros) vive en `keywords.json`; el plan de búsqueda principal
del scraper vive en `search-profiles.json` (59 perfiles JobSpy: 20 base de México +
39 regionales LATAM / US / CA; spec en `scraper-config-spec.md`); pesos, umbrales
de estrellas, on/off de keywords y países del ranker viven en `filter-config.json`
(cambios anotados en `filter-changelog.md`); el diseño general en `DESIGN.md`.

## Alcance de la Fase 1

| Incluye | No incluye |
|---|---|
| `scraper.py` — JobSpy sobre LinkedIn, Indeed, Glassdoor, ZipRecruiter (plan por perfiles en `search-profiles.json`, con reintentos, circuit breaker, rotación de user-agent y proxies opcionales) | Firecrawl (ningún import ni llamada en la Fase 1) |
| `filter_rank.py` — Ranker v2: único descarte 100% presencial + score 0–100 + estrellas 1–5; `filter_health.py` — autovigilancia | Generación de CV (Fase 2, prompt en `cv-prompt.md`) |
| `run_daily.py` — corrida diaria con log | Auto-apply (Fase 3): este código no aplica a nada ni envía mensajes |
| `db.py` + `schema.sql` — SQLite `vacantes.db` | Credenciales o API keys: no se usan |

Los sueldos **nunca se inventan**: si la fuente no trae moneda y periodo claros,
`sueldo_min_mxn`/`sueldo_max_mxn` quedan en NULL y el reporte muestra `[confirmar]`.

## Instalación

Python 3.12. Dependencias: stdlib + `python-jobspy` (que trae `pandas`).

```bash
cd ~/workspace/empleo-pipeline

# Opción A (recomendada; en Ubuntu/Debian pip --user está bloqueado por PEP 668):
python3 -m venv .venv
.venv/bin/pip install -q python-jobspy

# Opción B (si tu Python permite instalar a nivel usuario):
pip install --user -q python-jobspy
```

`run_daily.py` se arregla solo: si `jobspy` no se puede importar, se re-ejecuta
con `.venv/bin/python` si existe, y si no, intenta `pip install --user -q python-jobspy`.
Para `scraper.py` suelto usa `.venv/bin/python scraper.py ...` cuando JobSpy solo
esté en el venv.

Verificar imports:

```bash
python3 -c 'import scraper, filter_rank, run_daily; print("imports ok")'
```

## Comandos

```bash
# Prueba pequeña: 3 perfiles (Indeed, LinkedIn, Glassdoor), 5 resultados, base aparte
.venv/bin/python scraper.py --profiles 1_F1_AIAutomation_indeed,9_F1_AIAgents_linkedin,16_F1_MarketingAutomation_glassdoor --limit 5 --db prueba.db
python3 filter_rank.py --db prueba.db --all

# Prueba pequeña con el plan fallback por keywords (2 aristas, 2 keywords cada una)
.venv/bin/python scraper.py --no-profiles --aristas 1,6 --max-keywords-per-arista 2 --limit 5 --db prueba.db

# Corrida diaria completa: los 59 perfiles de search-profiles.json (25/50 por perfil)
python3 run_daily.py

# Variantes
python3 run_daily.py --limit 10
python3 run_daily.py --profiles 12_F3_HospitalitySaaS_linkedin --db vacantes.db
python3 run_daily.py --no-profiles --aristas 1,2,3 --limit 10 --db vacantes.db

# Re-rankear todo lo que hay en la base (no solo estado='nueva'); --top N recorta la lista
python3 filter_rank.py --all --top 20

# Probar el detector presencial (10 ejemplos: 5 presenciales, 5 remotos/híbridos)
python3 filter_rank.py --test-onsite

# Resumen legible del último logs/filter-health-YYYY-MM-DD.json (alertas)
python3 filter_health.py

# Añadir perfiles regionales que falten (idempotente; no regenera el archivo)
python3 build_regional_profiles.py --dry-run
```

Flags:

- `scraper.py`: `--aristas 1,3,5` · `--limit N` (default: `results_wanted` de cada perfil, 25/50; 25 en el plan por keywords) · `--db vacantes.db` · `--keywords-file keywords.json` · `--max-keywords-per-arista N` (solo plan por keywords) · `--profiles-file search-profiles.json` · `--no-profiles` (fuerza el plan por keywords) · `--profiles nombre1,nombre2` (depurar perfiles concretos)
- `filter_rank.py`: `--db` · `--keywords-file` · `--config filter-config.json` · `--all` · `--top N` · `--no-health` · `--test-onsite`
- `run_daily.py`: `--aristas` · `--limit` · `--db` · `--profiles-file` · `--no-profiles` · `--profiles`

`--max-keywords-per-arista` es **solo un override manual de prueba** y solo
afecta al plan fallback por keywords. El default, y lo que siempre usa
`run_daily.py`, es el plan por perfiles. En el plan fallback por keywords se
usan todas las keywords de todas las aristas, sin recortes. (Una keyword
repetida en dos aristas, hoy "Technical Account Manager" en 3 y 11, se busca
una sola vez y se atribuye a ambas: 78 keywords = 77 búsquedas únicas.)

## Perfiles de búsqueda (`search-profiles.json`)

El plan principal del scraper: 59 perfiles (Indeed 19, LinkedIn 29, Glassdoor 9,
ZipRecruiter 2). Los 20 originales (México) los genera `build_search_profiles.py`;
los 39 regionales (CO, AR, BR, CL, PE, EC, UY, PY, BO, GT, CR, PA, DO, US, CA) los
**añade** `build_regional_profiles.py` leyendo `filter-config.json › countries`,
siempre con filtro remoto: `is_remote: true` en Indeed / Glassdoor / ZipRecruiter
y la palabra "remote" en el `search_term` de LinkedIn (el scraper quita
`is_remote` en LinkedIn porque JobSpy lo ignora ahí). Indeed solo en países con
dominio Indeed (no PY, BO, GT, DO → solo LinkedIn); Glassdoor solo AR, BR, US, CA
(+ MX); ZipRecruiter solo US/CA. `build_search_profiles.py` se niega a
sobrescribir el archivo si ya tiene los regionales (usar `--force` y luego
`build_regional_profiles.py`). Cada perfil es una
búsqueda en un sitio con sus propios parámetros (`search_term`, `location`,
`results_wanted`, `country_indeed`, `is_remote`, `easy_apply`). Antes de
usarlos, `load_search_profiles()` los valida contra las reglas fatales de
`scraper-config-spec.md`: sin `hours_old` en Indeed cuando hay `is_remote` o
`job_type`; sin `hours_old` en LinkedIn cuando `easy_apply` es true;
`results_wanted` ≤ 100 en LinkedIn; sin booleanos fuera de Indeed. Lo
corregible se corrige con un WARNING en el log; lo que no, descarta el perfil
con un ERROR. Las aristas de cada perfil se derivan con el matching de
keywords existente sobre su `search_term` (p. ej. el perfil de Hospitality
SaaS se atribuye a la arista 6). Si el archivo falta, es ilegible o no deja
ningún perfil válido, el scraper cae al plan por keywords y lo deja en el log.

## Resiliencia (anti-bugs)

- **Secuencial, sin ráfagas.** Las búsquedas van de una en una (los 429 de
  LinkedIn venían de lanzar los 4 sitios en paralelo por keyword), con pausa
  con jitter entre búsquedas (base 2 s, ×0.5–1.5) y pausa extra al cambiar de
  sitio (base 5 s, mismo jitter). Tradeoff: la corrida completa por perfiles
  son solo 20 búsquedas secuenciales (minutos); el plan fallback por keywords
  (77 términos × 4 sitios) es más lento que antes, pero correcto.
- **Reintentos con backoff.** Cada búsqueda fallida se reintenta: 1 intento +
  hasta 3 reintentos, esperas ~10 s → 30 s → 90 s con jitter ×0.7–1.3. El
  error puede venir como excepción de JobSpy o en `df.attrs["errors"]`; ambos
  se tratan igual.
- **Circuit breaker por sitio.** Tras 5 fallos consecutivos el sitio se marca
  muerto por el resto de la corrida: un solo log ERROR con la causa, y sus
  búsquedas restantes se omiten sin volver a llamar a JobSpy (se cuentan como
  `skipped` y se resumen en una línea al final).
- **Rotación de user-agent.** JobSpy sí expone el parámetro `user_agent` y
  este código rota 4 UAs reales de navegadores de escritorio (Chrome Windows /
  macOS, Firefox Windows, Safari macOS), uno distinto por llamada. La rotación
  la hace este módulo; JobSpy solo aplica el UA que recibe.
- **Degradación elegante.** Un sitio que falla —o un fallo al guardar en la
  base— nunca tumba la corrida: queda en el log con su causa y la corrida
  sigue. Exit code 0 aunque falle una fuente; distinto de 0 solo si ninguna
  búsqueda respondió en ningún sitio.

## Proxies (opcional)

Si hay proxies, se leen de la variable de entorno `JOBSPY_PROXIES` (URLs
separadas por coma) o, si no está, de `proxy-config.json` junto a `scraper.py`
(lista JSON o `{"proxies": [...]}`); el formato de ejemplo está en
`proxy-config.example.json`. Sin proxy configurado el sistema funciona igual.
Los valores nunca se escriben en el log (solo cantidad y origen) y se redactan
de cualquier mensaje de error. **Nunca subas ni compartas un
`proxy-config.json` con proxies reales: contiene credenciales.**

### Proxies de Alex (2 droplets DigitalOcean, Squid)

- Droplet 2 (100.89.205.9) y droplet 1 (100.107.188.5), ambos con Squid en el
  puerto 3128, autenticación básica (usuario `proxy`) y allowlist SOLO de
  `.indeed.com`, `.linkedin.com`, `.licdn.com`, `.glassdoor.com` y
  `.ziprecruiter.com`; origen permitido solo tailnet `100.64.0.0/10` y
  localhost. `proxy-config.json` ya apunta a los túneles locales con rotación.
- La VM no llega directo a los droplets: se usan túneles SSH locales vía
  Tailscale (ver `proxy-tunnel.sh`):
  `localhost:13128 -> droplet2:3128` y `localhost:13129 -> droplet1:3128`.
  Comandos: `./proxy-tunnel.sh start|stop|status` (start es idempotente).
- Propuesta para el cron diario (`empleo-scrapeo-diario`): correr
  `./proxy-tunnel.sh start` antes de `run_daily.py` (y `status` para
  verificar). Sin los túneles arriba, el scraper ignora los proxies
  (falla la conexión y sigue con el resto de fuentes).
- Resultado de la prueba 16 nov 2026 (1 keyword, 4 sitios, limit 3, por túnel):
  LinkedIn FUNCIONA por ambos droplets (3/3); Indeed, Glassdoor y
  ZipRecruiter devuelven 403 aunque el túnel Squid funciona (bloquean la IP
  de centro de datos de DigitalOcean). Sin proxy residencial no hay forma de
  forzar esos tres sitios; el módulo Firecrawl es el respaldo para ellos.

Cron diario sugerido (08:00):

```cron
0 8 * * * cd $HOME/workspace/empleo-pipeline && /usr/bin/python3 run_daily.py >> logs/cron.out 2>&1
```

### Qué hace cada corrida

1. Ejecuta los perfiles de `search-profiles.json`, una búsqueda a la vez, con
   pausas con jitter (ver Resiliencia). Si el archivo de perfiles no se puede
   usar, cae al plan por keywords: cada keyword en los 4 sitios con
   `location='Mexico'`, `hours_old=168` (7 días), `country_indeed='Mexico'`,
   `linkedin_fetch_description=True`.
2. Si un sitio falla (bloqueo 429/403, caída de red, curl 56) se reintenta con
   backoff (ver Resiliencia) y, si acumula 5 fallos consecutivos, el circuit
   breaker lo da por muerto el resto de la corrida. Cada fallo queda en el log
   con su causa (`FALLO sitio | perfil | intento | error`) y la corrida
   continúa. Código de salida 0 aunque falle una fuente;
   distinto de 0 solo si no respondió ninguna búsqueda en ninguna fuente.
3. Inserta con `INSERT OR IGNORE` sobre `UNIQUE(empresa, titulo, url)`: re-ejecutar no
   duplica filas. Si una vacante ya existente reaparece por otra arista, solo se
   amplía su lista de aristas.
4. Corre el Ranker v2 sobre `estado='nueva'`, escribe `score`, `estrellas`,
   `modalidad_v2` y `pasa_filtro`, y escribe `logs/filter-health-YYYY-MM-DD.json`.
5. Log en `logs/YYYY-MM-DD.log` (se anexa si corres varias veces el mismo día).

## Cómo agregar una arista o keywords

Edita `keywords.json` y agrega un objeto a `aristas`:

```json
{"id": 12, "name": "Nombre de la arista", "priority": "high",
 "difficulty": "Moderado", "keywords": ["Keyword uno", "Keyword dos"],
 "sources": ["LinkedIn", "Indeed"]}
```

- `id` único. `priority` igual a `main` o que empiece por `high` suma +5 al score.
- Para agregar keywords a una arista existente, añádelas a su lista `keywords`.
- No hay que tocar código: la siguiente corrida las usa todas.
- En `filters` se usan `pay_target_mxn` (objetivo de sueldo) y `fast_process_signals`
  (se suman a las señales de proceso rápido). `remote_required` y `exclude` quedan como
  descripción de la estrategia: el único descarte real es 100% presencial (Ranker v2).
- Para apagar una keyword sin borrarla: `filter-config.json › keywords.disabled` (+ changelog).

## Ranker v2 — estrellas y único descarte (2026-11-16)

Autoridad: `ranking-rules.md` v2. Configuración: `filter-config.json` (los pesos ya
no están en el código).

**Único descarte (`pasa_filtro = 0`): 100% presencial** — señal de trabajo en sitio
y ninguna de remoto/híbrido, o una señal fuerte ("100% presencial", "fully on-site",
"Modalidad: presencial", "this is an on-site role", "no es remoto"). El detector
(`classify_modality` en `filter_rank.py`, resultado en la columna `modalidad_v2`):

- convierte negaciones en señal remota: "no presencial", "no requiere asistir de
  forma presencial", "sin traslados a oficina", "not an on-site role", "100% on-site: no";
- ignora menciones que no describen la modalidad: entrevistas / eventos / offsites /
  inducción / visitas ocasionales en persona, "onsite teams", "en sitio web",
  "Nescafé en oficinas" (oficina solo cuenta con verbo de trabajo), y jerga técnica
  ("soporte remoto", "software de asistencia remota", "remote desktop");
- detecta híbridos: "70% presencialidad", "50% of their time in-office",
  "3 days on-site", "presencial con algunos días de home office", "remote or on-site";
- la etiqueta de Indeed "Tipo/Lugar de trabajo: empleo presencial" es señal normal,
  no fuerte (aparece también en vacantes remotas).

Todo lo demás **se queda y baja estrellas**: híbrido, modalidad no especificada,
visa / relocación, sueldo bajo o desconocido. **No hay filtro por años de
experiencia ni por título "senior".**

### Estrellas (1–5, columna `estrellas`; NULL = descartada)

| Modalidad | Encaje fuerte (≥20 pts kw) | Bueno (≥10) | Débil (≥5) | Sin keywords |
|---|---|---|---|---|
| remoto | 4 → **5** si sueldo ≥ objetivo, proceso rápido o publicación < 7 días | 4 | 3 | 2 |
| híbrido / no especificada | 3 | 3 | 2 | 1 |

Ajustes después de la tabla: sueldo ligeramente abajo (≥ 85% del mínimo) → máx 4★;
bajo (60–85%) → máx 3★; claramente abajo (< 60%) → máx 2★ (en no-remotas, además −1);
visa / relocación → máx 2★ en remotas, −1 en no-remotas. Mínimo 1★. Orden de la
lista: estrellas desc, luego score desc. Umbrales en `filter-config.json › stars`.

## Cómo funciona el score (0–100)

| Componente | Puntos | Regla |
|---|---|---|
| Alineación con aristas | hasta 40 | 10 por keyword en el título, 5 si solo está en la descripción (el título cuenta doble); keywords en `keywords.disabled` no cuentan |
| Sueldo | hasta 8 | 8 si mínimo o máximo ≥ $35,000 MXN/mes; **4 si es desconocido (neutro, nunca 0, nunca inventado)**; 3 ligeramente abajo; 2 si solo hay un mínimo por debajo; 0 bajo |
| Publicación < 7 días | 10 | 5 si la fuente no dio fecha |
| Proceso rápido | 10 | "Easy Apply", "contratación inmediata", "immediate hiring", "proceso corto", "respuesta rápida"… |
| Boost hotelería (arista 6) | 10 | keyword de la arista 6 o término del sector (hotel, hospitality, PMS, travel tech…) en el texto |
| Prioridad de arista | 5 | alguna arista de la vacante es `main`, `high` o `high-sector` |
| Experiencia (señal débil) | hasta 5 | 5 si piden ≤ 3 años, 3 si no dicen, 1 si piden 4–5, 0 si piden más |

Máximo teórico 88, tope en 100. **Los años de experiencia son la señal más débil:**
como mucho mueven 5 de 100 puntos, nunca restan y nunca filtran.

## Autovigilancia del filtro (`logs/filter-health-YYYY-MM-DD.json`)

Cada evaluación (`run_daily.py` o `filter_rank.py`) escribe el JSON del día (la
última corrida del día sobrescribe). Contiene: `totals` (evaluadas, pasan, tasa de
pase, conteo por estrella, por modalidad), `drop_reasons`, `by_source` (evaluadas,
pasan, tasa, nuevas hoy), `by_keyword` (vacantes por keyword, nuevas hoy, si está
apagada), `by_country` (país derivado de `ubicacion`), `checks` (cada regla evaluada:
`ok` / `alert` / `sin_historial`) y `alerts` (las disparadas, con `probable_cause` y
`suggested_action`).

Reglas de alerta (umbrales en `filter-config.json › health`):

| Check | Dispara cuando |
|---|---|
| `pass_rate` | tasa de pase < 60% (demasiado estricto) o > 97% (demasiado laxo) |
| `stars_distribution` | > 30% de las que pasan son 5★, o ninguna con 4–5★ |
| `source_zero:<fuente>` | una fuente del plan no trajo ninguna vacante nueva hoy |
| `source_pass_zero:<fuente>` | una fuente con ≥ 5 evaluadas tiene 0 que pasan |
| `keyword_zero_streak` | una keyword con 0 vacantes en los últimos 3 días con scrapeo |
| `drop_reason_spike` | un motivo de descarte hoy > 2× su promedio (y +10 pts) con ≥ 5 casos |
| `new_rows_drop` | vacantes nuevas hoy < 50% del promedio de días anteriores |

Sin historial suficiente la regla queda en `sin_historial` (se evalúa, no se inventa).

### Cómo se corrige (Kavi / agentes) y cómo se reporta

1. El review diario lee el JSON del día (`python3 filter_health.py` da el resumen) y
   reporta las `alerts`.
2. Si una alerta justifica un cambio, se edita **solo** `filter-config.json`
   (pesos, `stars.*`, `keywords.disabled`, `countries`, `health.*`); se sube
   `version` y `updated`/`updated_by`.
3. Se anota en `filter-changelog.md`: qué cambió (antes → después), por qué (qué
   alerta), cuándo, quién, y el efecto medido tras `python3 filter_rank.py --all`.
4. El review diario incluye una línea "Cambio de filtro: …" para que Alex lo vea y
   lo pueda vetar. Nada de cambios silenciosos. Cambiar el único descarte
   (`hard_filter`) o el detector presencial requiere orden de Alex.

## Base de datos (`vacantes.db`, tabla `vacantes`)

| Columna | Tipo | Notas |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `titulo` | TEXT NOT NULL | |
| `empresa` | TEXT | `''` si falta (nunca NULL, para que el UNIQUE deduplique) |
| `ubicacion` | TEXT | |
| `modalidad` | TEXT | `remoto` / `hibrido` / `presencial` / `no especificada` |
| `sueldo` | TEXT | texto crudo de la fuente, p. ej. `MXN 35000-40000 monthly` |
| `sueldo_min_mxn`, `sueldo_max_mxn` | REAL | MXN/mes parseado; NULL = desconocido |
| `url` | TEXT | `''` si falta |
| `fecha_publicacion` | TEXT | `YYYY-MM-DD` |
| `fuente` | TEXT | `linkedin` / `indeed` / `glassdoor` / `zip_recruiter` |
| `aristas` | TEXT | JSON con ids de arista: las que matchean por texto + la de la búsqueda que la encontró |
| `keywords_matched` | TEXT | JSON con las keywords presentes en título + descripción |
| `descripcion` | TEXT | truncada a 8,000 caracteres |
| `estado` | TEXT | default `'nueva'` (el CRM lo moverá a aplicada, entrevista…) |
| `score` | REAL | 0–100 |
| `estrellas` | INTEGER | 1–5 (Ranker v2); NULL = descartada (100% presencial) o sin evaluar |
| `modalidad_v2` | TEXT | `remoto` / `hibrido` / `no_especificada` / `presencial` según el detector v2 |
| `pasa_filtro` | INTEGER | NULL sin evaluar, 1 pasa, 0 descartada |
| `fecha_scrapeo` | TEXT | ISO 8601 UTC |

`UNIQUE(empresa, titulo, url)`; índices en `estado`, `fuente`, `fecha_scrapeo`.

Conversión de sueldo a MXN/mes: USD × 18.5; anual ÷ 12; por hora × 160;
semanal × 52/12; diario × 260/12. Un `$` sin moneda explícita, o sin periodo,
se deja en NULL.

Consultas útiles:

```bash
sqlite3 vacantes.db "SELECT fuente, COUNT(*) FROM vacantes GROUP BY fuente;"
sqlite3 vacantes.db "SELECT estrellas, score, titulo, empresa, url FROM vacantes WHERE pasa_filtro=1 ORDER BY estrellas DESC, score DESC LIMIT 20;"
```

## Fase 2 — CVs por vacante (`cvgen.py`, 2026-11-16)

Genera **un .docx a medida por vacante** en `~/workspace/cv/cv-{empresa}-{titulo}.docx`,
sin mentir. Dependencia: `python-docx` 1.1.0, **instalado en `.venv`** (también está en
el python3 del sistema). No usa pdflatex.

**Por qué no puede inventar:** todos los hechos viven en `profile.json` (viñetas ES/EN
ya redactadas con ATOP y verificadas contra `cv-template-base.tex` + `build_cv_alianza.py`).
`cvgen.py` no redacta hechos: detecta idioma y familia de rol de la vacante (titular y
perfil verdaderos por familia), elige y reordena viñetas, proyectos y habilidades por
match, expande acrónimos ("Nombre completo (ACR)") y renderiza (Calibri 11, márgenes 2 cm,
una columna, sin tablas). Lo que la vacante pide y Alex no tiene (`not_possessed`:
Salesforce, Terraform, TypeScript, Microsoft 365...) **se omite** y se compensa con la
habilidad transferible indicada. Lo no verificado no entra al documento: va a
`confirmar` en la cobertura. Para añadir un logro: edítalo en `profile.json` (con fuente).

```bash
.venv/bin/python cvgen.py --top 5                 # default: 5 mejores pendientes (>= 4★), dedup (empresa, título)
.venv/bin/python cvgen.py --vacante-id 200        # una vacante (repetible)
.venv/bin/python cvgen.py --vacante-id 200 --force    # regenerar (v2...); NUNCA si ya se envió
.venv/bin/python cvgen.py --vacante-id 200 --dry-run  # genera en /tmp, no registra nada
.venv/bin/python cvgen.py --all --confirmo-todo   # lote completo: solo con confirmación del operador
.venv/bin/python cvgen.py --min-estrellas 3 --top 10  # bajar el corte de estrellas
.venv/bin/python cvgen.py --validate ~/workspace/cv/cv-x.docx   # revisa reglas duras de un .docx
```

- **Idempotente:** tabla `cv_generaciones` en `vacantes.db` (estado `generado` / `duplicado` /
  `pendiente_descripcion` / `error`). Sin `--force` no regenera; con `--force` tampoco toca
  vacantes en estado aplicada o superior. Los duplicados (misma empresa normalizada + título,
  p. ej. LinkedIn + Indeed) comparten el mismo CV y quedan como `duplicado`.
- **Sin descripción:** intenta `firecrawl_scrape` vía el helper (nunca lee la API key); si no
  hay texto, queda `pendiente_descripcion` sin CV (reintenta al día siguiente). Firecrawl
  **no soporta LinkedIn** (probado 2026-11-16), así que las vacantes de LinkedIn sin descripción
  (100 de 118) quedan pendientes hasta que el scraper traiga la descripción.
- **Validación en cada CV** (si falla, el archivo se borra y queda `error`): abre con python-docx,
  0 tablas, sin columnas ni bordes, `e-mail:` y `LinkedIn:`, Core Skills al final, sin emojis,
  sin "Objetivo"/"Referencias", sin pronombres ni pasivas, sin afirmaciones prohibidas
  (`forbidden_claims`), **sin texto oculto/blanco/diminuto ni instrucciones para IAs**.
- **Salidas:** `logs/cv-coverage-YYYY-MM-DD.json` (keywords cubiertas / no cubiertas, requisitos
  omitidos + habilidad transferible, requisitos de años no afirmados, `confirmar`) y
  `logs/cv-crm-sync-YYYY-MM-DD.json` (por vacante: externalKey=url, `updateVacancy` con
  cvVersion / cvFile / nextStep y datos para `uploadCv`). Claude Code no tiene acceso al
  artefacto crm-empleo: el agente principal aplica ese archivo.

## Fase 3 — auto-apply (`autoapply.py`, 2026-11-16)

Autorizado por Alex el 2026-11-15 18:48: aplicar directo en su nombre y actualizar el CRM.
El módulo **prepara y registra**; el **envío lo hace el operador** (agente principal) con su
tarea de navegador y la sesión guardada de LinkedIn/Indeed. `autoapply.py` no abre navegadores,
no hace login y no lee ni guarda contraseñas (en ningún archivo, log o variable de entorno).

```bash
.venv/bin/python autoapply.py --dry-run               # top 5: cola simulada + valida cada CV; no escribe nada
.venv/bin/python autoapply.py --limit 5               # encola las 5 mejores vacantes distintas (no envía)
.venv/bin/python autoapply.py --vacante-id 200        # encola una sola vacante
.venv/bin/python autoapply.py --run-queue [--limit N] [--vacante-id N]  # entrega tareas al operador
.venv/bin/python autoapply.py --report resultado.json # registra el resultado ('-' = stdin; objeto o lista)
.venv/bin/python autoapply.py --status                # cola + cupo diario
.venv/bin/python autoapply.py --vacante-id 200 --reintentar   # re-encola una bloqueada/fallida
.venv/bin/python test_autoapply.py                    # pruebas en sandbox (copia de la DB; no toca nada real)
```

**Selección.** `pasa_filtro=1`, `estado='nueva'`, orden estrellas DESC, score DESC (igual que
`cvgen.py`; `--orden score` para score puro). Dedup por empresa+título: LinkedIn + Indeed de la
misma vacante = **una** aplicación (la otra URL queda como `alternativas` del paquete y, al aplicar,
ambas filas pasan a `aplicada`). `--limit N` = ventana de las N mejores vacantes distintas **no
aplicadas ni en cola**: las que no tienen CV de Fase 2 se reportan como pendientes
(`cvgen.py --vacante-id N`) y **no** se sustituyen por vacantes de menor rango.

**Paquete** (en `apply-queue.json › entries.vNNN.paquete`): url, plataforma/tier, canal
(formulario | correo), ruta absoluta del .docx, cvVersion, alternativas, datos del candidato
**leídos de `profile.json`** (nombre, e-mail, teléfono, LinkedIn, GitHub, ubicación, inglés B2+),
`respuestas_conocidas` y `solo_alex` de `apply-config.json` (cada respuesta con su fuente).

**Interfaz con el operador.** `--run-queue` marca las tareas `en_curso`, respeta el cupo diario
(default 10) y devuelve JSON (también en `logs/apply-tasks-*.json`) con, por tarea:
`delay_before_s` (60–180 s aleatorio, 0 en la primera), `paquete`, `instrucciones_navegador`
(texto listo para la tarea de navegador, con todas las reglas) y `reporte_plantilla`. El operador
reporta con `--report` un JSON:

```json
{"queue_id": "v200", "outcome": "aplicada", "url_usada": "https://...",
 "confirmation_text": "Tu postulación se ha enviado", "folio": null,
 "pregunta": null, "motivo": null, "detalle": "...", "cv_subido": "cv-....docx",
 "observado": "2026-11-16T10:05:00-06:00"}
```

| outcome | exige | efecto |
|---|---|---|
| `aplicada` | `confirmation_text` literal | cola `aplicada`; `vacantes.estado='aplicada'` (grupo; backup previo en `backups/`); log; CRM: status aplicada, appliedDate, cvVersion, cvFile, nextStep "Esperar respuesta de <empresa>; revisar Indeed/LinkedIn y correo", uploadCv |
| `pendiente-respuesta-Alex` | `pregunta` exacta | no se envió; CRM status nueva, notes con la pregunta, nextStep "Pregunta para Alex: <pregunta>" |
| `bloqueada` | `motivo` ∈ sesion_expirada, login_requerido, captcha, verificacion, sitio_bloqueado, limite_sitio, requiere_cuenta_externa, vacante_cerrada, otro | no fatal; CRM nueva + motivo; se re-encola solo con `--reintentar` |
| `fallida` | `motivo` (causa) | CRM nueva + causa en notes |
| `ya-aplicada` | — | el CRM o el sitio ya la mostraban aplicada; no se toca nada |

Salidas: `logs/apply-YYYY-MM-DD.log` (vacante, plataforma, hora, confirmación, folio o
"sin folio"), `logs/apply-crm-sync-YYYY-MM-DD.json` (externalKey=url + `updateVacancy`; Claude Code
no tiene acceso al artefacto crm-empleo: lo aplica el agente principal), `logs/apply-dryrun-*.json`.

**Idempotencia.** Una vacante (o su duplicado) en cola como pendiente / en_curso / aplicada /
pendiente-respuesta-Alex / ya-aplicada no se vuelve a encolar; `--report` rechaza una segunda
`aplicada` y cualquier reporte de una tarea que no esté `en_curso`; las tareas `en_curso` sin
reporte no se re-entregan (hay que verificar en "Mis empleos" y reportarlas). Además el paso 0 de
las instrucciones obliga a revisar el status en el CRM (`listVacancies`) antes de abrir el formulario.

**Límites.** Cupo diario 10 y delays 60–180 s (`apply-config.json › limites`). Plataformas: LinkedIn
Easy Apply e Indeed primero (tier 1), Greenhouse/Lever/Workable después (tier 2), resto tier 3. Un ATS
externo que exige crear cuenta queda `bloqueada` (el módulo no crea cuentas). Vacantes que piden
aplicar **por correo** (p. ej. SUNKSSD, Wellfound) no tienen formulario: quedan
`pendiente-respuesta-Alex` con la instrucción literal, porque piden pretensión salarial,
disponibilidad y un video/texto que solo Alex puede dar. Lo que no está en `respuestas_conocidas`
(salario, fecha de inicio, autorización de trabajo/visa, años en una herramienta, carta, datos
sensibles) **detiene** la aplicación; nunca se inventa. Para desbloquear una respuesta recurrente,
Alex la confirma y se agrega a `respuestas_conocidas` con su fuente.

## Notas y límites conocidos

- JobSpy usa los endpoints públicos de cada sitio, sin login. Estado observado:
  Glassdoor está bloqueado de forma persistente (403 / curl 56) y ZipRecruiter
  devuelve 403 ("blocked: use a proxy") desde la corrida del 15 nov 2026;
  Indeed da 403 de forma intermitente (en la prueba del 16 nov 01:24 UTC falló
  los 4 intentos del perfil `1_F1_AIAutomation_indeed`, mientras que en la
  re-verificación del 15 nov 19:26 CST devolvió 329 filas); LinkedIn da 429
  ocasional. Sin proxy no hay forma de forzar estos sitios: el sistema los
  degrada con elegancia (reintentos + circuit breaker) y la corrida sigue con
  los que responden. Un sitio persistentemente bloqueado cuesta ~130 s de
  esperas de reintento antes de que el breaker lo calle (en la corrida
  completa, el breaker lo abre en el 2º perfil de ese sitio). ZipRecruiter
  además casi no publica vacantes para México.
- Rotación de user-agent: JobSpy sí expone `user_agent` y este código rota 4
  UAs reales por llamada; no es una limitación pendiente.
- El match de keywords es por frase exacta, sin distinguir mayúsculas ni acentos y
  en límite de palabra (para que "AWS" no matchee "laws").
- "Easy Apply" solo se detecta si aparece en el texto de la vacante.
