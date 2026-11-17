#!/usr/bin/env python3
"""Amplía search-profiles.json con perfiles regionales (Ranker v2, 2026-11-16).

NO regenera el archivo: lee los perfiles existentes y solo AÑADE los que faltan
(por ``name``). Idempotente: correrlo dos veces no duplica nada. Los países
salen de ``filter-config.json`` › ``countries``; México se omite porque ya
tiene 20 perfiles propios (los originales de build_search_profiles.py).

Reglas (scraper-config-spec.md + build prompt Ranker v2):
  - Siempre filtro remoto: ``is_remote: true`` en Indeed / Glassdoor /
    ZipRecruiter. En LinkedIn el scraper quita ``is_remote`` (JobSpy lo ignora
    ahí), así que el filtro remoto va en el ``search_term`` ("remote").
  - Indeed: booleanos OK, ``country_indeed`` siempre, location = país real
    (nunca "Remote"), sin ``hours_old`` (por is_remote).
  - LinkedIn: sin booleanos, location por país, results_wanted <= 100.
  - Glassdoor: sin booleanos, ``country_indeed``, location "Ciudad, País"
    (o "Ciudad, Estado" en US/CA). Solo países con dominio Glassdoor en JobSpy.
  - ZipRecruiter: SOLO US/CA, location "City, State", sin booleanos.
    (El spec original decía "sin ZipRecruiter" porque la estrategia era solo
    LATAM; el prompt del Ranker v2 añade América del Norte y pide ZipRecruiter
    para US/CA. Anotado en scraper-config-spec.md.)

Uso:
    python3 build_regional_profiles.py            # añade y valida
    python3 build_regional_profiles.py --dry-run  # solo muestra qué añadiría
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PROFILES = BASE_DIR / "search-profiles.json"
CONFIG = BASE_DIR / "filter-config.json"

# Mercados grandes: 2 perfiles LinkedIn; el resto 1.
BIG_MARKETS = {"CO", "AR", "BR", "CL", "PE", "US", "CA"}
# Países con dominio Glassdoor en JobSpy (jobspy/glassdoor/constant.py), además de MX.
GLASSDOOR_OK = {"AR", "BR", "US", "CA"}
ZIP_OK = {"US", "CA"}

INDEED_TERM = ('("AI Automation" OR "Marketing Automation" OR "Implementation Specialist" '
               'OR "Revenue Operations" OR "Customer Onboarding")')
INDEED_TERM_BR = ('("AI Automation" OR "Marketing Automation" OR "Implementation Specialist" '
                  'OR "Revenue Operations" OR "Automação de Marketing" OR "Implantação")')

_BOOLEAN_RE = re.compile(r"\b(OR|AND)\b")


def regional_profiles(countries: list[dict], start: int) -> list[dict]:
    out: list[dict] = []
    n = start

    def add(profile: dict) -> None:
        nonlocal n
        profile["name"] = profile["name"].format(n=n)
        out.append(profile)
        n += 1

    for c in countries:
        cc = c["code"]
        if cc == "MX":
            continue
        li_loc = c.get("linkedin_location", c["name"])
        add({"name": "{n}_F1_AIAutomation" + cc + "_linkedin", "site_name": "linkedin",
             "search_term": "AI automation remote", "location": li_loc, "results_wanted": 25})
        if cc in BIG_MARKETS:
            add({"name": "{n}_F3_Implementation" + cc + "_linkedin", "site_name": "linkedin",
                 "search_term": "implementation specialist remote", "location": li_loc,
                 "results_wanted": 25})
        if c.get("indeed"):
            add({"name": "{n}_F1_AutomationImpl" + cc + "_indeed", "site_name": "indeed",
                 "search_term": INDEED_TERM_BR if cc == "BR" else INDEED_TERM,
                 "location": c["name"] if cc not in ("US",) else "United States",
                 "country_indeed": c["indeed"], "is_remote": True, "results_wanted": 25})
        if cc in GLASSDOOR_OK and c.get("glassdoor_location"):
            add({"name": "{n}_F3_ImplementationSpecialist" + cc + "_glassdoor", "site_name": "glassdoor",
                 "search_term": "Implementation Specialist", "location": c["glassdoor_location"],
                 "country_indeed": c["indeed"], "is_remote": True, "results_wanted": 25})
        if cc in ZIP_OK and c.get("zip_location"):
            add({"name": "{n}_F1_AIAutomation" + cc + "_zip_recruiter", "site_name": "zip_recruiter",
                 "search_term": "AI Automation Specialist", "location": c["zip_location"],
                 "country_indeed": c["indeed"], "is_remote": True, "results_wanted": 25})
    return out


def validate(profiles: list[dict]) -> list[str]:
    """Reglas del spec. Devuelve la lista de errores (vacía = válido)."""
    errors = []
    names = [p.get("name") for p in profiles]
    for name, cnt in Counter(names).items():
        if cnt > 1:
            errors.append(f"name duplicado: {name}")
    for p in profiles:
        name, site = p.get("name", "?"), p.get("site_name")
        for k in ("name", "site_name", "search_term", "location", "results_wanted"):
            if k not in p:
                errors.append(f"{name}: falta {k}")
        if " " in name:
            errors.append(f"{name}: name con espacios")
        if site not in ("indeed", "linkedin", "glassdoor", "zip_recruiter"):
            errors.append(f"{name}: site_name inválido {site!r}")
        if site != "indeed" and _BOOLEAN_RE.search(p.get("search_term", "")):
            errors.append(f"{name}: booleanos fuera de indeed")
        if site == "indeed":
            if not p.get("country_indeed"):
                errors.append(f"{name}: indeed sin country_indeed")
            if p.get("location", "").lower() == "remote":
                errors.append(f"{name}: indeed con location Remote")
            if (p.get("is_remote") or p.get("job_type")) and "hours_old" in p:
                errors.append(f"{name}: hours_old prohibido con is_remote/job_type")
        if site == "linkedin":
            if p.get("results_wanted", 0) > 100:
                errors.append(f"{name}: results_wanted > 100 en linkedin")
            if p.get("easy_apply") and "hours_old" in p:
                errors.append(f"{name}: hours_old prohibido con easy_apply")
        if site == "glassdoor":
            if not p.get("country_indeed"):
                errors.append(f"{name}: glassdoor sin country_indeed")
            if ", " not in p.get("location", ""):
                errors.append(f"{name}: glassdoor location no es 'Ciudad, País/Estado'")
        if site == "zip_recruiter":
            if p.get("country_indeed") not in ("USA", "Canada"):
                errors.append(f"{name}: zip_recruiter fuera de US/CA")
            if ", " not in p.get("location", ""):
                errors.append(f"{name}: zip_recruiter location no es 'City, State'")
    return errors


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    existing = json.loads(PROFILES.read_text(encoding="utf-8"))
    countries = json.loads(CONFIG.read_text(encoding="utf-8"))["countries"]
    # Idempotencia: comparamos sin el prefijo numérico (el número depende del orden).
    def key(p):
        return re.sub(r"^\d+_", "", p["name"])
    have = {key(p) for p in existing}
    start = max(int(p["name"].split("_", 1)[0]) for p in existing) + 1
    new = [p for p in regional_profiles(countries, start) if key(p) not in have]
    # Renumerar de forma contigua tras filtrar.
    for i, p in enumerate(new):
        p["name"] = f"{start + i}_{key(p)}"
    merged = existing + new
    errors = validate(merged)
    if errors:
        print("ERRORES de validación:\n  " + "\n  ".join(errors))
        return 1
    print(f"Existentes: {len(existing)} | nuevos: {len(new)} | total: {len(merged)}")
    if args.dry_run:
        for p in new:
            print("  +", p["name"], "|", p["location"])
        return 0
    PROFILES.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    json.loads(PROFILES.read_text(encoding="utf-8"))  # revalidar parseo
    print("OK:", PROFILES)
    return 0


if __name__ == "__main__":
    sys.exit(main())
