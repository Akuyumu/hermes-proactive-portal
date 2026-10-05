"""Offline regression for selected-watch cache identity; no files/network."""
import copy
import unittest
from test_native_bridge import FakeRunner, _payload, _row, _search_calls
from proactive_pilot.searchbridge import NativeSearchBridge, SEARCH_CACHE_ARTIFACT

class MemoryStore:
    def __init__(self): self.data = {}
    def load(self, name, default): return copy.deepcopy(self.data.get(name, default))
    def save(self, name, data): self.data[name] = copy.deepcopy(data)

class BindingTests(unittest.TestCase):
    def test_revision_invalidates_memory_and_persistent_cache(self):
        store = MemoryStore()
        runner = FakeRunner([_payload([_row('https://example.org/first')]), _payload([_row('https://example.org/second')])])
        bridge = NativeSearchBridge(runner, policy={}, clock=lambda:1000., wall_clock=lambda:1000., cache_store=store)
        bridge.bind_watch('test-watch', 1)
        self.assertFalse(bridge.search('public topic').from_cache)
        bridge.bind_watch('test-watch', 1)
        self.assertTrue(bridge.search('public topic').from_cache)
        bridge.bind_watch('test-watch', 2)
        changed = bridge.search('public topic')
        self.assertFalse(changed.from_cache)
        self.assertEqual(changed.hits[0].url, 'https://example.org/second')
        self.assertEqual(len(_search_calls(runner)), 2)
        self.assertEqual(list(store.data[SEARCH_CACHE_ARTIFACT]['entries']), ['test-watch|v2|public topic'])
