"""Public-feed expansion tests: URL allowlist/SSRF, redirect denial, RSS/GitHub
parse + HTML stripping, cadence/caching, 429 without retry, Reddit per-run limit,
sanitized topics (no private leakage), bounded registry, end-to-end wiring.

Standalone (pytest not required):
  TMPDIR=<scratch> python3 tests/test_public_feeds.py
No network: a fake fetcher is injected. No production source is read.
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot.candidates import build_packet, packet_digest  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.gates import _fresh_healthy_coverage, decide  # noqa: E402
from proactive_pilot.evaluator import EvaluatorResult  # noqa: E402
from proactive_pilot.policy import load_policy  # noqa: E402
from proactive_pilot.publicfeeds import (  # noqa: E402
    FetchFailed,
    FetchResponse,
    URLRejected,
    _SafeRedirectHandler,
    canonical_host,
    collect_public_feeds,
    discover_topics,
    ensure_discovery_sources,
    fetch_bytes,
    load_registry,
    new_registry,
    parse_rss_atom,
    parse_github_releases,
    registry_add,
    sanitize_topic_text,
    strip_html,
    validate_public_url,
)
from proactive_pilot.state import StateStore  # noqa: E402

SG = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=SG)

RSS = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><id>e1</id><title>Hermes 1.2 released</title>
    <link href="https://example.com/e1"/>
    <updated>2026-10-03T11:00:00Z</updated>
    <summary>&lt;script&gt;alert(1)&lt;/script&gt;A &lt;b&gt;new&lt;/b&gt; release</summary>
  </entry>
</feed>"""


class FakeFetcher:
    def __init__(self, responses):
        self.responses = responses
        self.calls: list[str] = []

    def __call__(self, url, **kwargs):
        self.calls.append(url)
        resp = self.responses.get(url)
        if resp is None:
            for key, value in self.responses.items():
                if url.startswith(key):
                    resp = value
                    break
        if resp is None:
            raise FetchFailed("no fake response")
        if isinstance(resp, Exception):
            raise resp
        if callable(resp):
            return resp(url, **kwargs)
        return resp


def _policy(seeds=None):
    policy = load_policy(None)
    policy["feed_seed_sources"] = seeds if seeds is not None else []
    return policy


def _seed(url="https://api.github.com/repos/NousResearch/hermes-agent/releases",
          kind="reddit_rss", cadence=60):
    return [{"kind": kind, "url": url, "provenance": "test seed", "interest": "test",
             "cadence_minutes": cadence}]


def _rss_response():
    return FetchResponse(200, {"ETag": "abc"}, RSS.encode("utf-8"),
                         "https://www.reddit.com/r/hermesagent/.rss")


# -- URL safety / SSRF --------------------------------------------------------


def test_url_allowlist_accepts_public_and_rejects_unsafe():
    validate_public_url("https://api.github.com/repos/a/b/releases")
    validate_public_url("https://www.reddit.com/r/hermes/.rss")
    for bad in (
        "http://api.github.com/repos/a/b/releases",              # not https
        "https://user:pass@api.github.com/repos/a/b",            # credentials
        "https://api.github.com:8443/repos/a/b",                 # non-443 port
        "https://localhost/repos/a/b",                           # localhost
        "https://127.0.0.1/repos/a/b",                           # loopback IP
        "https://10.0.0.5/repos/a/b",                            # private IP
        "https://169.254.169.254/repos/a/b",                     # link-local metadata
        "https://evil.example/repos/a/b",                        # host not allowlisted
        "https://api.github.com/secret/path",                    # path not allowlisted
        "https://api.github.com/repos/a/b?access_token=leak",    # secret query key
    ):
        try:
            validate_public_url(bad)
        except URLRejected:
            continue
        raise AssertionError(f"expected rejection for {bad}")


def test_redirect_handler_rejects_unsafe_target():
    handler = _SafeRedirectHandler({**{"api.github.com": ("/repos/", "/search/")}})
    import urllib.request

    req = urllib.request.Request("https://api.github.com/repos/a/b/releases")
    try:
        handler.redirect_request(req, None, 302, "Found", {},
                                 "http://169.254.169.254/latest/meta-data")
    except URLRejected:
        return
    raise AssertionError("unsafe redirect was not rejected")


# -- parsing / sanitization ---------------------------------------------------


def test_parser_strips_html_and_script():
    entries = parse_rss_atom(RSS.encode("utf-8"))
    assert entries and entries[0]["title"] == "Hermes 1.2 released"
    assert "<script" not in entries[0]["summary"]
    assert "alert(1)" not in entries[0]["summary"]
    assert "new release" in entries[0]["summary"]
    assert strip_html("<b>hi</b><script>x</script>") == "hi"


def test_github_release_parse_strips_body():
    body = json.dumps([{"id": 1, "tag_name": "v1.2", "name": "Release",
                        "body": "<script>bad</script>notes",
                        "html_url": "https://github.com/a/b/releases/1",
                        "published_at": "2026-10-03T10:00:00Z"}]).encode()
    items = parse_github_releases(body)
    assert items[0]["title"] == "Release" and "bad" not in items[0]["summary"]


def test_sanitized_topics_do_not_leak_private_text():
    context = [
        "Pay invoice INV-9384756 to alex@example.com by 2026-10-05",
        "Research quantum computing grants",
        "quantum computing reading group",
    ]
    topics = discover_topics(context, max_topics=4)
    joined = " ".join(topics)
    assert "@" not in joined and "http" not in joined
    assert not any(ch.isdigit() for ch in joined)
    assert "quantum" in topics
    # A sensitive context line yields no tokens at all.
    assert sanitize_topic_text("Your OTP code is 483920") == ""

    registry = new_registry()
    ensure_discovery_sources(registry, topics, _policy(), NOW)
    urls = [s["url"] for s in registry["sources"].values()]
    assert urls
    for url in urls:
        assert "@" not in url and " " not in url
        assert "alex" not in url and "9384756" not in url


# -- registry bounds ----------------------------------------------------------


def test_registry_bounded_and_generic_requires_optin():
    registry = new_registry()
    assert registry_add(registry, kind="rss", url="https://generic.example/feed.xml",
                        provenance="x", interest="x", now=NOW, cadence_minutes=60) is False
    assert registry_add(registry, kind="rss", url="https://generic.example/feed.xml",
                        provenance="x", interest="x", now=NOW, cadence_minutes=60,
                        generic_opt_in=True) is True
    reg = new_registry()
    for i in range(12):
        assert registry_add(reg, kind="github_search", url=f"https://api.github.com/search/repositories?q=t{i}",
                            provenance="x", interest="x", now=NOW, cadence_minutes=60) is True
    assert len(reg["sources"]) == 12
    # 13th rotates out the oldest auto-added source rather than exceeding cap.
    assert registry_add(reg, kind="github_search", url="https://api.github.com/search/repositories?q=new",
                        provenance="x", interest="x", now=NOW, cadence_minutes=60) is True
    assert len(reg["sources"]) == 12


# -- end-to-end collection + caching/rate limits ------------------------------


def test_collect_public_feeds_end_to_end():
    with tempfile.TemporaryDirectory() as tmp:
        store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
        policy = _policy(_seed(url="https://www.reddit.com/r/hermesagent/.rss"))
        fetcher = FakeFetcher({"https://www.reddit.com/r/hermesagent/.rss": _rss_response()})
        results = collect_public_feeds(policy, SeededClock(NOW.isoformat()), store=store,
                                       context=["quantum computing"], fetcher=fetcher)
        feeds = [r for r in results if r.name.startswith("feed:")]
        assert feeds and feeds[0].count == 1 and feeds[0].healthy
        assert feeds[0].items[0]["ref"].startswith("feed:")
        packet = build_packet(results, clock=SeededClock(NOW.isoformat()), policy=policy, baseline={})
        kinds = {c["kind"] for c in packet["candidates"]}
        assert "feed" in kinds
        assert packet_digest(packet)  # stable, non-empty


def test_cadence_caches_and_does_not_refetch():
    with tempfile.TemporaryDirectory() as tmp:
        store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
        policy = _policy(_seed(url="https://www.reddit.com/r/hermesagent/.rss", cadence=60))
        fetcher = FakeFetcher({"https://www.reddit.com/r/hermesagent/.rss": _rss_response()})
        clock1 = SeededClock((NOW).isoformat())
        collect_public_feeds(policy, clock1, store=store, fetcher=fetcher)
        assert len(fetcher.calls) == 1
        clock2 = SeededClock((NOW + timedelta(minutes=10)).isoformat())
        results2 = collect_public_feeds(policy, clock2, store=store, fetcher=fetcher)
        assert len(fetcher.calls) == 1, "within cadence must not refetch"
        feed = next(r for r in results2 if r.name.startswith("feed:"))
        assert feed.cached and feed.cache_age_minutes >= 10


def test_429_marks_stale_without_retry():
    with tempfile.TemporaryDirectory() as tmp:
        store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
        policy = _policy(_seed(url="https://www.reddit.com/r/hermesagent/.rss", cadence=60))
        fetcher = FakeFetcher({"https://www.reddit.com/r/hermesagent/.rss": _rss_response()})
        collect_public_feeds(policy, SeededClock(NOW.isoformat()), store=store, fetcher=fetcher)
        assert len(fetcher.calls) == 1
        fetcher.responses["https://www.reddit.com/r/hermesagent/.rss"] = FetchResponse(
            429, {}, b"", "https://www.reddit.com/r/hermesagent/.rss")
        later = SeededClock((NOW + timedelta(hours=2)).isoformat())
        results = collect_public_feeds(policy, later, store=store, fetcher=fetcher)
        assert len(fetcher.calls) == 2, "429 must not be retried in-run"
        feed = next(r for r in results if r.name.startswith("feed:"))
        assert feed.status == "stale" and feed.cached


def test_reddit_per_run_limit_is_one_request():
    with tempfile.TemporaryDirectory() as tmp:
        store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
        policy = _policy([
            {"kind": "reddit_rss", "url": "https://www.reddit.com/r/a/.rss",
             "provenance": "t", "interest": "a", "cadence_minutes": 60},
            {"kind": "reddit_rss", "url": "https://www.reddit.com/r/b/.rss",
             "provenance": "t", "interest": "b", "cadence_minutes": 60},
        ])
        fetcher = FakeFetcher({
            "https://www.reddit.com/r/a/.rss": _rss_response(),
            "https://www.reddit.com/r/b/.rss": _rss_response(),
        })
        collect_public_feeds(policy, SeededClock(NOW.isoformat()), store=store, fetcher=fetcher)
        assert len(fetcher.calls) == 1, "at most one Reddit request per run"


def test_fetch_failure_uses_stale_cache_and_is_not_fresh():
    with tempfile.TemporaryDirectory() as tmp:
        store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
        policy = _policy(_seed(url="https://www.reddit.com/r/hermesagent/.rss", cadence=60))
        url = "https://www.reddit.com/r/hermesagent/.rss"
        fetcher = FakeFetcher({url: _rss_response()})
        collect_public_feeds(policy, SeededClock(NOW.isoformat()), store=store, fetcher=fetcher)
        fetcher.responses[url] = FetchFailed("boom")
        later = SeededClock((NOW + timedelta(hours=2)).isoformat())
        results = collect_public_feeds(policy, later, store=store, fetcher=fetcher)
        feed = next(r for r in results if r.name.startswith("feed:"))
        assert feed.status == "stale" and feed.cached and feed.cache_at
        packet = build_packet(results, clock=later, policy=policy, baseline={})
        ref = feed.items[0]["ref"]
        # The cached observation time is used, so stale cache is never "fresh".
        assert _fresh_healthy_coverage(packet, ref, later.now(), policy) is False


def test_feed_urgency_rejected_by_gates():
    policy = _policy()
    policy["_preferences"] = {"explicit": [], "inferred": []}
    packet = {
        "schema": 1,
        "coverage": {"feed:x": {"status": "ok", "checked_at": NOW.isoformat()}},
        "candidates": [{
            "kind": "feed", "source": "feed:x", "ref": "feed:x:1",
            "version": "v", "phase": "fresh",
            "date": (NOW + timedelta(minutes=20)).isoformat(), "all_day": False,
        }],
    }
    prop = EvaluatorResult("notify", kind="feed", key="feed:x:1", message="new release",
                           urgency="urgent", evidence=["feed:x:1"], consequence="x",
                           confidence="high", deadline=(NOW + timedelta(minutes=20)).isoformat())
    dec = decide(prop, packet, clock=SeededClock(NOW.isoformat()), policy=policy,
                 prefs=policy["_preferences"], state={})
    assert not dec["send"]


def test_expanded_sources_wiring():
    from proactive_pilot.collectors import expanded_sources

    policy = _policy()
    factories = expanded_sources(policy, SeededClock(NOW.isoformat()), store=None)
    # Core + feed + obligation are all present.
    assert len(factories) >= 3




def test_429_cached_evidence_is_not_fresh_and_not_urgent():
    with tempfile.TemporaryDirectory() as tmp:
        store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
        policy = _policy(_seed(url="https://www.reddit.com/r/hermesagent/.rss", cadence=60))
        policy["_preferences"] = {"explicit": [], "inferred": []}
        url = "https://www.reddit.com/r/hermesagent/.rss"
        fetcher = FakeFetcher({url: _rss_response()})
        collect_public_feeds(policy, SeededClock(NOW.isoformat()), store=store, fetcher=fetcher)
        fetcher.responses[url] = FetchResponse(429, {}, b"", url)
        later = SeededClock((NOW + timedelta(hours=2)).isoformat())
        results = collect_public_feeds(policy, later, store=store, fetcher=fetcher)
        feed = next(r for r in results if r.name.startswith("feed:"))
        packet = build_packet(results, clock=later, policy=policy, baseline={})
        assert feed.status == "stale" and feed.cached
        # Stale cached evidence can never substantiate urgency.
        assert _fresh_healthy_coverage(packet, feed.items[0]["ref"], later.now(), policy) is False
        prop = EvaluatorResult("notify", kind="feed", key=feed.items[0]["ref"],
                               message="new release", urgency="urgent",
                               evidence=[feed.items[0]["ref"]], consequence="x",
                               confidence="high", deadline=(later.now()).isoformat())
        dec = decide(prop, packet, clock=later, policy=policy, prefs=policy["_preferences"], state={})
        assert not dec["send"]





def test_unpolled_never_cached_source_is_partial_not_fresh_healthy():
    """A source never fetched (Reddit per-run slot already consumed) must NOT be
    reported as a fresh healthy ``ok`` check with a fabricated cache."""
    with tempfile.TemporaryDirectory() as tmp:
        store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
        policy = _policy([
            {"kind": "reddit_rss", "url": "https://www.reddit.com/r/a/.rss",
             "provenance": "t", "interest": "a", "cadence_minutes": 60},
            {"kind": "reddit_rss", "url": "https://www.reddit.com/r/b/.rss",
             "provenance": "t", "interest": "b", "cadence_minutes": 60},
        ])
        fetcher = FakeFetcher({
            "https://www.reddit.com/r/a/.rss": _rss_response(),
            "https://www.reddit.com/r/b/.rss": _rss_response(),
        })
        results = collect_public_feeds(policy, SeededClock(NOW.isoformat()), store=store,
                                       fetcher=fetcher)
        unpolled = [r for r in results
                    if r.name.startswith("feed:") and not r.cached and r.count == 0]
        assert unpolled, "the second Reddit source must be reported as unpolled"
        res = unpolled[0]
        assert res.checked_at == "", "unpolled source must not fabricate a fresh check"
        assert res.truncated is True
        packet = build_packet(results, clock=SeededClock(NOW.isoformat()), policy=policy, baseline={})
        cov = packet["coverage"][res.name]
        assert cov["status"] == "partial" and not cov["checked_at"] and not cov["cached"]


def test_deferred_source_without_cache_is_partial_not_fresh():
    with tempfile.TemporaryDirectory() as tmp:
        store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
        policy = _policy([
            {"kind": "reddit_rss", "url": "https://www.reddit.com/r/a/.rss",
             "provenance": "t", "interest": "a", "cadence_minutes": 60},
            {"kind": "reddit_rss", "url": "https://www.reddit.com/r/b/.rss",
             "provenance": "t", "interest": "b", "cadence_minutes": 60},
        ])
        policy["feed_max_sources_per_run"] = 1
        fetcher = FakeFetcher({
            "https://www.reddit.com/r/a/.rss": _rss_response(),
            "https://www.reddit.com/r/b/.rss": _rss_response(),
        })
        results = collect_public_feeds(policy, SeededClock(NOW.isoformat()), store=store,
                                       fetcher=fetcher)
        deferred = [r for r in results if "deferred" in (r.failure or "")]
        assert deferred, "the source beyond the per-run cap must be deferred"
        res = deferred[0]
        assert res.truncated is True and res.cached is False and res.checked_at == ""


def test_304_preserves_real_cache_at_and_refreshes_checked_at():
    with tempfile.TemporaryDirectory() as tmp:
        store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
        policy = _policy(_seed(url="https://www.reddit.com/r/hermesagent/.rss", cadence=1))
        url = "https://www.reddit.com/r/hermesagent/.rss"
        state = {"status": 200, "etag": "abc"}

        def handler(u, **kw):
            if state["status"] == 304:
                return FetchResponse(304, {}, b"", u)
            return FetchResponse(200, {"ETag": state["etag"]}, RSS.encode("utf-8"), u)

        fetcher = FakeFetcher({url: handler})
        r1 = collect_public_feeds(policy, SeededClock(NOW.isoformat()), store=store, fetcher=fetcher)
        first = next(r for r in r1 if r.name.startswith("feed:"))
        # A fresh 200 is not "cached"; its observation time becomes the cache_at.
        assert first.checked_at and not first.cached
        real_cache_at = first.checked_at
        state["status"] = 304
        later = SeededClock((NOW + timedelta(minutes=2)).isoformat())
        r2 = collect_public_feeds(policy, later, store=store, fetcher=fetcher)
        second = next(r for r in r2 if r.name.startswith("feed:"))
        assert second.cache_at == real_cache_at, "304 keeps the true observation time"
        assert second.checked_at == later.now().isoformat(), "304 refreshes the health check time"
        assert second.cached is True


import unittest as _unittest


class AwarenessControls(_unittest.TestCase):
    pass


def _attach_tests(cls):
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            setattr(cls, _name, (lambda self, _fn=_fn: _fn()))


_attach_tests(AwarenessControls)


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for test in tests:
        try:
            test()
            passed += 1
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            print(f"FAIL {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR {test.__name__}: {exc!r}")
    print(f"\n{passed}/{len(tests)} public-feed tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    raise SystemExit(_run_all())
