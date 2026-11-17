#!/usr/bin/env python3
"""Autovigilancia del filtro (Ranker v2): escribe ``logs/filter-health-YYYY-MM-DD.json``.

Lo llama :func:`filter_rank.evaluate` tras cada evaluación. Contiene:
  - totales: evaluadas, pasan, descartadas, tasa de pase, conteo por estrella,
  - motivos de descarte (conteo y tasa),
  - por fuente: evaluadas, pasan, tasa de pase, nuevas del día,
  - por keyword: vacantes evaluadas que la contienen y nuevas del día,
  - por país (derivado de ``ubicacion`` con la lista de ``filter-config.json``),
  - ``checks``: cada regla de alerta evaluada (ok / alert / sin_historial),
  - ``alerts``: solo las que se dispararon, cada una con causa probable y acción sugerida.

El review diario lee este JSON. Las correcciones se hacen en
``filter-config.json`` y se anotan en ``filter-changelog.md`` (ver README).
Un archivo por día: si hay varias corridas, la última sobrescribe la del día.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from textutil import normalize

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
PROFILES_FILE = BASE_DIR / "search-profiles.json"

_REGIONAL_TOKENS = ("remote", "remoto", "anywhere", "worldwide", "latam", "latin america",
                    "americas", "north america")


def country_of(ubicacion: str | None, countries: list[dict]) -> str:
    """Código de país (MX, CO, …) desde la ubicación; ``REGIONAL`` / ``OTRO`` / ``DESCONOCIDO``."""
    loc = normalize(ubicacion).strip()
    if not loc:
        return "DESCONOCIDO"
    last = loc.split(",")[-1].strip()
    for c in countries:
        names = {normalize(c["name"]), c["code"].lower(), *map(normalize, c.get("aliases", []))}
        if last in names:
            return c["code"]
    for c in countries:
        for alias in [c["name"], *c.get("aliases", [])]:
            alias = normalize(alias)
            if len(alias) > 3 and alias in loc:
                return c["code"]
    if any(tok in loc for tok in _REGIONAL_TOKENS):
        return "REGIONAL"
    return "OTRO"


def _scrape_day(item: dict) -> str | None:
    value = item.get("fecha_scrapeo") or ""
    return value[:10] or None


def _load_previous(log_dir: Path, today: date, days: int) -> list[dict]:
    prev = []
    for i in range(1, days + 1):
        path = log_dir / f"filter-health-{today - timedelta(days=i):%Y-%m-%d}.json"
        if path.exists():
            try:
                prev.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
    return prev


def _expected_sources(fcfg: dict) -> list[str]:
    """Fuentes que el plan de búsqueda usa hoy (config ∩ search-profiles.json)."""
    expected = list(fcfg["health"].get("expected_sources", []))
    try:
        planned = {p.get("site_name") for p in json.loads(PROFILES_FILE.read_text(encoding="utf-8"))}
        expected = [s for s in expected if s in planned]
    except (OSError, json.JSONDecodeError):
        pass
    return expected


def _alert(checks: list, alerts: list, check: str, status: str, message: str,
           cause: str = "", action: str = "", severity: str = "warning", metric=None) -> None:
    entry = {"check": check, "status": status, "message": message}
    if metric is not None:
        entry["metric"] = metric
    checks.append(entry)
    if status == "alert":
        alerts.append({"check": check, "severity": severity, "message": message,
                       "probable_cause": cause, "suggested_action": action, "metric": metric})


def build_health(result: dict, evaluated: list[dict], fcfg: dict, config: dict,
                 log_dir: Path, today: date | None = None) -> dict:
    """Arma el dict de salud del filtro (sin escribirlo)."""
    h = fcfg["health"]
    today = today or datetime.now(timezone.utc).date()
    today_s = today.isoformat()
    total = len(evaluated)
    passed = sum(1 for e in evaluated if e["passes"])
    pass_rate = round(passed / total, 4) if total else None
    countries = fcfg.get("countries", [])
    disabled = {normalize(k) for k in fcfg.get("keywords", {}).get("disabled", [])}

    # Por día de scrapeo (para historial interno de la propia base).
    by_day: dict[str, list[dict]] = defaultdict(list)
    for e in evaluated:
        day = _scrape_day(e)
        if day:
            by_day[day].append(e)
    new_today = by_day.get(today_s, [])

    # Motivos de descarte.
    drops = Counter(r for e in evaluated for r in e["reasons"])
    drop_reasons = {r: {"count": n, "rate": round(n / total, 4)} for r, n in drops.items()}

    # Por fuente.
    by_source = defaultdict(lambda: {"evaluated": 0, "passed": 0, "new_today": 0})
    for e in evaluated:
        s = by_source[e["fuente"] or "desconocida"]
        s["evaluated"] += 1
        s["passed"] += e["passes"]
        s["new_today"] += _scrape_day(e) == today_s
    for src in _expected_sources(fcfg):
        by_source[src]  # crea la fuente en 0 si no trajo nada
    for s in by_source.values():
        s["pass_rate"] = round(s["passed"] / s["evaluated"], 4) if s["evaluated"] else None

    # Por keyword (todas las de keywords.json, incluso con 0).
    by_keyword = {}
    for arista in config["aristas"]:
        for kw in arista["keywords"]:
            if kw in by_keyword:
                continue
            by_keyword[kw] = {"arista": arista["id"], "matches_evaluated": 0,
                              "matches_new_today": 0, "passed": 0,
                              "disabled": normalize(kw) in disabled}
    for e in evaluated:
        for kw in e["keywords"]:
            if kw in by_keyword:
                by_keyword[kw]["matches_evaluated"] += 1
                by_keyword[kw]["passed"] += e["passes"]
                by_keyword[kw]["matches_new_today"] += _scrape_day(e) == today_s

    # Por país.
    by_country = defaultdict(lambda: {"evaluated": 0, "passed": 0, "new_today": 0})
    for e in evaluated:
        c = by_country[country_of(e.get("ubicacion"), countries)]
        c["evaluated"] += 1
        c["passed"] += e["passes"]
        c["new_today"] += _scrape_day(e) == today_s
    for c in countries:
        by_country[c["code"]]

    stars = Counter(e["stars"] for e in evaluated if e["passes"])
    by_modality = Counter(e["modality"] for e in evaluated)

    # ---------------------------------------------------------------- alertas
    checks, alerts = [], []
    prev = _load_previous(log_dir, today, h["history_days"])

    # 1) Tasa de pase fuera de rango.
    if pass_rate is None:
        _alert(checks, alerts, "pass_rate", "alert", "No hubo vacantes evaluadas.",
               "El scraper no insertó filas o la consulta no encontró estado='nueva'.",
               "Revisar logs/YYYY-MM-DD.log del scraper.", "critical")
    elif pass_rate < h["pass_rate_min"]:
        _alert(checks, alerts, "pass_rate", "alert",
               f"Tasa de pase {pass_rate:.1%} < mínimo {h['pass_rate_min']:.0%} (demasiado estricto).",
               "El detector presencial está marcando de más (regex nueva, cambio de formato en una "
               "fuente) o los perfiles traen sobre todo vacantes presenciales.",
               "Revisar una muestra de descartadas y ajustar el detector o los perfiles.",
               metric=pass_rate)
    elif pass_rate > h["pass_rate_max"]:
        _alert(checks, alerts, "pass_rate", "alert",
               f"Tasa de pase {pass_rate:.1%} > máximo {h['pass_rate_max']:.0%} (demasiado laxo).",
               "El detector presencial no está encontrando señales (texto vacío, descripciones "
               "no descargadas) o hard_filter.discard_onsite_only está apagado.",
               "Verificar que las descripciones llegan y que discard_onsite_only = true.",
               metric=pass_rate)
    else:
        _alert(checks, alerts, "pass_rate", "ok",
               f"Tasa de pase {pass_rate:.1%} dentro de [{h['pass_rate_min']:.0%}, {h['pass_rate_max']:.0%}].",
               metric=pass_rate)

    # 2) Distribución de estrellas (laxo / estricto).
    if passed:
        share5 = stars.get(5, 0) / passed
        top = stars.get(5, 0) + stars.get(4, 0)
        if share5 > h["five_star_share_max"]:
            _alert(checks, alerts, "stars_distribution", "alert",
                   f"{share5:.1%} de las que pasan son 5★ (máx {h['five_star_share_max']:.0%}).",
                   "Umbrales de estrellas demasiado laxos (fit_strong_min bajo) o una keyword muy genérica.",
                   "Subir stars.fit_strong_min o apagar la keyword genérica en filter-config.json.",
                   metric=round(share5, 4))
        elif top < h["top_stars_min"]:
            _alert(checks, alerts, "stars_distribution", "alert",
                   f"Solo {top} vacantes con 4–5★.",
                   "Umbrales demasiado estrictos o perfiles que no traen roles alineados.",
                   "Revisar stars.* y los perfiles de búsqueda.", metric=top)
        else:
            _alert(checks, alerts, "stars_distribution", "ok",
                   f"5★ = {share5:.1%}, 4–5★ = {top}.", metric=dict(stars))

    # 3) Fuente entera en 0 (sin vacantes nuevas hoy, o 0% de pase).
    any_new_today = bool(new_today)
    for src in _expected_sources(fcfg):
        s = by_source[src]
        if any_new_today and s["new_today"] == 0:
            _alert(checks, alerts, f"source_zero:{src}", "alert",
                   f"La fuente {src} no trajo ninguna vacante nueva hoy.",
                   "Bloqueo de la fuente (403 / curl 56 / proxy caído / rate limit 429) o perfiles "
                   "de esa fuente descartados por validación.",
                   f"Buscar '{src}' en logs/{today_s}.log; si es bloqueo persistente, cubrir con "
                   "Firecrawl o proxies (README › Proxies).", metric=0)
        else:
            _alert(checks, alerts, f"source_zero:{src}", "ok" if any_new_today else "sin_historial",
                   f"{src}: {s['new_today']} nuevas hoy, {s['evaluated']} evaluadas."
                   if any_new_today else f"{src}: no hubo scrapeo hoy; no se evalúa.")
    for src, s in by_source.items():
        if s["evaluated"] >= h["source_min_rows_for_pass_alert"] and s["passed"] == 0:
            _alert(checks, alerts, f"source_pass_zero:{src}", "alert",
                   f"La fuente {src} tiene {s['evaluated']} evaluadas y 0 pasan.",
                   "Cambio de formato en la fuente que hace parecer todo presencial, o perfiles de "
                   "esa fuente sin filtro remoto.",
                   "Revisar una muestra de descartadas de esa fuente.", metric=0)

    # 4) Keyword con 0 resultados N días seguidos (días con scrapeo).
    n = h["keyword_zero_days"]
    days_with_data = sorted(d for d in by_day if d <= today_s)[-n:]
    if len(days_with_data) < n:
        zero_today = [k for k, v in by_keyword.items() if v["matches_new_today"] == 0 and not v["disabled"]]
        _alert(checks, alerts, "keyword_zero_streak", "sin_historial",
               f"Solo hay {len(days_with_data)} día(s) de scrapeo; se necesitan {n}. "
               f"Keywords en 0 hoy (informativo): {len(zero_today)}.",
               metric={"days_with_data": days_with_data, "zero_today": zero_today})
    else:
        streak = []
        for kw, v in by_keyword.items():
            if v["disabled"]:
                continue
            if all(not any(kw in e["keywords"] for e in by_day[d]) for d in days_with_data):
                streak.append(kw)
        if streak:
            _alert(checks, alerts, "keyword_zero_streak", "alert",
                   f"{len(streak)} keyword(s) con 0 vacantes en los últimos {n} días con scrapeo.",
                   "Keyword muerta (nadie publica ese título) o sin perfil de búsqueda que la cubra.",
                   "Apagarla en filter-config.json › keywords.disabled o sumar un perfil que la busque; "
                   "anotar en filter-changelog.md.", metric=streak)
        else:
            _alert(checks, alerts, "keyword_zero_streak", "ok",
                   f"Ninguna keyword en 0 los últimos {n} días.")

    # 5) Motivo de descarte que se dispara vs el promedio.
    prev_days = [d for d in sorted(by_day) if d < today_s][-h["history_days"]:]
    hist_rates = defaultdict(list)
    for d in prev_days:
        rows = by_day[d]
        c = Counter(r for e in rows for r in e["reasons"])
        for r in set(c) | set(drops):
            hist_rates[r].append(c.get(r, 0) / len(rows))
    for p in prev:  # historial de corridas anteriores (si la base ya no tiene esas filas)
        if p.get("date") in prev_days:
            continue
        for r, v in (p.get("drop_reasons_today") or {}).items():
            hist_rates[r].append(v.get("rate", 0))
    today_drops = Counter(r for e in new_today for r in e["reasons"])
    if not new_today or not hist_rates:
        _alert(checks, alerts, "drop_reason_spike", "sin_historial",
               "Sin días anteriores con datos para comparar los motivos de descarte.")
    else:
        spiked = []
        for r, cnt in today_drops.items():
            rate = cnt / len(new_today)
            hist = hist_rates.get(r) or [0.0]
            avg = sum(hist) / len(hist)
            if cnt >= h["drop_reason_min_count"] and rate > max(avg * h["drop_reason_spike_factor"],
                                                                avg + h["drop_reason_spike_abs"]):
                spiked.append({"reason": r, "rate_today": round(rate, 4), "avg": round(avg, 4)})
        if spiked:
            _alert(checks, alerts, "drop_reason_spike", "alert",
                   f"{len(spiked)} motivo(s) de descarte disparados vs el promedio.",
                   "Cambio de formato en una fuente o regex del detector demasiado amplia.",
                   "Revisar muestras de las descartadas hoy por ese motivo.", metric=spiked)
        else:
            _alert(checks, alerts, "drop_reason_spike", "ok",
                   "Motivos de descarte en línea con el promedio.")

    # 6) Caída brusca de vacantes nuevas vs el promedio.
    hist_new = [len(by_day[d]) for d in prev_days]
    hist_new += [p["new_rows_today"] for p in prev
                 if p.get("date") not in prev_days and isinstance(p.get("new_rows_today"), int)]
    if not hist_new:
        _alert(checks, alerts, "new_rows_drop", "sin_historial",
               f"{len(new_today)} vacantes nuevas hoy; sin días anteriores para comparar.",
               metric=len(new_today))
    else:
        avg_new = sum(hist_new) / len(hist_new)
        if avg_new > 0 and len(new_today) < avg_new * h["new_rows_drop_ratio"]:
            _alert(checks, alerts, "new_rows_drop", "alert",
                   f"Vacantes nuevas hoy {len(new_today)} < {h['new_rows_drop_ratio']:.0%} del promedio "
                   f"({avg_new:.0f}).",
                   "Fuente bloqueada, corrida incompleta (límite / timeout) o perfiles descartados.",
                   "Revisar el RESUMEN y los fallos por fuente en el log diario.",
                   "critical" if not new_today else "warning", metric=len(new_today))
        else:
            _alert(checks, alerts, "new_rows_drop", "ok",
                   f"{len(new_today)} nuevas hoy vs promedio {avg_new:.0f}.", metric=len(new_today))

    return {
        "date": today_s,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scope": result.get("scope"),
        "config_version": fcfg.get("version"),
        "totals": {
            "evaluated": total, "passed": passed, "dropped": total - passed,
            "pass_rate": pass_rate,
            "stars": {f"{s}": stars.get(s, 0) for s in (5, 4, 3, 2, 1)},
            "by_modality": dict(by_modality),
        },
        "new_rows_today": len(new_today),
        "drop_reasons": drop_reasons,
        "drop_reasons_today": {r: {"count": c, "rate": round(c / len(new_today), 4)}
                               for r, c in today_drops.items()} if new_today else {},
        "by_source": dict(by_source),
        "by_keyword": by_keyword,
        "by_country": dict(sorted(by_country.items())),
        "checks": checks,
        "alerts": alerts,
    }


def write_health(result: dict, evaluated: list[dict], db_path: str, fcfg: dict, config: dict,
                 config_file=None, log_dir=None) -> Path:
    """Escribe logs/filter-health-YYYY-MM-DD.json y devuelve la ruta."""
    log_dir = Path(log_dir) if log_dir else LOG_DIR
    log_dir.mkdir(exist_ok=True)
    health = build_health(result, evaluated, fcfg, config, log_dir)
    health["db"] = str(db_path)
    path = log_dir / f"filter-health-{health['date']}.json"
    path.write_text(json.dumps(health, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":  # resumen legible del último health JSON
    import sys
    files = sorted(LOG_DIR.glob("filter-health-*.json"))
    if not files:
        print("No hay logs/filter-health-*.json")
        sys.exit(1)
    data = json.loads(files[-1].read_text(encoding="utf-8"))
    t = data["totals"]
    print(f"{files[-1].name}: evaluadas {t['evaluated']} | pasan {t['passed']} "
          f"({t['pass_rate']:.1%}) | estrellas {t['stars']}")
    for a in data["alerts"]:
        print(f"  [{a['severity']}] {a['message']} — causa probable: {a['probable_cause']}")
    if not data["alerts"]:
        print("  Sin alertas.")
