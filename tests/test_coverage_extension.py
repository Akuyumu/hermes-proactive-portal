"""Extension-point / coverage audit wiring checks (staged gap #4).

No network, no invented endpoints: this only asserts the documented integration
seam really exists and that unsupported coverage stays unsupported.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import coverage_ext as C  # noqa: E402
from proactive_pilot import discovery, publicfeeds, publictransport  # noqa: E402


class CoverageExtensionTests(unittest.TestCase):
    def test_generic_integration_seam_functions_exist(self):
        self.assertTrue(callable(publicfeeds.parse_source))
        self.assertTrue(callable(publicfeeds.validate_public_url))
        self.assertTrue(callable(publictransport.vet_public_url))
        self.assertTrue(callable(publictransport.PinnedHTTPSTransport))
        self.assertTrue(callable(discovery.classify_hit))
        self.assertTrue(callable(discovery.vet_discovered_url))

    def test_opportunity_is_supported_via_generic_route(self):
        entry = C.coverage_status("opportunity")
        self.assertEqual(entry["status"], "supported_generic")
        self.assertTrue(set(entry["route"]).issubset(set(C.GENERIC_ROUTES)))

    def test_forecast_and_planned_notice_are_supported_but_live_transport_is_not(self):
        self.assertEqual(C.coverage_status("weather")["status"], "supported_fixed_official")
        self.assertEqual(C.coverage_status("planned_transport")["status"], "supported_planned_only")
        self.assertEqual(C.coverage_status("live_transport")["status"], "unsupported")
        self.assertEqual(C.unsupported_categories(), ["live_transport"])

    def test_unknown_category_is_unsupported(self):
        self.assertEqual(C.coverage_status("teleportation")["status"], "unsupported")

    def test_wired_kinds_are_the_expected_generic_routes(self):
        self.assertTrue(C.is_supported_kind("web_page"))
        self.assertTrue(C.is_supported_kind("web_json"))
        self.assertTrue(C.is_supported_kind("rss_atom"))
        self.assertTrue(C.is_supported_kind("nea_forecast"))
        self.assertTrue(C.is_supported_kind("smrt_notice"))
        self.assertFalse(C.is_supported_kind("weather_api"))
        self.assertFalse(C.is_supported_kind("live_transport"))


if __name__ == "__main__":
    unittest.main()
