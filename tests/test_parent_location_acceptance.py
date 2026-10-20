"""Independent parent privacy/deadline controls. No network or private data."""
import time
import unittest
from proactive_pilot import location as L
from test_parent_location_controls import T, policy, calendar, event

class ParentLocationAcceptance(unittest.TestCase):
    def test_actual_bridge_schema(self):
        from test_parent_location_controls import device_payload
        payload = device_payload()
        payload['device_id'] = payload.pop('id')
        payload.pop('is_own_device')
        self.assertTrue(L.parse_device_fix(payload, now=T.now()).available)
        payload['device_id'] = 'other-device'
        self.assertFalse(L.parse_device_fix(payload, now=T.now()).available)

    def test_actual_bridge_null_location(self):
        fix = L.parse_device_fix({'device_id': L.PHONE_DEVICE_ID, 'location': None}, now=T.now())
        self.assertEqual(fix.reason, 'location unavailable')

    def test_actual_bridge_conflicting_identity(self):
        from test_parent_location_controls import device_payload
        payload = device_payload()
        payload['device_id'] = L.PHONE_DEVICE_ID
        payload['id'] = 'other-device'
        self.assertFalse(L.parse_device_fix(payload, now=T.now()).available)

    def test_untrusted_transport_error_not_serialized(self):
        secret = 'RAW_ADDRESS_COORDINATE_MARKER'
        def fake(*args, **kwargs):
            return L.LocationHttpResponse(status=0, error=secret)
        ctx = L.collect_location_context([calendar([event()])], clock=T,
            policy=policy(timezone="Asia/Singapore"), prefs={}, state={}, http_get=fake)
        self.assertNotIn(secret, repr(ctx.to_packet()))
        self.assertEqual(ctx.status, 'location fetch failed')

    def test_total_read_deadline_without_network(self):
        class SlowResponse:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, limit):
                time.sleep(0.5)
                return b'{}'
        class FakeOpener:
            def open(self, *args, **kwargs): return SlowResponse()
        transport = L.LocalLoopbackTransport()
        transport._opener = FakeOpener()
        start = time.monotonic()
        result = transport(L.FORCE_URL, timeout=0.05, max_bytes=100)
        self.assertLess(time.monotonic() - start, 0.3)
        self.assertEqual(result.error, 'fetch failed')

if __name__ == '__main__': unittest.main()
