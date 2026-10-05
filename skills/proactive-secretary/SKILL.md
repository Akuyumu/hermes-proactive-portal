---
name: proactive-secretary
description: "Use when a user sets up, runs, tunes, pauses or troubleshoots the proactive secretary plugin (proactive alerts and requested reminders)."
version: 1.0.0
author: Hermes Plugin Community
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [secretary, proactive, notifications, reminders, cron]
    related_skills: []
---

# Proactive Secretary

## What this is

`hermes-proactive-secretary` watches a user's **own authorised** mail, calendar
and tasks plus optional public sources, decides conservatively whether anything
merits interrupting them, and delivers at most a short message through the
native Hermes scheduler. It also holds explicit, user-requested reminders
(event-bound or exact-time) in a durable owner-only registry and dispatches
them once.

It is **read-only over the user's sources** and makes no calendar/mail/task
writes, no purchases, and no bookings. The evaluator runs with a resolved
zero-tool toolset, so it can only judge the supplied evidence packet.

## When to use this skill

Load it when the user wants to:
- set the plugin up for the first time;
- see whether it is running and what it monitors;
- pause, resume, disable or uninstall it;
- record or inspect notification feedback;
- save or inspect a public watch;
- add or cancel a requested reminder;
- understand why an alert was (or was not) sent.

## First-time agent-assisted installation

Read the repository's `AGENT_SETUP.md` before installing or configuring a new
user's deployment. Discover the active profile and existing connectors, obtain
an explicitly authorised destination, review timezone/quiet hours and public
interests, and prepare with `setup --no-cron` before arming jobs. The repository
is private and requires access. Mail account names and calendar/task connector
requirements are currently specific, not automatically mapped to arbitrary
services; report missing coverage instead of silently expanding access.

## One-command setup

```bash
hermes plugins enable hermes-proactive-secretary   # once
hermes secretary setup                             # idempotent; safe to re-run
```

`setup` is the whole setup. It:

1. creates the owner-only (`0700`) runtime data directory;
2. if `--import-from <legacy_tree>` is given, copies durable legacy state
   (`var/`, `policy/policy.json`, `policy/preferences.json`) byte-for-byte and
   privately, refusing any conflicting target (use `--import-dry-run` first to
   preview an inventory with counts and hashes and change nothing);
3. writes a policy file from the shipped generic default **if absent** (it never
   overwrites an existing policy without `--force`, and never over an imported
   one);
4. detects which connectors are available on this machine;
5. runs the offline self-test and an offline no-model dry-run **before** arming
   anything;
6. only after those checks pass, installs the launchers and creates the two
   native cron jobs — pilot every 15 minutes, requested-reminder dispatcher every
   1 minute — verifying each exists;
7. prints a short summary: what is enabled, what is monitored, what is **not**
   covered, and how to pause.

A failing offline check therefore creates **zero** cron jobs. If job creation
fails part-way, setup removes only the jobs it created in that run and reports
any removal it could not complete; it never claims a failed cleanup succeeded.

### Importing existing legacy state

```bash
hermes secretary setup --import-from /path/to/legacy-tree --import-dry-run  # preview only
hermes secretary setup --import-from /path/to/legacy-tree                   # copy, then verify
```

The import never rebuilds reminders, never overwrites an existing file, and
leaves the legacy tree and any legacy cron jobs untouched (the parent pauses and
verifies them separately). A second identical import is a safe no-op. Symlinks
and path traversal are refused; only narrow transient/cache/shadow artifacts are
excluded, and excluded filenames are reported without their contents.

### What to check after setup

```bash
hermes secretary status        # enabled? which jobs? last run? counts?
hermes secretary doctor        # wiring + environment diagnostics
hermes secretary self-test     # full offline deterministic suite
```

Confirm:
- `status` reports `enabled` and lists `hermes-proactive-secretary-pilot` and
  `hermes-proactive-secretary-reminders`;
- `doctor` exits 0 (cron backend available; launchers present);
- `self-test` reports all checks passed.

## Day-to-day commands

| Command | What it does |
|---|---|
| `hermes secretary setup` | One-command, idempotent setup + verification (add `--force` to rewrite policy; `--no-cron` for offline checks) |
| `hermes secretary status` | enabled/disabled, job state, last run, coverage, counts |
| `hermes secretary pause` | Global pause: stops collection **and** delivery |
| `hermes secretary resume` | Clears the global pause |
| `hermes plugins enable hermes-proactive-secretary` | (native) re-enable after a native disable |
| `hermes secretary disable` | Pauses and removes **only this plugin's** two cron jobs |
| `hermes secretary uninstall` | Alias of disable; runtime data is preserved (`--purge` deletes it) |

Every command accepts `--json` and exits non-zero on failure.

### Native toggle vs. secretary disable

The two runners check the native `plugins.enabled` / `plugins.disabled` config
before they do any work. `hermes plugins disable hermes-proactive-secretary`
therefore stops collection and delivery on each **next** invocation and the
runners exit silently — but it does **not** remove the cron schedules (it never
can), so inert schedules remain until you clean them up. Because that toggle is
read from `plugins.enabled`, a missing, malformed or empty enabled list fails
closed for this opt-in plugin, so the runners stay quiet rather than run
unmanaged.

Use `hermes secretary disable` (or `uninstall`) for an explicit pause **plus**
cleanup of this plugin's own jobs and installed launchers before a native
uninstall. Already-delivered messages are not revoked; only future runs stop.
| `hermes secretary self-test` | Offline deterministic self-test |
| `hermes secretary run-once` | Dry, no-model run; never sends (add `--fixture <name>` for an offline run) |
| `hermes secretary watch add <topic>` | Save a public watch |
| `hermes secretary watch list` | List public watches |
| `hermes secretary watch remove <id>` | Remove a public watch |
| `hermes secretary feedback <action> [options]` | Record/inspect preferences (see below) |
| `hermes secretary doctor` | Environment and wiring diagnostics |

Every command accepts `--json` and exits non-zero on failure.

## Feedback

Use the validated command, never hand-edit JSON:

```bash
hermes secretary feedback add-explicit --kind mail --match newsletter \
  --scope newsletters --provenance "user asked to stop newsletters"
hermes secretary feedback add-explicit --kind mail --match career \
  --scope "generic career broadcasts" --provenance "user correction" \
  --conditional --exception "unusually fitting"
hermes secretary feedback add-inferred --kind mail --match career \
  --note "tentative pattern" --confidence low --provenance "observed replies"
hermes secretary feedback snooze --kind mail --match career --scope "today only" \
  --provenance "user said not today" --until <timezone-aware ISO>
hermes secretary feedback pause --provenance "user asked"
hermes secretary feedback resume --provenance "user asked"
hermes secretary feedback list
hermes secretary feedback forget --id <exact existing id>
```

Rules:
- Do **not** record "thanks" or silence as preferences.
- Clear corrections are durable without asking again; inferred patterns stay
  advisory and revisable.
- "Not today" is a scoped, temporary snooze — never a permanent ban.
- Conditional rules must keep their exception (career updates *unless unusually
  fitting* is not "never career updates").
- Read back the exact affected preference after a write before claiming saved.
- Bad input and corrupt artifacts fail closed (non-zero, nothing written).

## Requested reminders

Use the durable registry, never an untracked time-only cron job:

```bash
# Bind to an exact authorised calendar event (validated by a fresh read):
hermes secretary feedback watch ...        # (public watches use the watch command)
```

Event-bound and time-only reminders are created by the underlying registry
command (the plugin forwards `hermes secretary feedback` actions). Prefer:
ask for the exact event, the authorised alias / calendar ID / event ID,
timezone and requested lead time; only then register. Inspect with the registry
list/status and cancel by exact id. Never fabricate provenance or bind to an
ambiguous message.

Requested reminders bypass ordinary quiet hours / daily cap / cooldown (the user
explicitly asked) but **never** a global pause and never the explicit-provenance
requirement. Delivery is best-effort; do not promise hard punctuality.

## Honest coverage limits

- The collectors read the user's own **authorised** mail/calendar/tasks. A user
  with no such connector gets **public-source monitoring only** — say so.
- Undated tasks are not covered.
- Blocked, failed, deferred and truncated sources are recorded separately; do
  not promise complete coverage.
- Public feed items are tentative public evidence, not obligations. A forecast
  is a forecast, not an active warning.
- The evaluator is conservative: silence is the default and always acceptable.
- "Emitted" means the plugin wrote a message; **actual delivery is owned by the
  native scheduler** and is not confirmed by the plugin.

## Troubleshooting

- **`setup` fails with "cron surface unavailable"** — the Hermes cron CLI is not
  reachable. Confirm `hermes cron` works, then re-run `hermes secretary setup`.
  Use `--no-cron` only for an offline check; it does not create the jobs.
- **`doctor` fails a critical check** — read the printed check name. Missing
  launchers/policy mean the plugin tree is incomplete; a missing cron backend
  means the scheduler is unavailable.
- **No alerts** — first check `hermes secretary status` (paused? jobs present?),
  then `hermes secretary run-once --check` for a coverage diagnostic. An empty
  result is not proof of healthy collection.
- **Nothing is urgent** — silence is expected. The evaluator only notifies for
  genuine, evidence-backed, time-bound items.
- **A reminder did not fire** — inspect the registry list/status. Overlap makes
  a dispatch skip silently by design; `emitted` is not delivery-confirmed.
- **State or preferences look corrupt** — the runtime fails closed rather than
  re-sending. Restore from a known-good file; do not delete the lock or reset
  dedupe state to "fix" a symptom.
- **Pause did not take** — `hermes secretary pause` is global and stops
  collection and delivery including urgency. `resume` is the only clear.

## Verification (for the agent)

Before claiming success, do all of:
1. `hermes secretary setup` exits 0 and lists both jobs.
2. `hermes secretary status` shows enabled + both jobs + a monitoring summary.
3. `hermes secretary self-test` reports all checks passed.
4. `hermes secretary doctor` exits 0.
5. State the coverage limits above rather than equating passing synthetic tests
   with secretary-quality judgment.
