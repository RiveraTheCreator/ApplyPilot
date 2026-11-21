"""Tests del cableado del reapunte 2026-11-20 en filter_rank.py (caps senior / región).

Uso: .venv/bin/python -m unittest test_filter_rank   (o python3 test_filter_rank.py)
Solo lectura: carga keywords.json, filter-config.json (2.0.0) y
docs/filter-config-2.1.0-proposed.json; no abre vacantes.db ni escribe nada.
"""
from __future__ import annotations

import copy
import sys
import unittest
from datetime import date
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import filter_rank  # noqa: E402
from textutil import load_keywords, normalize  # noqa: E402

PROPOSED = BASE / "docs" / "filter-config-2.1.0-proposed.json"
TODAY = date(2026, 11, 21)

# Descripción remota con encaje fuerte genérico; el título es lo que varía.
DESC = ("Fully remote role. Work from anywhere. We use Salesforce, HubSpot, CRM, Zapier, "
        "Make, n8n, Python, automation, AI agents, customer success and onboarding. "
        "Immediate start.")


def make_row(titulo: str, ubicacion: str = "Remote", descripcion: str = DESC) -> dict:
    return {
        "id": 1, "titulo": titulo, "descripcion": descripcion, "ubicacion": ubicacion,
        "sueldo_min_mxn": None, "sueldo_max_mxn": None, "fecha_publicacion": "2026-11-20",
        "aristas": "[]", "modalidad": "remoto",
    }


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kw = load_keywords(BASE / "keywords.json")
        cls.fast_re = filter_rank.build_fast_regex(cls.kw)
        cls.cfg20 = filter_rank.load_filter_config(BASE / "filter-config.json")
        cls.cfg21 = filter_rank.load_filter_config(PROPOSED)
        # 2.0.0 "pura": la propuesta sin ninguna clave nueva (por si el real ya se actualizó).
        cfg = copy.deepcopy(cls.cfg21)
        cfg.pop("seniority"), cfg.pop("region")
        for k in ("seniority_title_penalty", "region_excluded_penalty"):
            cfg["weights"].pop(k)
        for k in ("seniority_title_cap", "region_excluded_cap"):
            cfg["stars"].pop(k)
        cls.cfg_stripped = cfg
        cls.senior_re = filter_rank.build_seniority_regex(cls.cfg21)
        cls.region_re, cls.countries = filter_rank.build_region_matcher(cls.cfg21)

    def score(self, row, cfg):
        return filter_rank.score_row(row, self.kw, cfg, self.fast_re, TODAY)

    def senior(self, title):
        return filter_rank.is_senior_title(normalize(title), self.senior_re)

    def region(self, title, loc):
        return filter_rank.is_region_excluded(normalize(title), normalize(loc),
                                              self.region_re, self.countries)


class TestSeniorityDetector(Base):
    def test_senior_titles(self):
        for t in ("Senior AI Engineer", "Staff Software Engineer, Product", "Principal AI Engineer",
                  "Chief Agentic AI Engineer", "Lead AI/ML Engineer", "AI Engineering Lead",
                  "Sr. Associate - S&M - Customer Success 5B", "Director of Operations",
                  "Head of Customer Success", "VP Sales", "Gen AI Software Engineer Senior"):
            self.assertTrue(self.senior(t), t)

    def test_not_senior(self):
        for t in ("Customer Success and Support Manager for Lead Generation Company",
                  "Account Manager SMB", "Technical Account Manager (Mexico)",
                  "Revenue Operations Manager (Hubspot & AI)", "AI Engineer",
                  "Seniority-agnostic Analyst", "Leader in automation"):
            self.assertFalse(self.senior(t), t)


class TestRegionDetector(Base):
    def test_excluded(self):
        cases = [
            ("Customer Success Manager, EMEA", "Remote"),
            ("Technology Education Specialist", "India"),
            ("AI Trainer - Software Engineering [MV001]", "Pune, India"),
            ("Advanced Specialist, Content Developer", "Poland"),
            ("Technical Account Manager (Remote Europe)", "Berlin Office; Austria"),
            ("Email Marketing Automation Specialist", "Remote (Asia Only)"),
            ("PH Technical Solutions Engineer", "Remote (Philippines)"),
            ("Analista", "Warszawa, Polonia"),
        ]
        for t, loc in cases:
            self.assertTrue(self.region(t, loc), (t, loc))

    def test_not_excluded(self):
        cases = [
            ("AI Engineer", "London, Ontario, Canada"),   # London, Ontario ≠ Londres, UK
            ("AI Engineer", "Remote"),                     # región desconocida = neutra
            ("AI Engineer", ""),
            ("Implementation Consultant", "Mexico City, Mexico"),
            ("Customer Success Manager", "Remote US"),
        ]
        for t, loc in cases:
            self.assertFalse(self.region(t, loc), (t, loc))

    def test_description_ignored(self):
        row = make_row("AI Engineer", "Remote",
                       DESC + " We hire in Germany, Poland, India and across EMEA and APAC.")
        res = self.score(row, self.cfg21)
        self.assertNotIn("region_excluded", res["flags"])


class TestComputeStarsCaps(Base):
    def test_caps_after_other_rules(self):
        s = self.cfg21["stars"]
        self.assertEqual(filter_rank.compute_stars("remoto", "strong", "target", True, False, s), 5)
        self.assertEqual(filter_rank.compute_stars("remoto", "strong", "target", True, False, s,
                                                   senior=True), 3)
        self.assertEqual(filter_rank.compute_stars("remoto", "strong", "target", True, False, s,
                                                   region_excluded=True), 2)
        self.assertEqual(filter_rank.compute_stars("remoto", "strong", "target", True, False, s,
                                                   senior=True, region_excluded=True), 2)
        # El cap nunca sube: 1★ sigue en 1★.
        self.assertEqual(filter_rank.compute_stars("hibrido", "none", "unknown", False, False, s,
                                                   senior=True), 1)

    def test_caps_absent_in_old_config(self):
        s = self.cfg_stripped["stars"]
        self.assertEqual(filter_rank.compute_stars("remoto", "strong", "target", True, False, s,
                                                   senior=True, region_excluded=True), 5)


class TestScoreRow(Base):
    def test_senior_row(self):
        base = self.score(make_row("AI Automation Engineer"), self.cfg21)
        sen = self.score(make_row("Senior AI Automation Engineer"), self.cfg21)
        self.assertGreaterEqual(base["stars"], 4)
        self.assertEqual(sen["stars"], min(3, base["stars"]))
        self.assertIn("senior_title", sen["flags"])
        self.assertNotIn("region_excluded", sen["flags"])
        self.assertEqual(sen["parts"]["seniority"], -10)
        self.assertEqual(sen["parts"]["region"], 0)
        self.assertAlmostEqual(sen["score"], base["score"] - 10)

    def test_sr_dot_and_lead_generation(self):
        sr = self.score(make_row("Sr. AI Automation Engineer"), self.cfg21)
        self.assertIn("senior_title", sr["flags"])
        self.assertLessEqual(sr["stars"], 3)
        lg = self.score(make_row("Customer Success Manager for Lead Generation Company"),
                        self.cfg21)
        self.assertNotIn("senior_title", lg["flags"])
        self.assertEqual(lg["parts"]["seniority"], 0)

    def test_region_row(self):
        res = self.score(make_row("AI Automation Engineer", "Remote - EMEA"), self.cfg21)
        self.assertIn("region_excluded", res["flags"])
        self.assertLessEqual(res["stars"], 2)
        self.assertEqual(res["parts"]["region"], -10)
        lo = self.score(make_row("AI Automation Engineer", "London, Ontario, Canada"), self.cfg21)
        self.assertNotIn("region_excluded", lo["flags"])

    def test_both_flags(self):
        res = self.score(make_row("Senior CSM - German Speaking (Remote)", "Berlin Office"),
                         self.cfg21)
        self.assertIn("senior_title", res["flags"])
        self.assertIn("region_excluded", res["flags"])
        self.assertLessEqual(res["stars"], 2)
        self.assertEqual(res["parts"]["seniority"] + res["parts"]["region"], -20)

    def test_score_floor_zero(self):
        row = make_row("Senior Something (EMEA)", "Poland", descripcion="")
        row["fecha_publicacion"] = "2020-01-01"
        res = self.score(row, self.cfg21)
        self.assertGreaterEqual(res["score"], 0.0)


class TestBackwardCompat(Base):
    """Config 2.0.0 (sin claves nuevas): sin caps, sin penalizaciones, sin flags, sin partes nuevas."""

    ROWS = [
        ("Senior AI Automation Engineer", "Remote - EMEA"),
        ("Staff Software Engineer, Product", "Poland"),
        ("Sr. Associate", "Pune, India"),
        ("AI Automation Engineer", "Remote"),
    ]

    def _check(self, cfg):
        for t, loc in self.ROWS:
            res = self.score(make_row(t, loc), cfg)
            self.assertNotIn("senior_title", res["flags"])
            self.assertNotIn("region_excluded", res["flags"])
            self.assertNotIn("seniority", res["parts"])
            self.assertNotIn("region", res["parts"])
            # Igual a la fórmula previa: min(100, suma) y compute_stars sin caps.
            self.assertEqual(res["score"], float(min(100, sum(res["parts"].values()))))
            plain = filter_rank.compute_stars(res["modality"], res["fit"], res["salary"],
                                              res["fast"] or res["recent"],
                                              "visa_relocacion" in res["flags"], cfg["stars"])
            self.assertEqual(res["stars"], plain)

    def test_stripped_config_noop(self):
        self._check(self.cfg_stripped)

    def test_real_config_noop_if_still_200(self):
        if "seniority" in self.cfg20 or "region" in self.cfg20:
            self.skipTest("filter-config.json real ya trae seniority/region (≥ 2.1.0)")
        self._check(self.cfg20)

    def test_senior_row_keeps_stars_on_old_config(self):
        old = self.score(make_row("Senior AI Automation Engineer"), self.cfg_stripped)
        plain = self.score(make_row("AI Automation Engineer"), self.cfg_stripped)
        self.assertEqual(old["stars"], plain["stars"])


class TestReportGrouping(unittest.TestCase):
    """El top del reporte muestra cada empresa+título una sola vez (solo presentación)."""

    @staticmethod
    def item(i, titulo, empresa, stars=4, score=30.0):
        return {"id": i, "titulo": titulo, "empresa": empresa, "stars": stars, "score": score,
                "sueldo": None, "fuente": "linkedin", "modality": "remoto", "aristas": [1],
                "url": f"https://example.invalid/{i}"}

    def test_group_and_top(self):
        ranked = [self.item(1, "Staff Software Engineer, Product", "LawnStarter", 4, 36),
                  self.item(2, "Staff Software Engineer, Product", "LawnStarter", 4, 36),
                  self.item(3, "Technical Account Manager", "Samsara", 4, 30),
                  self.item(4, "staff software engineer - product", "Lawnstarter", 4, 20),
                  self.item(5, "AI Engineer", "Acme", 3, 25)]
        result = {"ranked": ranked, "evaluated": 5, "passed": 5, "dropped": 0,
                  "stars": {4: 4, 3: 1}, "drop_reasons": {}}
        groups = filter_rank.group_ranked(ranked)
        self.assertEqual([[i["id"] for i in g] for g in groups], [[1, 2, 4], [3], [5]])
        out = filter_rank.format_report(result, top=2)
        self.assertIn("(+2 publicaciones más del mismo rol: ids 2, 4)", out)
        self.assertIn("Samsara", out)
        self.assertNotIn("Acme", out)
        self.assertIn("... y 1 vacantes distintas más", out)
        self.assertIn("publicaciones repetidas: 2 en 1 grupos", out)
        self.assertIn("Por estrellas: 5★ 0 | 4★ 4 | 3★ 1", out)  # conteos por fila, sin cambio


if __name__ == "__main__":
    unittest.main(verbosity=2)
