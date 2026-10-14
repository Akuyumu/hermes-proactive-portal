"""Official credentialed LTA DataMall TrainServiceAlerts source tests.

No network: a fake credentialed transport and a fake key sentinel are injected
(the key is only ever read from an injected mapping, never the real environment).
Covered: status-1 all-clear marker, status-2 incident mapping, missing key
fail-closed, host/path/redirect/port/scheme rejection, body cap, malformed JSON,
non-JSON content type, and credential redaction from evidence and exception text.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.policy import load_policy  # noqa: E402
from proactive_pilot.publicfeeds import (  # noqa: E402
    CredentialMissing,
    FeedFailed,
    FetchFailed,
    FetchResponse,
    LTA_ACCOUNT_KEY_ENV,
    LTA_ALL_CLEAR_ID,
    LTA_DATAMALL_HOST,
    LTA_TRAIN_ALERTS_PATH,
    LTA_TRAIN_ALERTS_URL,
    URLRejected,
    _RefuseRedirects,
    _redact_credential,
    collect_public_feeds,
    content_type_allowed,
    fetch_lta_train_alerts,
    parse_lta_train_alerts,
    validate_public_url,
)
from proactive_pilot.state import StateStore  # noqa: E402

FAKE_KEY = "FAKE-LTA-KEY-SENTINEL-do-not-use"
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)

STATUS1 = json.dumps({
    "odata.metadata": "https://datamall2.mytransport.sg/ltaodataservice/$metadata#TrainServicesAlerts",
    "value": {"Status": 1, "AffectedSegments": [], "Message": []},
}).encode("utf-8")

STATUS2 = json.dumps({
    "odata.metadata": "https://datamall2.mytransport.sg/ltaodataservice/$metadata#TrainServicesAlerts",
    "value": {
        "Status": 2,
        "AffectedSegments": [
            {"Line": "NSL", "Direction": "Both", "Stations": ["NS1", "NS2"]},
        ],
        "Message": [
            {"Content": "Train service on NSL is delayed due to a signal fault.",
             "CreatedDate": "2026-10-15T11:30:00+08:00"},
        ],
    },
}).encode("utf-8")


class _FakeResponse:
    def __init__(self, body, status=200, headers=None, url=LTA_TRAIN_ALERTS_URL):
        self._body = body
        self.status = status
        self.headers = headers if headers is not None else {"Content-Type": "application/json"}
        self.url = url

    def read(self, n=-1):
        if n is None or n < 0:
            return self._body
        return self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpener:
    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.requests = []
        self.timeouts = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self.exc is not None:
            raise self.exc
        return self.response


def _env(key=FAKE_KEY):
    return {LTA_ACCOUNT_KEY_ENV: key}


def _lta_policy():
    policy = load_policy(None)
    policy["feed_seed_sources"] = [{
        "kind": "lta_train_alerts",
        "url": LTA_TRAIN_ALERTS_URL,
        "provenance": "test seed",
        "interest": "lta",
        "cadence_minutes": 60,
    }]
    return policy


class LtaTrainAlertsTests(unittest.TestCase):

    # -- parser: status 1 / status 2 ----------------------------------------

    def test_status_1_is_explicit_all_clear_marker(self):
        items = parse_lta_train_alerts(STATUS1, now=NOW)
        self.assertEqual(len(items), 1)
        row = items[0]
        self.assertTrue(row["all_clear"])
        self.assertEqual(row["service_status"], "normal")
        self.assertEqual(row["category"], "live_transport")
        self.assertEqual(row["id"], LTA_ALL_CLEAR_ID)
        self.assertEqual(row["link"], LTA_TRAIN_ALERTS_URL)
        self.assertIn("all-clear", row["title"].lower())

    def test_status_2_maps_each_affected_segment_and_message(self):
        items = parse_lta_train_alerts(STATUS2, now=NOW)
        self.assertEqual(len(items), 2)
        self.assertTrue(any("NSL" in i["summary"] for i in items))
        self.assertTrue(any("signal fault" in i["summary"] for i in items))
        for row in items:
            self.assertFalse(row["all_clear"])
            self.assertEqual(row["service_status"], "disruption")
            self.assertEqual(row["category"], "live_transport")
            self.assertTrue(row["live_service_alert"])

    def test_status_2_sanitizes_and_bounds_untrusted_fields(self):
        body = json.dumps({
            "value": {
                "Status": 2,
                "AffectedSegments": [{"Line": "<script>alert(1)</script>",
                                      "Stations": ["<b>NS1</b>"]}],
                "Message": [{"Content": "<b>x</b>" + "A" * 1000}],
            }
        }).encode("utf-8")
        items = parse_lta_train_alerts(body, now=NOW)
        self.assertTrue(items)
        for row in items:
            self.assertNotIn("<script", row["summary"])
            self.assertNotIn("alert(1)", row["summary"])
            self.assertLessEqual(len(row["summary"]), 500)
            self.assertLessEqual(len(row["title"]), 220)

    def test_status_2_max_items_is_bounded(self):
        body = json.dumps({
            "value": {
                "Status": 2,
                "AffectedSegments": [{"Line": f"L{i}"} for i in range(50)],
                "Message": [],
            }
        }).encode("utf-8")
        self.assertLessEqual(len(parse_lta_train_alerts(body, now=NOW, max_items=3)), 3)

    # -- parser: fail closed on malformed / unexpected ----------------------

    def test_malformed_and_unexpected_shapes_fail_closed(self):
        for body in (
            b"not json",
            b"[]",
            b"{}",                                                   # no value
            b'{"value": 5}',                                         # value not object
            b'{"value": {"Status": 1}}',                             # arrays missing
            b'{"value": {"Status": true, "AffectedSegments": [], "Message": []}}',
            b'{"value": {"Status": "1", "AffectedSegments": [], "Message": []}}',
            b'{"value": {"Status": 7, "AffectedSegments": [], "Message": []}}',
            b'{"value": {"Status": 2, "AffectedSegments": [], "Message": []}}',  # status 2, no detail
            b'{"value": {"Status": 2, "AffectedSegments": [{}], "Message": [{"Content": ""}]}}',
        ):
            with self.subTest(body=body):
                with self.assertRaises(FeedFailed):
                    parse_lta_train_alerts(body, now=NOW)

    # -- credentialed fetch: key handling -----------------------------------

    def test_key_missing_fails_closed_without_network(self):
        opener = _FakeOpener(response=_FakeResponse(STATUS1))
        with self.assertRaises(CredentialMissing) as ctx:
            fetch_lta_train_alerts(environ={}, opener=opener)
        self.assertIn(LTA_ACCOUNT_KEY_ENV, str(ctx.exception))
        self.assertFalse(opener.requests, "no network call may be attempted without a key")

    def test_empty_key_fails_closed_without_network(self):
        opener = _FakeOpener(response=_FakeResponse(STATUS1))
        with self.assertRaises(CredentialMissing):
            fetch_lta_train_alerts(environ={LTA_ACCOUNT_KEY_ENV: "   "}, opener=opener)
        self.assertFalse(opener.requests)

    def test_key_is_sent_only_as_a_header_never_in_url(self):
        opener = _FakeOpener(response=_FakeResponse(STATUS1))
        response = fetch_lta_train_alerts(environ=_env(), opener=opener)
        self.assertEqual(response.status, 200)
        req = opener.requests[0]
        self.assertNotIn(FAKE_KEY, req.full_url)
        self.assertNotIn("?", req.full_url)
        self.assertIn(FAKE_KEY, " ".join(str(v) for v in req.headers.values()))

    # -- credentialed fetch: host/path/redirect/port/scheme -----------------

    def test_host_path_port_scheme_are_rejected(self):
        validate_public_url(LTA_TRAIN_ALERTS_URL)  # the one exact endpoint passes
        for bad in (
            f"https://{LTA_DATAMALL_HOST}{LTA_TRAIN_ALERTS_PATH}/extra",
            f"https://{LTA_DATAMALL_HOST}{LTA_TRAIN_ALERTS_PATH}X",
            f"https://{LTA_DATAMALL_HOST}/ltaodataservice/Other",
            "https://evil.example/ltaodataservice/TrainServiceAlerts",
            f"http://{LTA_DATAMALL_HOST}{LTA_TRAIN_ALERTS_PATH}",
            f"https://{LTA_DATAMALL_HOST}:8443{LTA_TRAIN_ALERTS_PATH}",
            f"https://user:pass@{LTA_DATAMALL_HOST}{LTA_TRAIN_ALERTS_PATH}",
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(URLRejected):
                    validate_public_url(bad)

    def test_query_string_is_refused_before_network(self):
        opener = _FakeOpener(response=_FakeResponse(STATUS1))
        with self.assertRaises(URLRejected):
            fetch_lta_train_alerts(environ=_env(), opener=opener,
                                   url=LTA_TRAIN_ALERTS_URL + "?foo=bar")
        self.assertFalse(opener.requests)

    def test_redirects_are_refused(self):
        with self.assertRaises(FeedFailed):
            _RefuseRedirects().redirect_request(
                None, None, 302, "Found", {}, "https://evil.example/")

    def test_off_host_final_url_is_rejected(self):
        opener = _FakeOpener(response=_FakeResponse(
            STATUS1, url="https://evil.example/ltaodataservice/TrainServiceAlerts"))
        with self.assertRaises(URLRejected):
            fetch_lta_train_alerts(environ=_env(), opener=opener)

    # -- credentialed fetch: bounds and content type ------------------------

    def test_oversized_body_is_capped(self):
        body = STATUS1 + b" " * 5000
        opener = _FakeOpener(response=_FakeResponse(body))
        response = fetch_lta_train_alerts(environ=_env(), opener=opener, max_bytes=100)
        self.assertLessEqual(len(response.body), 100)

    def test_non_json_content_type_is_refused(self):
        self.assertFalse(content_type_allowed("lta_train_alerts", "text/html"))
        self.assertFalse(content_type_allowed("lta_train_alerts", "application/octet-stream"))
        self.assertTrue(content_type_allowed("lta_train_alerts", "application/json"))
        self.assertTrue(content_type_allowed("lta_train_alerts", "application/json; charset=utf-8"))

    # -- no credential leakage ----------------------------------------------

    def test_no_credential_reaches_evidence_or_exception_text(self):
        items = parse_lta_train_alerts(STATUS2, now=NOW)
        self.assertNotIn(FAKE_KEY, json.dumps(items))
        opener = _FakeOpener(exc=urllib.error.URLError(FAKE_KEY))
        with self.assertRaises(FeedFailed) as ctx:
            fetch_lta_train_alerts(environ=_env(), opener=opener)
        self.assertNotIn(FAKE_KEY, str(ctx.exception))
        self.assertIn("[redacted]", str(ctx.exception))
        # The account header name is redacted too.
        self.assertNotIn("AccountKey", _redact_credential("AccountKey: secret", FAKE_KEY))

    # -- end-to-end collection through the registry -------------------------

    def test_collect_wires_credentialed_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
            policy = _lta_policy()
            calls = []

            def fake_cred(*, timeout=None, max_bytes=None):
                calls.append((timeout, max_bytes))
                return FetchResponse(200, {"Content-Type": "application/json"},
                                     STATUS2, LTA_TRAIN_ALERTS_URL)

            def unused_fetcher(*a, **k):
                raise FetchFailed("generic fetcher must not be used for lta")

            results = collect_public_feeds(
                policy, SeededClock(NOW.isoformat()), store=store,
                fetcher=unused_fetcher, credentialed_fetcher=fake_cred)
            lta = [r for r in results if r.name.startswith("feed:lta_train_alerts")]
            self.assertTrue(lta, results)
            self.assertTrue(lta[0].healthy, lta[0].error)
            self.assertTrue(calls)
            self.assertTrue(any(i["category"] == "live_transport" for i in lta[0].items))
            self.assertTrue(all(not i["all_clear"] for i in lta[0].items))

    def test_collect_fails_closed_when_key_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
            policy = _lta_policy()

            def unused_fetcher(*a, **k):
                raise FetchFailed("generic fetcher must not be used for lta")

            with mock.patch.dict(os.environ, {LTA_ACCOUNT_KEY_ENV: ""}):
                results = collect_public_feeds(
                    policy, SeededClock(NOW.isoformat()), store=store,
                    fetcher=unused_fetcher)  # no credentialed_fetcher -> real path
            lta = [r for r in results if r.name.startswith("feed:lta_train_alerts")]
            self.assertTrue(lta, results)
            self.assertFalse(lta[0].healthy)
            self.assertIn(LTA_ACCOUNT_KEY_ENV, (lta[0].error or lta[0].failure))


if __name__ == "__main__":
    unittest.main()
