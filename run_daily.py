#!/usr/bin/env python3
"""Corrida diaria del pipeline: scraper -> filtro/ranking -> monitor -> log.

Por defecto ejecuta los perfiles de ``search-profiles.json`` (cada uno con su
``results_wanted``), una búsqueda a la vez. Si ese archivo falta o no tiene
perfiles válidos, cae al plan por keywords: las 11 aristas y TODAS sus
keywords, 25 resultados por keyword y por sitio. Idempotente: re-ejecutar no
duplica filas (UNIQUE + INSERT OR IGNORE).

Uso:
    python3 run_daily.py
    python3 run_daily.py --limit 10
    python3 run_daily.py --profiles 12_F3_HospitalitySaaS_linkedin --db prueba.db
    python3 run_daily.py --no-profiles --aristas 1,6 --limit 5 --db prueba.db

Código de salida: 0 aunque falle alguna fuente (queda en el log); distinto de
0 solo si la corrida falla por completo (ninguna búsqueda respondió, no se
pudo cargar JobSpy o hubo una excepción no controlada).
"""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
VENV_PYTHON = BASE_DIR / ".venv" / "bin" / "python"
TOP_IN_SUMMARY = 15

log = logging.getLogger("empleo")


def setup_logging() -> Path:
    """Log a logs/YYYY-MM-DD.log (se anexa) y a la consola."""
    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"{datetime.now():%Y-%m-%d}.log"
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    console = logging.StreamHandler(sys.stdout)
    for handler in (file_handler, console):
        handler.setFormatter(formatter)
    log.setLevel(logging.INFO)
    log.handlers[:] = [file_handler, console]
    log.propagate = False
    return log_path


def _can_import_jobspy() -> bool:
    try:
        importlib.import_module("jobspy")
        return True
    except ImportError:
        return False


def ensure_jobspy() -> bool:
    """Garantiza que JobSpy sea importable.

    1) Si ya se importa, listo. 2) Si existe el .venv del proyecto con JobSpy,
    se re-ejecuta este script con ese intérprete. 3) Si no, intenta
    ``pip install --user -q python-jobspy`` (falla en sistemas con Python
    "externally managed"; en ese caso crea el .venv, ver README).
    """
    if _can_import_jobspy():
        return True
    # sys.prefix, no sys.executable: .venv/bin/python es un symlink al Python del sistema.
    in_venv = Path(sys.prefix).resolve() == (BASE_DIR / ".venv").resolve()
    if VENV_PYTHON.exists() and not in_venv:
        probe = subprocess.run([str(VENV_PYTHON), "-c", "import jobspy"], capture_output=True)
        if probe.returncode == 0:
            log.info("JobSpy no está en %s; re-ejecutando con %s", sys.executable, VENV_PYTHON)
            for handler in log.handlers:
                handler.flush()
            os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])
    log.info("JobSpy no instalado; ejecutando pip install --user -q python-jobspy")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--user", "-q", "python-jobspy"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.error("pip install falló: %s", (result.stderr or result.stdout).strip()[-500:])
        return False
    importlib.invalidate_caches()
    return _can_import_jobspy()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Corrida diaria: scraper + filtro/ranking.")
    parser.add_argument("--aristas", help="ids separados por coma (default: las 11)")
    parser.add_argument("--limit", type=int, default=None,
                        help="resultados por búsqueda (default: results_wanted de cada "
                             "perfil; 25 en el plan por keywords)")
    parser.add_argument("--db", default=str(BASE_DIR / "vacantes.db"))
    parser.add_argument("--profiles-file", default=str(BASE_DIR / "search-profiles.json"),
                        help="perfiles de búsqueda (default search-profiles.json)")
    parser.add_argument("--no-profiles", action="store_true",
                        help="ignora los perfiles y usa el plan por keywords")
    parser.add_argument("--profiles", help="nombres de perfil separados por coma (depuración)")
    args = parser.parse_args(argv)

    os.chdir(BASE_DIR)
    log_path = setup_logging()
    log.info("=== Corrida diaria: aristas=%s limit=%s db=%s perfiles=%s ===",
             args.aristas or "TODAS", args.limit or "default", args.db,
             "NO (plan por keywords)" if args.no_profiles else args.profiles or "todos")

    if not ensure_jobspy():
        log.error("FALLO TOTAL: no se pudo cargar JobSpy. Ver README (instalación).")
        return 2

    import filter_rank
    import scraper

    keywords_file = str(BASE_DIR / "keywords.json")
    try:
        stats = scraper.scrape_aristas(
            arista_ids=scraper.parse_aristas_arg(args.aristas),
            limit=args.limit,
            db_path=args.db,
            keywords_file=keywords_file,
            max_keywords_per_arista=None,  # siempre TODAS las keywords en la corrida diaria
            profiles_file=args.profiles_file,
            use_profiles=not args.no_profiles,
            profiles=scraper.parse_profiles_arg(args.profiles),
        )
    except Exception:  # noqa: BLE001
        log.exception("FALLO TOTAL en el scraper")
        return 1
    for line in scraper.format_summary(stats).splitlines():
        log.info(line)
    failed_sites = [s for s, v in stats["per_site"].items() if v["ok"] == 0]
    if failed_sites:
        log.warning("Fuentes sin ninguna búsqueda exitosa: %s", ", ".join(failed_sites))

    try:
        import firecrawl_sources
        fc_stats = firecrawl_sources.run_firecrawl_sources(
            args.db, limit=5, max_credits=100, keywords_file=keywords_file)
        for line in firecrawl_sources.format_summary(fc_stats).splitlines():
            log.info(line)
    except Exception:  # noqa: BLE001 - Firecrawl nunca tumba la corrida diaria
        log.exception("FALLO en Firecrawl; la corrida sigue con el filtro")

    try:
        result = filter_rank.evaluate(args.db, keywords_file, all_rows=False)
    except Exception:  # noqa: BLE001
        log.exception("FALLO TOTAL en filter_rank")
        return 1
    for line in filter_rank.format_report(result, top=TOP_IN_SUMMARY).splitlines():
        log.info(line)

    plan = (f"perfiles ({stats['profiles']})" if stats["plan_source"] == "profiles"
            else f"keywords ({stats['searches']} búsquedas)")
    log.info(
        "RESUMEN: plan=%s | %d filas nuevas | %d evaluadas | %d pasan filtros | %d fallos de búsqueda "
        "(%s) | log: %s",
        plan, stats["inserted"], result["evaluated"], result["passed"], len(stats["failures"]),
        ", ".join(f"{s}:{v['failed']}" for s, v in stats["per_site"].items()), log_path,
    )
    run_monitor(args.db)
    if stats["total_failure"]:
        log.error("FALLO TOTAL: ninguna búsqueda respondió en ninguna fuente.")
        return 1
    return 0


def run_monitor(db_path: str) -> None:
    """Monitor del pipeline completo (solo lectura). Nunca cambia el exit code de la corrida."""
    try:
        import monitor
        snap = monitor.run(db_path=db_path)
        for line in monitor.format_summary(snap).splitlines():
            log.info("MONITOR %s", line)
        log.info("MONITOR JSON: %s", snap.get("_path"))
    except Exception:  # noqa: BLE001 - el monitor nunca tumba la corrida diaria
        log.exception("FALLO en el monitor; la corrida sigue (no afecta el exit code)")


if __name__ == "__main__":
    sys.exit(main())
