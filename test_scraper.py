"""Tests de resiliencia del scraper (sin red: scrape_site se sustituye por un doble).

Uso: .venv/bin/python -m unittest test_scraper   (o python3 test_scraper.py)
Ninguna prueba llama a JobSpy, a los sitios ni a Firecrawl, ni lee proxy-config.json.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scraper  # noqa: E402

E429 = "response status code 429 (blocked: use a <proxy>)"
E403 = "response status code 403 (blocked: use a <proxy>)"
ETUN = "Failed to perform, curl: (7) CONNECT tunnel failed, response 403."
FAKE_PROXIES = ["http://proxy-a.invalid:1", "http://proxy-b.invalid:2"]  # nunca se usan en red


def job(site="linkedin", name="p"):
    return {"profile": name, "site": site, "kwargs": {"search_term": "x", "site_name": [site]},
            "arista_ids": [1]}


class FakeSite:
    """Respuestas encoladas por llamada: (n_filas, error). Registra los proxies usados."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, kwargs, proxies=None, user_agent=None):
        self.calls.append(proxies)
        n, err = self.responses.pop(0)
        return [{"title": f"t{i}"} for i in range(n)], err


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


@mock.patch.object(scraper.time, "sleep", lambda s: None)
class RunSearchTests(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(scraper.classify_error(E429), "rate_limit")
        self.assertEqual(scraper.classify_error(E403), "blocked")
        self.assertEqual(scraper.classify_error(ETUN), "proxy")
        self.assertEqual(scraper.classify_error("timeout"), "other")
        self.assertIsNone(scraper.classify_error(None))

    def test_partial_rows_count_as_success_and_stop_retrying(self):
        fake = FakeSite([(28, E429)])
        br = scraper.CircuitBreaker(threshold=2)
        br.failures["linkedin"] = 1
        with mock.patch.object(scraper, "scrape_site", fake):
            rows, err, attempts = scraper.run_search(job(), br)
        self.assertEqual((len(rows), attempts), (28, 1))
        self.assertEqual(err, E429)
        self.assertEqual(br.failures["linkedin"], 0)
        self.assertFalse(br.is_open("linkedin"))

    def test_generic_error_retries_up_to_max(self):
        fake = FakeSite([(0, "timeout"), (0, "timeout"), (0, "timeout"), (0, "timeout")])
        with mock.patch.object(scraper, "scrape_site", fake):
            rows, err, attempts = scraper.run_search(job(), scraper.CircuitBreaker(threshold=99))
        self.assertEqual((rows, attempts), ([], 4))

    def test_blocked_no_backoff_retry_but_single_direct_probe(self):
        fake = FakeSite([(0, E403), (0, E403)])
        route = scraper.ProxyRoute(FAKE_PROXIES)
        with mock.patch.object(scraper, "scrape_site", fake):
            rows, err, attempts = scraper.run_search(job("indeed"), scraper.CircuitBreaker(threshold=99),
                                                     FAKE_PROXIES, route=route)
        self.assertEqual(attempts, 2)
        self.assertEqual(fake.calls, [FAKE_PROXIES, None])  # 2º intento sin proxies
        # Segunda búsqueda del mismo sitio: ya no se repite la prueba directa.
        fake2 = FakeSite([(0, E403)])
        with mock.patch.object(scraper, "scrape_site", fake2):
            _, _, attempts2 = scraper.run_search(job("indeed"), scraper.CircuitBreaker(threshold=99),
                                                 FAKE_PROXIES, route=route)
        self.assertEqual(attempts2, 1)

    def test_direct_probe_success_switches_route(self):
        fake = FakeSite([(0, ETUN), (12, None), (5, None)])
        route = scraper.ProxyRoute(FAKE_PROXIES)
        with mock.patch.object(scraper, "scrape_site", fake):
            rows, err, _ = scraper.run_search(job("glassdoor"), scraper.CircuitBreaker(), FAKE_PROXIES,
                                              route=route)
            self.assertEqual((len(rows), err), (12, None))
            self.assertIn("glassdoor", route.direct_sites)
            scraper.run_search(job("glassdoor"), scraper.CircuitBreaker(), FAKE_PROXIES, route=route)
        self.assertEqual(fake.calls, [FAKE_PROXIES, None, None])

    def test_blocked_without_proxies_single_attempt(self):
        fake = FakeSite([(0, E403)])
        with mock.patch.object(scraper, "scrape_site", fake):
            _, err, attempts = scraper.run_search(job("indeed"), scraper.CircuitBreaker(threshold=99))
        self.assertEqual((err, attempts), (E403, 1))

    def test_rate_limit_still_backs_off_and_recovers(self):
        fake = FakeSite([(0, E429), (0, E429), (50, None)])
        with mock.patch.object(scraper, "scrape_site", fake):
            rows, err, attempts = scraper.run_search(job(), scraper.CircuitBreaker())
        self.assertEqual((len(rows), err, attempts), (50, None, 3))


class BreakerTests(unittest.TestCase):
    def test_rate_limit_half_open_probe(self):
        clock = Clock()
        br = scraper.CircuitBreaker(threshold=2, cooldown=100, clock=clock)
        br.record_failure("linkedin", E429)
        self.assertTrue(br.record_failure("linkedin", E429))
        self.assertTrue(br.is_open("linkedin"))
        clock.t = 101
        self.assertFalse(br.is_open("linkedin"))  # prueba
        self.assertTrue(br.record_failure("linkedin", E429))  # prueba falla: reabre x2
        clock.t = 250
        self.assertTrue(br.is_open("linkedin"))
        clock.t = 302
        self.assertFalse(br.is_open("linkedin"))
        br.record_success("linkedin")
        self.assertFalse(br.is_open("linkedin"))
        self.assertNotIn("linkedin", br.open_causes)

    def test_blocked_stays_open(self):
        clock = Clock()
        br = scraper.CircuitBreaker(threshold=1, cooldown=100, clock=clock)
        br.record_failure("indeed", E403)
        clock.t = 10_000
        self.assertTrue(br.is_open("indeed"))


@mock.patch.object(scraper.time, "sleep", lambda s: None)
class Replay20261120(unittest.TestCase):
    """Secuencia real de LinkedIn del log 2026-11-20: con la lógica vieja el breaker se abría
    en el perfil 13 (28 filas devueltas) y se omitían 24 búsquedas. Ahora no debe abrirse."""

    def test_linkedin_sequence_does_not_open(self):
        seq = [(0, E429), (50, None),                       # perfil 9
               (0, E429), (0, E429), (50, None),            # 10
               (0, E429), (0, E429), (50, None),            # 11
               (0, E429), (0, E429), (0, E429), (10, E429),  # 12 (parcial al final)
               (28, E429)]                                  # 13 (parcial)
        fake = FakeSite(seq)
        br = scraper.CircuitBreaker()
        with mock.patch.object(scraper, "scrape_site", fake):
            for _ in range(5):
                scraper.run_search(job(), br)
        self.assertFalse(br.is_open("linkedin"))
        self.assertEqual(fake.responses, [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
