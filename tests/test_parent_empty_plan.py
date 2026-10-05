"""Parent regression: empty plans are not successful acquisition."""
import unittest
from datetime import datetime,timezone
from proactive_pilot.planner import validate_plan,PlannerError,plan_for_watch,FakePlanner
from proactive_pilot.policy import load_policy
class ParentPlanTests(unittest.TestCase):
 def test_empty_supported_model_plan_falls_back_honestly(self):
  p=load_policy(None); w={'id':'synthetic','version':1,'public_topic':'embedded databases','include':['simulation'],'exclude':['recruitment'],'scope':'github'}
  raw={'schema':1,'queries':[],'scope':'unsupported','semantic':True,'provenance':'model'}
  with self.assertRaises(PlannerError): validate_plan(raw,p,watch=w)
  result,used=plan_for_watch(w,p,FakePlanner(raw),now=datetime.now(timezone.utc))
  self.assertFalse(used)
  self.assertFalse(result['semantic'])
  self.assertEqual(result['provenance'],'deterministic-nonsemantic')
  self.assertEqual(result['queries'],['embedded databases simulation -recruitment'])
if __name__=='__main__': unittest.main()
