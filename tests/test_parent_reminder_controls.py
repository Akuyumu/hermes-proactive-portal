"""Independent reminder release controls; synthetic sources only."""
import sys
from pathlib import Path
from datetime import datetime, timedelta
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from proactive_pilot import reminders as R
from proactive_pilot.clock import SG

T = datetime(2026,10,3,12,0,tzinfo=SG)

class Getter:
    def __init__(self, start):
        self.start = start
    def get(self, *args):
        return R.normalize_event({'status':'confirmed', 'start':{'dateTime':self.start.isoformat()}})

class ReleaseControls(unittest.TestCase):
    def event(self, original):
        reg = R.empty_registry()
        R.add_event(reg,text='Meeting reminder',alias='primary',calendar_id='primary',event_id='e1',lead_minutes=10,provenance='synthetic explicit user request',now=T,getter=Getter(original))
        return reg

    def test_real_cancelled_payload_without_start_recognized(self):
        obs = R.normalize_event({'status':'cancelled','id':'e1'})
        self.assertTrue(obs.get('ok'), 'A legitimate cancellation has no start but must retire a reminder')
        self.assertEqual(obs['status'], 'cancelled')

    def test_moved_earlier_hours_before_original_time_detected(self):
        reg = self.event(T + timedelta(hours=5))
        plan = R.plan_dispatch(reg,now=T+timedelta(minutes=50),prefs={},getter=Getter(T+timedelta(hours=1)))
        self.assertTrue(plan.messages, 'Earlier move must be discovered before old trigger, not missed by polling only near old time')

    def test_restart_after_old_time_still_revalidates_later_move(self):
        reg = self.event(T+timedelta(hours=1))
        plan = R.plan_dispatch(reg,now=T+timedelta(hours=2),prefs={},getter=Getter(T+timedelta(hours=4)))
        rem = next(iter(reg['reminders'].values()))
        self.assertEqual(rem['status'], 'pending', 'Old-time expiry cannot retire an event before current-source revalidation')
        self.assertEqual(rem['event_due'], (T+timedelta(hours=4)).isoformat())
        self.assertFalse(plan.messages)

if __name__=='__main__':
    unittest.main()
