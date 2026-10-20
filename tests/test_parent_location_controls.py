"""Stage-only local phone-context controls; synthetic + injected fake HTTP only.

Runs with unittest discover (no pytest / no network):
  PYTHONPATH=src python3 -m unittest discover -s tests

Every test injects an explicit fake HTTP responder and fake evaluator. Nothing
here reads Find Hub, personal sources, credentials, real state, jobs or sends.
Coordinates used are synthetic and must never escape into packet/state/log.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import location as L  # noqa: E402
from proactive_pilot import main as M  # noqa: E402
from proactive_pilot.candidates import build_packet, packet_digest  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.collectors import SourceResult  # noqa: E402
from proactive_pilot.evaluator import EvaluatorResult, FakeEvaluator  # noqa: E402
from proactive_pilot.policy import DEFAULT_POLICY  # noqa: E402

T = SeededClock("2026-10-03T09:00:00+08:00")
EVENT_START = "2026-10-03T09:30:00+08:00"
FIX_TS = "2026-10-03T09:00:00+08:00"
VENUE = "Clinic, 12 Orchard Road"
UNIQUE_LAT = 12.34567
UNIQUE_LON = 98.76543
UNIQUE_ADDRESS = "999 Secret Lane, Nowhere"
RAW_MARKER = "RAW_RESPONSE_MARKER_XYZ"


def policy(enabled: bool = True, **over) -> dict:
    p = json.loads(json.dumps(DEFAULT_POLICY))
    p["timezone"] = "Asia/Singapore"
    p["location_context_enabled"] = enabled
    p.update(over)
    return p


def calendar(items, *, status: str = "ok", name: str = "calendar:primary") -> SourceResult:
    return SourceResult(name, status, count=len(items), items=list(items),
                        checked_at="2026-10-03T09:00:00+08:00")


def event(*, start: str = EVENT_START, title: str = "Dentist appointment",
          status: str = "confirmed", all_day: bool = False, location: str = VENUE,
          ref: str = "evt:primary:e1", **extra) -> dict:
    item = {"ref": ref, "id": ref.rsplit(":", 1)[-1], "title": title, "start": start,
            "end": "2026-10-03T10:00:00+08:00", "all_day": all_day, "status": status,
            "calendar": "primary", "calendar_summary": "Primary", "location": location,
            "updated": "2026-10-03T08:00:00+08:00", "source": "primary"}
    item.update(extra)
    return item


def device_payload(*, location=None, device_id: str = L.PHONE_DEVICE_ID,
                   own=True, last_seen=None) -> dict:
    if location is None:
        location = {"latitude": 1.3, "longitude": 103.8, "accuracy": 25.0,
                    "timestamp": FIX_TS, "source": "findhub", "cached": False}
    return {"id": device_id, "name": "Phone", "type": "smartphone",
            "last_seen": last_seen, "location": location, "is_own_device": own}


class FakeGetter:
    def __init__(self, *, payload=None, status: int = 200, body: str | None = None,
                 error: str = "", raises: BaseException | None = None):
        self.payload = payload
        self.status = status
        self.body = body
        self.error = error
        self.raises = raises
        self.urls: list[str] = []
        self.timeouts: list[float] = []

    def __call__(self, url: str, *, timeout: float, max_bytes: int) -> L.LocationHttpResponse:
        self.urls.append(url)
        self.timeouts.append(timeout)
        if self.raises is not None:
            raise self.raises
        if self.body is not None:
            return L.LocationHttpResponse(status=self.status, body=self.body, error=self.error)
        return L.LocationHttpResponse(status=self.status, body=json.dumps(self.payload), error=self.error)


def run(results, *, getter=None, pol=None, prefs=None, state=None, clock=T,
        destination=None, enabled=True, deadline_seconds=L.HTTP_DEADLINE_SECONDS):
    return L.collect_location_context(
        results, clock=clock, policy=pol if pol is not None else policy(enabled),
        prefs=prefs or {}, state=state if state is not None else {},
        http_get=getter, destination=destination, deadline_seconds=deadline_seconds,
    )


def run_main(tmp: Path, *, fixture: str, getter, enabled: bool = True,
             evaluator=None, over: dict | None = None, prefs: dict | None = None,
             clock_str: str = "2026-10-03T09:00:00+08:00"):
    pol = tmp / "policy.json"
    body = {"location_context_enabled": enabled, "timezone": "Asia/Singapore"}
    if over:
        body.update(over)
    pol.write_text(json.dumps(body), encoding="utf-8")
    argv = ["--state-dir", str(tmp / "state"), "--policy", str(pol),
            "--seeded-clock", clock_str, "--fixture", fixture]
    if prefs is not None:
        pref_path = tmp / "prefs.json"
        pref_path.write_text(json.dumps(prefs), encoding="utf-8")
        argv += ["--preferences", str(pref_path)]
    args = M.build_parser().parse_args(argv)
    ev = evaluator or FakeEvaluator(EvaluatorResult("silence"))
    rc = M.run_once(args, evaluator=ev, location_http=getter)
    return rc, ev


class LocationControls(unittest.TestCase):
    # -- gating: zero HTTP ----------------------------------------------------
    def test_disabled_by_default_zero_http(self):
        g = FakeGetter(payload=device_payload())
        ctx = run([calendar([event()])], getter=g, enabled=False)
        self.assertEqual(ctx.status, "disabled")
        self.assertEqual(g.urls, [])

    def test_no_calendar_source_zero_http(self):
        g = FakeGetter(payload=device_payload())
        self.assertEqual(run([], getter=g).status, "calendar unavailable")
        self.assertEqual(g.urls, [])

    def test_calendar_error_zero_http(self):
        g = FakeGetter(payload=device_payload())
        self.assertEqual(run([calendar([], status="error")], getter=g).status, "calendar unavailable")
        self.assertEqual(g.urls, [])

    def test_no_qualifying_event_zero_http(self):
        g = FakeGetter(payload=device_payload())
        ctx = run([calendar([event(start="2026-10-03T14:00:00+08:00")])], getter=g)
        self.assertEqual(ctx.status, "no qualifying event")
        self.assertEqual(g.urls, [])

    def test_missing_venue_zero_http(self):
        g = FakeGetter(payload=device_payload())
        ctx = run([calendar([event(location="")])], getter=g)
        self.assertEqual(ctx.status, "no qualifying event")
        self.assertEqual(g.urls, [])

    def test_ambiguous_venue_zero_http(self):
        g = FakeGetter(payload=device_payload())
        ctx = run([calendar([event(location="TBD")])], getter=g)
        self.assertEqual(ctx.status, "no qualifying event")
        self.assertEqual(g.urls, [])

    def test_url_only_venue_zero_http(self):
        g = FakeGetter(payload=device_payload())
        ctx = run([calendar([event(location="https://example.com/campus-event")])], getter=g)
        self.assertEqual(ctx.status, "no qualifying event")
        self.assertEqual(g.urls, [])

    def test_generic_url_in_description_is_not_remote(self):
        # A physical venue plus a generic campus-event URL in the description is
        # still an in-person event and remains eligible.
        g = FakeGetter(payload=device_payload())
        ev = event(location=VENUE, description="Details: https://example.com/campus/event/1")
        ctx = run([calendar([ev])], getter=g)
        self.assertTrue(ctx.available)
        self.assertEqual(g.urls, [L.FORCE_URL])

    def test_online_event_zero_http(self):
        g = FakeGetter(payload=device_payload())
        ctx = run([calendar([event(title="Zoom sync", location="https://meet.google.com/abc")])], getter=g)
        self.assertEqual(ctx.status, "no qualifying event")
        self.assertEqual(g.urls, [])

    def test_cancelled_event_zero_http(self):
        g = FakeGetter(payload=device_payload())
        self.assertEqual(run([calendar([event(status="cancelled")])], getter=g).status, "no qualifying event")
        self.assertEqual(g.urls, [])

    def test_all_day_event_zero_http(self):
        g = FakeGetter(payload=device_payload())
        ev = event(all_day=True, start="2026-10-03T00:00:00+08:00")
        self.assertEqual(run([calendar([ev])], getter=g).status, "no qualifying event")
        self.assertEqual(g.urls, [])

    def test_date_only_start_zero_http(self):
        g = FakeGetter(payload=device_payload())
        self.assertEqual(run([calendar([event(start="2026-10-03", all_day=False)])], getter=g).status,
                         "no qualifying event")
        self.assertEqual(g.urls, [])

    def test_ambiguous_title_zero_http(self):
        g = FakeGetter(payload=device_payload())
        self.assertEqual(run([calendar([event(title="(untitled event)")])], getter=g).status,
                         "no qualifying event")
        self.assertEqual(g.urls, [])

    def test_explicit_geo_without_venue_is_eligible(self):
        g = FakeGetter(payload=device_payload())
        ev = event(location="", geo={"latitude": 1.29, "longitude": 103.85})
        ctx = run([calendar([ev])], getter=g)
        self.assertTrue(ctx.available)
        self.assertEqual(g.urls, [L.FORCE_URL])

    def test_quiet_hours_zero_http(self):
        g = FakeGetter(payload=device_payload())
        night = SeededClock("2026-10-03T03:00:00+08:00")
        ctx = run([calendar([event(start="2026-10-03T03:30:00+08:00")])], getter=g, clock=night)
        self.assertEqual(ctx.status, "quiet hours")
        self.assertEqual(g.urls, [])

    def test_global_pause_zero_http(self):
        g = FakeGetter(payload=device_payload())
        ctx = run([calendar([event()])], getter=g, prefs={"paused": {"active": True}})
        self.assertEqual(ctx.status, "global pause")
        self.assertEqual(g.urls, [])

    def test_insufficient_budget_defers_without_http(self):
        g = FakeGetter(payload=device_payload())
        ctx = run([calendar([event()])], getter=g, deadline_seconds=0.5)
        self.assertEqual(ctx.status, "budget deferred")
        self.assertEqual(g.urls, [])

    # -- fixed endpoint + earliest binding + cadence -------------------------
    def test_forced_endpoint_is_fixed_phone_only(self):
        g = FakeGetter(payload=device_payload())
        ctx = run([calendar([event()])], getter=g)
        self.assertTrue(ctx.available and ctx.fresh)
        self.assertEqual(ctx.event_ref, "evt:primary:e1")
        self.assertEqual(g.urls, [L.FORCE_URL])
        self.assertEqual(L.FORCE_URL, f"{L.LOCAL_ORIGIN}{L.PHONE_PATH}?force=true")

    def test_earliest_event_binds_and_later_geo_not_used(self):
        g = FakeGetter(payload=device_payload())
        early = event(ref="evt:primary:early", start="2026-10-03T09:20:00+08:00",
                      title="Standup", location=VENUE)
        late = event(ref="evt:primary:late", start="2026-10-03T10:20:00+08:00",
                     title="Late venue", location="", geo={"latitude": 1.29, "longitude": 103.85})
        ctx = run([calendar([late, early])], getter=g)
        self.assertEqual(ctx.event_ref, "evt:primary:early")
        self.assertFalse(ctx.destination_resolved)
        self.assertIsNone(ctx.distance_m)
        self.assertEqual(g.urls, [L.FORCE_URL])

    def test_deadline_clamped_and_passed_to_getter(self):
        g = FakeGetter(payload=device_payload())
        run([calendar([event()])], getter=g, deadline_seconds=3.0)
        self.assertEqual(len(g.timeouts), 1)
        self.assertLessEqual(g.timeouts[0], 3.0)
        self.assertLessEqual(g.timeouts[0], L.HTTP_DEADLINE_SECONDS)

    def test_force_cadence_blocks_repeat_within_window(self):
        g = FakeGetter(payload=device_payload())
        state: dict = {}
        self.assertTrue(run([calendar([event()])], getter=g, state=state).available)
        self.assertEqual(len(g.urls), 1)
        second = run([calendar([event()])], getter=g, state=state)
        self.assertEqual(second.status, "cadence")
        self.assertEqual(len(g.urls), 1)
        later = SeededClock("2026-10-03T09:16:00+08:00")
        g.payload = device_payload(location={
            "latitude": 1.3, "longitude": 103.8, "accuracy": 25.0,
            "timestamp": "2026-10-03T09:15:00+08:00"})
        third = run([calendar([event()])], getter=g, state=state, clock=later)
        self.assertTrue(third.available)
        self.assertEqual(len(g.urls), 2)

    def test_state_records_only_attempt_timestamp(self):
        g = FakeGetter(payload=device_payload())
        state: dict = {}
        run([calendar([event()])], getter=g, state=state)
        self.assertEqual(list(state.keys()), [L.LOCATION_ATTEMPTS_KEY])
        self.assertEqual(list(state[L.LOCATION_ATTEMPTS_KEY].keys()), [L.PHONE_DEVICE_ID])
        self.assertEqual(state[L.LOCATION_ATTEMPTS_KEY][L.PHONE_DEVICE_ID], T.now().isoformat())

    # -- fail-closed response handling ---------------------------------------
    def test_null_location_fail_closed(self):
        g = FakeGetter(payload=device_payload(location=None))
        g.payload["location"] = None
        ctx = run([calendar([event()])], getter=g)
        self.assertFalse(ctx.available)
        self.assertEqual(ctx.status, "location unavailable")

    def test_missing_identity_fail_closed(self):
        payload = device_payload()
        payload.pop("id")
        ctx = run([calendar([event()])], getter=FakeGetter(payload=payload))
        self.assertEqual(ctx.status, "missing identity")

    def test_not_own_device_fail_closed(self):
        ctx = run([calendar([event()])], getter=FakeGetter(payload=device_payload(own=False)))
        self.assertEqual(ctx.status, "not own device")

    def test_wrong_device_fail_closed(self):
        other = device_payload(device_id="aaaaaaaa-0000-0000-0000-000000000000")
        self.assertEqual(run([calendar([event()])], getter=FakeGetter(payload=other)).status, "wrong device")

    def test_missing_time_fail_closed(self):
        loc = {"latitude": 1.3, "longitude": 103.8, "accuracy": 25.0}
        ctx = run([calendar([event()])], getter=FakeGetter(payload=device_payload(location=loc)))
        self.assertEqual(ctx.status, "location missing time")

    def test_naive_timestamp_fail_closed(self):
        loc = {"latitude": 1.3, "longitude": 103.8, "accuracy": 25.0,
               "timestamp": "2026-10-03T09:00:00"}
        ctx = run([calendar([event()])], getter=FakeGetter(payload=device_payload(location=loc)))
        self.assertEqual(ctx.status, "location missing time")

    def test_date_only_timestamp_fail_closed(self):
        loc = {"latitude": 1.3, "longitude": 103.8, "accuracy": 25.0, "timestamp": "2026-10-03"}
        ctx = run([calendar([event()])], getter=FakeGetter(payload=device_payload(location=loc)))
        self.assertEqual(ctx.status, "location missing time")

    def test_stale_fail_closed(self):
        loc = {"latitude": 1.3, "longitude": 103.8, "accuracy": 25.0,
               "timestamp": "2026-10-03T08:00:00+08:00"}
        ctx = run([calendar([event()])], getter=FakeGetter(payload=device_payload(location=loc)))
        self.assertEqual(ctx.status, "location stale")
        self.assertFalse(ctx.fresh)

    def test_bad_accuracy_fail_closed(self):
        loc = {"latitude": 1.3, "longitude": 103.8, "accuracy": 500.0, "timestamp": FIX_TS}
        ctx = run([calendar([event()])], getter=FakeGetter(payload=device_payload(location=loc)))
        self.assertEqual(ctx.status, "location bad accuracy")

    def test_bool_accuracy_fail_closed(self):
        loc = {"latitude": 1.3, "longitude": 103.8, "accuracy": True, "timestamp": FIX_TS}
        ctx = run([calendar([event()])], getter=FakeGetter(payload=device_payload(location=loc)))
        self.assertEqual(ctx.status, "location parse error")

    def test_bool_coordinate_fail_closed(self):
        loc = {"latitude": True, "longitude": 103.8, "accuracy": 25.0, "timestamp": FIX_TS}
        ctx = run([calendar([event()])], getter=FakeGetter(payload=device_payload(location=loc)))
        self.assertEqual(ctx.status, "location parse error")

    def test_out_of_bounds_fail_closed(self):
        loc = {"latitude": 999.0, "longitude": 103.8, "accuracy": 25.0, "timestamp": FIX_TS}
        ctx = run([calendar([event()])], getter=FakeGetter(payload=device_payload(location=loc)))
        self.assertEqual(ctx.status, "location out of bounds")

    def test_nan_fail_closed(self):
        loc = {"latitude": float("nan"), "longitude": 103.8, "accuracy": 25.0, "timestamp": FIX_TS}
        ctx = run([calendar([event()])], getter=FakeGetter(payload=device_payload(location=loc)))
        self.assertEqual(ctx.status, "location parse error")

    def test_time_future_fail_closed(self):
        loc = {"latitude": 1.3, "longitude": 103.8, "accuracy": 25.0,
               "timestamp": "2026-10-03T12:00:00+08:00"}
        ctx = run([calendar([event()])], getter=FakeGetter(payload=device_payload(location=loc)))
        self.assertEqual(ctx.status, "location time future")

    def test_status_error_fail_closed(self):
        g = FakeGetter(status=500, body="server error latitude=1.3 longitude=103.8")
        ctx = run([calendar([event()])], getter=g)
        self.assertFalse(ctx.available)
        self.assertEqual(ctx.status, "location status 500")
        self.assertNotIn("1.3", json.dumps(ctx.to_packet()))

    def test_malformed_body_fail_closed(self):
        self.assertEqual(run([calendar([event()])], getter=FakeGetter(body="{not json")).status,
                         "location parse error")

    def test_fetch_exception_never_leaks_coordinates(self):
        leak = ValueError(f"failure at latitude={UNIQUE_LAT} longitude={UNIQUE_LON}")
        state: dict = {}
        ctx = run([calendar([event()])], getter=FakeGetter(raises=leak), state=state)
        self.assertEqual(ctx.status, "location fetch failed")
        blob = json.dumps(ctx.to_packet()) + json.dumps(state)
        self.assertNotIn(str(UNIQUE_LAT), blob)
        self.assertNotIn(str(UNIQUE_LON), blob)

    # -- production transport safety (no network) -----------------------------
    def test_local_transport_blocks_foreign_origin_and_redirects(self):
        transport = L.LocalLoopbackTransport()
        self.assertTrue(transport.allowed(L.FORCE_URL))
        self.assertFalse(transport.allowed("http://127.0.0.1:8000/api/v1/devices/other"))
        self.assertFalse(transport.allowed("http://evil.example/api/v1/devices/" + L.PHONE_DEVICE_ID))
        resp = transport("http://evil.example/steal", timeout=1.0, max_bytes=100)
        self.assertEqual(resp.status, 0)
        self.assertEqual(resp.error, "origin not allowed")
        handler = L._NoRedirectHandler.build()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "http://evil.example"))
        handlers = transport._opener.handlers
        names = {type(h).__name__ for h in handlers}
        self.assertIn("_Handler", names, "redirect handler must be installed")
        # Environment proxies must not be trusted: either no proxy handler is
        # installed at all, or the installed one carries an empty proxy map.
        proxy_handlers = [h for h in handlers if type(h).__name__ == "ProxyHandler"]
        for proxy in proxy_handlers:
            self.assertFalse(getattr(proxy, "proxies", {}), "env proxies must be ignored")

    # -- destination (local, explicit only) ----------------------------------
    def test_valid_fresh_fix_with_explicit_destination(self):
        ctx = run([calendar([event()])], getter=FakeGetter(payload=device_payload()),
                  destination=(1.29, 103.85))
        self.assertTrue(ctx.available and ctx.fresh)
        self.assertTrue(ctx.destination_resolved)
        self.assertIsInstance(ctx.distance_m, int)
        self.assertEqual(ctx.distance_confidence, "approximate")
        self.assertIn("not a route ETA", ctx.disclaimer)
        payload = ctx.to_packet()
        self.assertIn("distance_approx_m", payload)
        self.assertIn("disclaimer", payload)

    def test_unknown_named_destination_invents_nothing(self):
        ctx = run([calendar([event(location="Some Unmapped Venue")])],
                  getter=FakeGetter(payload=device_payload()))
        self.assertTrue(ctx.available)
        self.assertFalse(ctx.destination_resolved)
        self.assertIsNone(ctx.distance_m)
        payload = ctx.to_packet()
        for forbidden in ("distance_approx_m", "eta", "route", "travel_time", "arrival"):
            self.assertNotIn(forbidden, payload)
        self.assertNotIn("Some Unmapped Venue", json.dumps(payload))

    def test_phonefix_is_not_person_arrival(self):
        ctx = run([calendar([event()])], getter=FakeGetter(payload=device_payload()))
        payload = ctx.to_packet()
        for forbidden in ("arrival", "arrived", "present", "at_event", "person"):
            self.assertNotIn(forbidden, payload)
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["available"])

    # -- privacy + digest -----------------------------------------------------
    def test_private_context_has_no_coordinates_recursively(self):
        loc = {"latitude": UNIQUE_LAT, "longitude": UNIQUE_LON, "accuracy": 25.0,
               "timestamp": FIX_TS, "address": UNIQUE_ADDRESS, "note": RAW_MARKER}
        state: dict = {}
        ctx = run([calendar([event(location=UNIQUE_ADDRESS)])],
                  getter=FakeGetter(payload=device_payload(location=loc)), state=state)
        packet = build_packet([calendar([event(location=UNIQUE_ADDRESS)])], clock=T, policy=policy(),
                              baseline={}, private_context=ctx.to_packet())
        blob = json.dumps(packet) + json.dumps(state) + json.dumps(ctx.to_packet())
        for forbidden in (str(UNIQUE_LAT), str(UNIQUE_LON), UNIQUE_ADDRESS, RAW_MARKER,
                          "latitude", "longitude", "address"):
            self.assertNotIn(forbidden, blob)

    def test_material_private_context_affects_digest(self):
        base = build_packet([calendar([event()])], clock=T, policy=policy(), baseline={})
        ok = {"source": "phone_location", "status": "ok", "available": True, "fresh": True,
              "ref": "evt:primary:e1", "ephemeral": True}
        absent = {"source": "phone_location", "status": "no qualifying event",
                  "available": False, "fresh": False, "ephemeral": True}
        with_ok = build_packet([calendar([event()])], clock=T, policy=policy(), baseline={},
                               private_context=ok)
        with_absent = build_packet([calendar([event()])], clock=T, policy=policy(), baseline={},
                                   private_context=absent)
        self.assertNotEqual(packet_digest(base), packet_digest(with_ok))
        self.assertNotEqual(packet_digest(with_ok), packet_digest(with_absent))

    def test_non_material_flags_do_not_churn_digest(self):
        a = {"source": "phone_location", "status": "ok", "available": True, "fresh": True,
             "ref": "evt:primary:e1", "forced": True, "ephemeral": True}
        b = dict(a, forced=False)
        with_a = build_packet([calendar([event()])], clock=T, policy=policy(), baseline={}, private_context=a)
        with_b = build_packet([calendar([event()])], clock=T, policy=policy(), baseline={}, private_context=b)
        self.assertEqual(packet_digest(with_a), packet_digest(with_b))

    def test_qualifying_events_none_vs_empty(self):
        now = T.now()
        self.assertIsNone(L.qualifying_events([], now))
        self.assertIsNone(L.qualifying_events([calendar([], status="error")], now))
        self.assertEqual(L.qualifying_events([calendar([])], now), [])
        self.assertEqual(len(L.qualifying_events([calendar([event()])], now)), 1)

    # -- main.run integration -------------------------------------------------
    def test_main_run_context_seen_by_evaluator(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as d:
            g = FakeGetter(payload=device_payload())
            rc, ev = run_main(Path(d), fixture="location_inperson", getter=g)
            self.assertEqual(rc, 0)
            self.assertEqual(g.urls, [L.FORCE_URL])
            self.assertTrue(ev.calls)
            packet = ev.calls[0]
            self.assertEqual(packet["private_context"]["ref"], "evt:primary:loc1")
            self.assertTrue(packet["private_context"]["available"])
            self.assertTrue(packet["private_context"]["fresh"])

    def test_main_run_pause_zero_http(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as d:
            g = FakeGetter(payload=device_payload())
            rc, ev = run_main(Path(d), fixture="location_inperson", getter=g,
                              prefs={"schema": 1, "paused": {"active": True}})
            self.assertEqual(rc, 0)
            self.assertEqual(g.urls, [])
            self.assertEqual(ev.calls, [])

    def test_main_run_quiet_zero_http(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as d:
            g = FakeGetter(payload=device_payload())
            rc, ev = run_main(Path(d), fixture="location_inperson", getter=g,
                              clock_str="2026-10-03T03:00:00+08:00")
            self.assertEqual(rc, 0)
            self.assertEqual(g.urls, [])
            self.assertTrue(ev.calls)
            self.assertEqual(ev.calls[0]["private_context"]["status"], "quiet hours")

    def test_main_run_no_event_zero_http(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as d:
            g = FakeGetter(payload=device_payload())
            rc, ev = run_main(Path(d), fixture="location_online", getter=g)
            self.assertEqual(rc, 0)
            self.assertEqual(g.urls, [])
            self.assertEqual(ev.calls[0]["private_context"]["status"], "no qualifying event")

    def test_main_run_budget_deferred_zero_http(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as d:
            g = FakeGetter(payload=device_payload())
            rc, ev = run_main(Path(d), fixture="location_inperson", getter=g,
                              over={"collector_budget_seconds": 0.6})
            self.assertEqual(rc, 0)
            self.assertEqual(g.urls, [])
            self.assertEqual(ev.calls[0]["private_context"]["status"], "budget deferred")

    def test_main_run_privacy_recursive_packet_and_state(self):
        loc = {"latitude": UNIQUE_LAT, "longitude": UNIQUE_LON, "accuracy": 25.0,
               "timestamp": FIX_TS, "address": UNIQUE_ADDRESS, "note": RAW_MARKER}
        with tempfile.TemporaryDirectory(dir=ROOT) as d:
            g = FakeGetter(payload=device_payload(location=loc))
            rc, ev = run_main(Path(d), fixture="location_inperson", getter=g)
            self.assertEqual(rc, 0)
            packet_blob = json.dumps(ev.calls[0])
            state_file = Path(d) / "state" / "notify_state.json"
            state_blob = state_file.read_text(encoding="utf-8") if state_file.exists() else ""
            for forbidden in (str(UNIQUE_LAT), str(UNIQUE_LON), UNIQUE_ADDRESS, RAW_MARKER,
                              "latitude", "longitude"):
                self.assertNotIn(forbidden, packet_blob)
                self.assertNotIn(forbidden, state_blob)
            self.assertIn("evt:primary:loc1", packet_blob)

    def test_main_run_disabled_zero_http(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as d:
            g = FakeGetter(payload=device_payload())
            rc, ev = run_main(Path(d), fixture="location_inperson", getter=g, enabled=False)
            self.assertEqual(rc, 0)
            self.assertEqual(g.urls, [])
            self.assertNotIn("private_context", ev.calls[0])


if __name__ == "__main__":
    unittest.main()
