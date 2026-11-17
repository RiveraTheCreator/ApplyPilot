# Integración Firecrawl en el pipeline de empleo

## Qué es
`firecrawl_sources.py` complementa a `scraper.py` (JobSpy) con vacantes de
fuentes que JobSpy no cubre o que bloquean IPs de centro de datos:
**Wellfound, Get on Board, Torre, Remote OK, We Work Remotely** y páginas de
carrera nativas listadas en `TARGET_COMPANY_PAGES` (n8n, entre otras).

Estrategia por fuente (se prefiere buscar a scrapear):
1. `firecrawl_search` con queries `site:` (máximo 2 por fuente) para descubrir
   URLs de vacantes individuales.
2. `firecrawl_scrape` de 1 página de listado por fuente para sacar varias
   vacantes de una sola llamada.
3. `firecrawl_scrape` de vacantes individuales hasta `--limit` en total por
   corrida, para la descripción completa. Solo URLs que aún no están en la
   base; las que no entran en el tope se guardan con el extracto de la
   búsqueda o del listado.

Las URLs de categoría/listado se descartan: nunca se crea una vacante a partir
de una página que no sea de vacante individual. Cada vacante se normaliza al
esquema de `vacantes` con las mismas funciones que `scraper.map_job` y se
guarda con `db.insert_vacante` (INSERT OR IGNORE: re-ejecutar no duplica).
Después pasa por `filter_rank.py` igual que cualquier otra, sin código
especial. Lo que la página no trae (sueldo, empresa, fecha) queda en None /
vacío: nada se inventa.

## Credencial y presupuesto
Firecrawl se invoca SOLO vía el helper `custom.firecrawl/bin/client.py`
(subprocess). El helper gestiona la credencial: este módulo no la lee, no la
recibe y no la escribe en ningún sitio. **Nunca extraer ni guardar la API key
cruda.**

Presupuesto por corrida: `--max-credits` (default 100) y una reserva mínima
`--min-reserve` (default 100: no se gasta si a la cuenta le quedan menos).
El gasto se contabiliza con el `creditsUsed` de cada respuesta y se contrasta
con `firecrawl_credit_usage` antes y después (el contador de la cuenta puede
tardar en reflejar el gasto). Coste observado 2026-11-16: search 2 créditos,
scrape 1 crédito. Saldo: ~1,500 créditos hasta el 2026-12-02.

Degradación elegante: un fallo (timeout, 429, 401/403, JSON inválido, página
sin vacantes parseables) queda en el log con su causa y la corrida sigue.
`run_firecrawl_sources` nunca lanza excepción por un fallo de fuente.

## CLI
```bash
python3 firecrawl_sources.py --db vacantes.db --limit 5
python3 firecrawl_sources.py --db vacantes.db --limit 3 --sources wellfound,remoteok --dry-run
python3 firecrawl_sources.py --db vacantes.db --sources torre,getonboard --max-credits 50
```
Opciones: `--db`, `--limit` (scrapes de detalle por corrida, en total;
default 5), `--sources` (wellfound, getonboard, torre, remoteok,
weworkremotely, empresas), `--max-credits` (default 100), `--min-reserve`
(default 100), `--dry-run` (llamadas y parseo sin insertar),
`--keywords-file` (default keywords.json).

## Integración en run_daily.py
Pegar en `run_daily.main()`, después del bloque que loguea el resumen de
JobSpy ("Fuentes sin ninguna búsqueda exitosa") y antes de
`filter_rank.evaluate(...)`:

```python
try:
    import firecrawl_sources
    fc_stats = firecrawl_sources.run_firecrawl_sources(
        args.db, limit=5, max_credits=100, keywords_file=keywords_file)
    for line in firecrawl_sources.format_summary(fc_stats).splitlines():
        log.info(line)
except Exception:  # noqa: BLE001 - Firecrawl nunca tumba la corrida diaria
    log.exception("FALLO en Firecrawl; la corrida sigue con el filtro")
```

El logger "empleo.firecrawl" hereda los handlers de "empleo" (consola + log
del día). Firecrawl nunca tumba la corrida diaria: si falla, el log lo dice
y la corrida sigue con JobSpy + filtro.

## Estado
- 2026-11-16: módulo creado y probado en DB de prueba (29 filas: Wellfound
  19, Remote OK 10, 0 créditos netos en el contador). Primera corrida real
  contra `vacantes.db` (2026-11-16 07:00): **29 filas nuevas** (Wellfound 19,
  Remote OK 10, fuente `firecrawl:wellfound` / `firecrawl:remoteok`), 0
  duplicadas, 1 URL descartada, 0 fallos; créditos 1500 → 1500 en el contador
  (13 reportados por las llamadas). Total DB: 796 filas.
- Fuentes que hoy dan 0 por bloqueo (Indeed, Glassdoor, ZipRecruiter) se
  cubren parcialmente con estas fuentes; el health JSON las marca como
  alerta de "fuente en 0".
