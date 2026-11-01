# Public coverage

Public checks are **bounded and periodic**. Each cycle has one total allowance
for public source attempts, shared by ordinary feeds and adaptive evidence. The
allowance is not a promise of complete coverage, and it is never increased by
running both paths.

When ordinary feeds and an adaptive watch are both due, each receives a reserved
share of that same allowance (for a two-attempt cycle: one each). An odd spare
follows the existing alternating priority. A path may use unused spare quota or
borrow a share the other path has explicitly finished without using; it cannot
take a due path's guaranteed share. With only one path enabled or due, that path
keeps the full existing allowance.

Only a real fetch attempt consumes quota. A source skipped because of cadence,
source-health cooldown, the shared Reddit limit, a transport gate, or a
positive-but-too-small remaining budget is examined and rotated past, but is not
charged as an attempt and does not record a failure.

## Deferral is not failure

When the remaining budget is too small to attempt a source safely, the source is
reported as deferred. Deferral does not refresh `last_fetch_at`, does not
increment the failure count, and does not write an HTTP-style `stale_reason`.
The same rule applies to ordinary feeds, adaptive watches, and maintenance
probes.

A deferred source may still carry its existing cached evidence, but only with
its true observation time. An expired structured announcement or forecast stays
stale: deferral never revives expired evidence or turns it into a healthy check.

## Adaptive rotation

Adaptive watches rotate their own due sources with a bounded per-watch cursor.
One busy watch therefore does not consume another watch's rotation position.
Legacy global cursor state is migrated to the current watch when first seen; no
source is deleted.

## Limits

These controls make coverage fairer within the unchanged budget, spend and
request caps. They do not provide universal, real-time or guaranteed coverage,
and they do not prove that a live source is fixed. Silence still means only that
nothing qualified within the checks that actually ran.
