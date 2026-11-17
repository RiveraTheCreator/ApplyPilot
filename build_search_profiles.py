# -*- coding: utf-8 -*-
"""Genera search-profiles.json: 15-25 perfiles JobSpy desde keywords.json,
siguiendo scraper-config-spec.md (Mi Primera Chamba).
Estrategia LATAM/Mexico 100% remoto -> SIN ZipRecruiter.
Distribución: Indeed 8 (40%), LinkedIn 7 (35%), Glassdoor 5 (25%).

AVISO (Ranker v2, 2026-11-16): este script REESCRIBE search-profiles.json con
los 20 perfiles base de México. Los perfiles regionales (LATAM + US/CA) los
añade build_regional_profiles.py. Si regeneras la base, corre después
build_regional_profiles.py. Sin --force, el script se niega a sobrescribir un
archivo que ya tiene más de 20 perfiles.
"""
import json
import sys

OUT = "/home/hatch/workspace/empleo-pipeline/search-profiles.json"

profiles = [
    # --- Indeed (booleanos OK; country_indeed siempre; is_remote true; sin hours_old) ---
    {"name": "1_F1_AIAutomation_indeed", "site_name": "indeed",
     "search_term": '("AI Automation Specialist" OR "Marketing Automation" OR "n8n" OR "workflow automation")',
     "location": "Mexico", "country_indeed": "Mexico", "is_remote": True, "results_wanted": 25},
    {"name": "2_F1_RevOps_indeed", "site_name": "indeed",
     "search_term": '("Revenue Operations" OR "RevOps Analyst" OR "CRM Implementation" OR "HubSpot Operations")',
     "location": "Mexico", "country_indeed": "Mexico", "is_remote": True, "results_wanted": 25},
    {"name": "3_F1_OnboardingCS_indeed", "site_name": "indeed",
     "search_term": '("Customer Onboarding Specialist" OR "Technical Customer Success" OR "Implementation Specialist")',
     "location": "Mexico", "country_indeed": "Mexico", "is_remote": True, "results_wanted": 25},
    {"name": "4_F1_LowCode_indeed", "site_name": "indeed",
     "search_term": '("Low-Code Developer" OR "Internal Tools Developer" OR "No-Code Solutions Builder")',
     "location": "Mexico", "country_indeed": "Mexico", "is_remote": True, "results_wanted": 25},
    {"name": "5_F2_OpsAnalyst_indeed", "site_name": "indeed",
     "search_term": '("Operations Analyst" OR "Business Operations Analyst" OR "Automation Analyst")',
     "location": "Mexico", "country_indeed": "Mexico", "is_remote": True, "results_wanted": 25},
    {"name": "6_F2_ProductOps_indeed", "site_name": "indeed",
     "search_term": '("Product Operations Specialist" OR "Implementation Project Coordinator" OR "Technical Project Coordinator")',
     "location": "Mexico", "country_indeed": "Mexico", "is_remote": True, "results_wanted": 25},
    {"name": "7_F3_HospitalityTech_indeed", "site_name": "indeed",
     "search_term": '("Hospitality Implementation Specialist" OR "Hotel Technology Consultant" OR "Hospitality Customer Success")',
     "location": "Mexico", "country_indeed": "Mexico", "is_remote": True, "results_wanted": 25},
    {"name": "8_F3_SolutionsEngineer_indeed", "site_name": "indeed",
     "search_term": '("Solutions Engineer" OR "Implementation Consultant" OR "Technical Account Manager")',
     "location": "Mexico", "country_indeed": "Mexico", "is_remote": True, "results_wanted": 25},
    # --- LinkedIn (sin booleanos; keywords simples; results_wanted <= 100; sin hours_old con easy_apply) ---
    {"name": "9_F1_AIAgents_linkedin", "site_name": "linkedin",
     "search_term": "AI agents automation n8n",
     "location": "Mexico", "results_wanted": 50, "easy_apply": True},
    {"name": "10_F1_CRMAutomation_linkedin", "site_name": "linkedin",
     "search_term": "CRM automation API integrations",
     "location": "Mexico", "results_wanted": 50, "easy_apply": True},
    {"name": "11_F2_NetworkCloud_linkedin", "site_name": "linkedin",
     "search_term": "Network Engineer Junior Cloud Support AWS Linux",
     "location": "Mexico", "results_wanted": 50},
    {"name": "12_F3_HospitalitySaaS_linkedin", "site_name": "linkedin",
     "search_term": "Hospitality Technology PMS Onboarding",
     "location": "Remote", "results_wanted": 50, "easy_apply": True},
    {"name": "13_F4_AITrainer_linkedin", "site_name": "linkedin",
     "search_term": "AI Trainer Technical Enablement Developer Educator",
     "location": "Remote", "results_wanted": 50},
    {"name": "14_F4_KAMSaas_linkedin", "site_name": "linkedin",
     "search_term": "Account Manager SMB SaaS Customer Success",
     "location": "Mexico", "results_wanted": 50, "easy_apply": True},
    {"name": "15_F1_WhatsAppAutomation_linkedin", "site_name": "linkedin",
     "search_term": "WhatsApp automation CRM",
     "location": "Mexico", "results_wanted": 50},
    # --- Glassdoor (sin booleanos; country_indeed siempre; location estricta ciudad, país) ---
    {"name": "16_F1_MarketingAutomation_glassdoor", "site_name": "glassdoor",
     "search_term": "Marketing Automation Specialist",
     "location": "Mexico City, Mexico", "country_indeed": "Mexico", "is_remote": True, "results_wanted": 25},
    {"name": "17_F2_RevOps_glassdoor", "site_name": "glassdoor",
     "search_term": "RevOps Analyst",
     "location": "Mexico City, Mexico", "country_indeed": "Mexico", "is_remote": True, "results_wanted": 25},
    {"name": "18_F3_SolutionsConsultant_glassdoor", "site_name": "glassdoor",
     "search_term": "Implementation Consultant Solutions",
     "location": "Mexico City, Mexico", "country_indeed": "Mexico", "is_remote": True, "results_wanted": 25},
    {"name": "19_F4_AIAdoption_glassdoor", "site_name": "glassdoor",
     "search_term": "AI Adoption Specialist",
     "location": "Mexico City, Mexico", "country_indeed": "Mexico", "is_remote": True, "results_wanted": 25},
    {"name": "20_F2_DataOps_glassdoor", "site_name": "glassdoor",
     "search_term": "Data Operations Specialist Business Analyst",
     "location": "Mexico City, Mexico", "country_indeed": "Mexico", "is_remote": True, "results_wanted": 25},
]

# Validación interna según el spec
names = [p["name"] for p in profiles]
assert len(names) == len(set(names)), "names duplicados"
assert all(" " not in n for n in names), "name con espacios"
for p in profiles:
    assert p["site_name"] in ("indeed", "linkedin", "glassdoor", "zip_recruiter")
    assert p["site_name"] != "zip_recruiter", "ZipRecruiter prohibido para LATAM"
    for k in ("name", "site_name", "search_term", "location", "results_wanted"):
        assert k in p, f"falta {k} en {p['name']}"
    if p["site_name"] == "linkedin":
        assert p["results_wanted"] <= 100
        assert "OR" not in p["search_term"] and "AND" not in p["search_term"]
        if p.get("easy_apply"):
            assert "hours_old" not in p
    if p["site_name"] == "indeed":
        assert "country_indeed" in p
        assert p["location"] != "Remote"
        if p.get("is_remote") or "job_type" in p:
            assert "hours_old" not in p
    if p["site_name"] == "glassdoor":
        assert "country_indeed" in p
        assert ", " in p["location"]

try:
    with open(OUT) as f:
        _current = json.load(f)
except (OSError, ValueError):
    _current = []
if len(_current) > len(profiles) and "--force" not in sys.argv:
    sys.exit(f"{OUT} tiene {len(_current)} perfiles (incluye regionales). "
             "Usa --force para regenerar la base y luego corre build_regional_profiles.py.")

with open(OUT, "w") as f:
    json.dump(profiles, f, indent=2, ensure_ascii=False)
    f.write("\n")

# Revalidar parseo
with open(OUT) as f:
    loaded = json.load(f)
from collections import Counter
print("OK:", OUT)
print("perfiles:", len(loaded), "| por plataforma:", dict(Counter(p["site_name"] for p in loaded)))
