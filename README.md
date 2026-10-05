# hermes-proactive-secretary

A proactive secretary plugin for [Hermes](https://hermes-agent.nousresearch.com):
read-only collectors over **your own authorised** mail, calendar and tasks plus
optional public sources, a conservative zero-tool evaluator, bounded
notification gates, and a durable requested-reminder registry.

It is deliberately quiet: silence is the default, and an alert only goes out for
a genuine, evidence-backed, time-bound reason. It never sends mail, mutates
calendars or tasks, books, purchases, or expands permissions.

## Quickstart

```bash
# 1. install + enable the plugin (standalone repo, installed under ~/.hermes/plugins)
hermes plugins install <owner>/hermes-proactive-secretary
hermes plugins enable hermes-proactive-secretary

# 2. one-command, idempotent setup (policy, state, the two native cron jobs, verification)
hermes secretary setup

# 3. verify
hermes secretary status
hermes secretary doctor
hermes secretary self-test
```

`hermes secretary setup` is safe to re-run. It verifies the offline self-test
and a no-model dry-run **before** installing launchers or creating any cron job,
so a failing check arms nothing. It never overwrites an existing policy unless
you pass `--force`, and it reports honestly if a rollback removal does not
complete.

### Importing existing legacy state

If you already have a legacy tree (`<legacy>/var/`, `<legacy>/policy/policy.json`,
`<legacy>/policy/preferences.json`), preview and then import it:

```bash
hermes secretary setup --import-from <legacy_tree> --import-dry-run   # inventory + hashes, no changes
hermes secretary setup --import-from <legacy_tree>                    # copy byte-for-byte, then verify
```

The import copies durable files byte-for-byte with owner-only permissions
(`0700` dirs / `0600` files), never rebuilds reminders, refuses any conflicting
target file, is a safe no-op on an identical re-run, and rejects symlinks and
path traversal. It never deletes or adopts legacy cron jobs; that stays a manual,
verified cutover step.

## Native disable vs `hermes secretary disable`

Both scheduled runners check the native `plugins.enabled` / `plugins.disabled`
config before they import the pipeline, read state, collect, evaluate or emit.
So `hermes plugins disable hermes-proactive-secretary` stops work on each next
run (empty stdout, no alert). It does **not** remove the cron schedules — it
cannot — so the schedules remain inert until you clean them up. The enabled list
must explicitly contain the plugin; a missing, malformed or empty enabled list
fails closed and the runners stay silent.

Use `hermes secretary disable` (or `uninstall`) to pause **and** remove this
plugin's own jobs and installed launchers before a native uninstall. Messages
already delivered are not revoked.

## Offline verification vs. live cutover

The automatic setup, the runner gate, the state import and the tests are all
exercised **offline** against fake profiles: no live/private source is read, no
network is used, and no real cron job is created or removed. Actual end-to-end
native-cron behaviour, installed-copy promotion, publication and the live
legacy-to-plugin cutover remain the parent's verified manual steps.

## Agent-guided alternative

If you'd rather have the agent drive it, ask it to "load the proactive-secretary
skill and set the plugin up". The bundled skill walks through the same steps,
what to check after each, the exact commands, and the honest limits. The agent
can then explain the coverage on your machine.

## What it does

- Collects (read-only, bounded, fixed-argv) from whichever connectors are
  available:
  - calendar + tasks through the shipped productivity-secretary library when
    present;
  - mail metadata through the Himalaya CLI when present (`formal`/`informal`
    accounts only), never message bodies beyond a short redacted preview;
  - public feeds/pages when a user saves a public watch.
- Builds a bounded evidence packet and asks the native evaluator (resolved to a
  **zero-tool** toolset) whether anything merits interrupting you now.
- Applies deterministic envelope guards: quiet hours, daily cap, cooldown,
  dedupe, expiry, stale-source handling, a single-writer lock, and your
  explicit preferences.
- Dispatches at most one short message per run through the native scheduler.
- Holds explicit **requested reminders** (event-bound or exact-time) in a
  durable owner-only registry and dispatches them once, with overlap, move,
  cancellation and source-failure handling.

## What it does not do

- It does not send mail, create/modify calendar events or tasks, purchase,
  book, or take any external action on your behalf.
- It does not guarantee complete coverage: blocked, failed, deferred and
  truncated sources are recorded separately. Undated tasks are not covered.
- It does not confirm delivery. "Emitted" means a message was written to stdout;
  the native scheduler owns actual delivery.
- It does not fabricate urgency, personal fit, location, or a deadline.

## Permissions and privacy

- Everything runs locally except the existing native evaluator/model call that
  judges the evidence packet.
- Source content is treated as untrusted evidence, never as instructions.
- Credential/OTP messages are never fetched; URLs and codes are redacted from
  previews.
- Runtime state lives in an owner-only directory (`0700` dir / `0600` files)
  under `$HERMES_HOME/plugin-data/hermes-proactive-secretary/` (state, policy
  copy, preferences). It is never written into the installed plugin tree.
- The plugin declares no privileged capabilities and overrides no built-in tool.

## If you have no connectors

Without an authorised mail/calendar/task connector you get **public-source
monitoring only** (`feed`/`obligation` evidence from public pages you
explicitly save). No personal sources are read.

## Configuration reference

A generic default ships at `policy/policy.default.json`. Setup copies it to the
data directory as `policy.json`. Notable keys:

| Key | Default | Meaning |
|---|---|---|
| `persona` | `"Hermes"` | Name used in the evaluator prompt |
| `notify_address` | `"the user"` | How the evaluator addresses you |
| `timezone` | `"Asia/Singapore"` | Display/reference timezone |
| `quiet_start_hour` / `quiet_end_hour` | `0` / `8` | Quiet hours (urgent may bypass) |
| `daily_normal_cap` | `3` | Max ordinary alerts per day |
| `cooldown_minutes` | `30` | Minimum gap between deliveries |
| `feeds_enabled` / `obligations_enabled` | `true` | Public feeds / mail obligations |
| `feed_seed_sources` | `[]` | **Empty by default** — add your own public sources |
| `adaptive_mode` | `"disabled"` | Keep disabled unless you want adaptive discovery |
| `location_context_enabled` | `false` | Local phone context is opt-in and off by default |
| `evaluator.toolset` | `"context_engine"` | Must resolve to zero tools |
| `evaluator.hermes_bin` / `evaluator.profile` | unset | Override the native binary/profile |

### Adding public sources

`feed_seed_sources` ships **empty**. Add entries you have vetted, for example:

```json
{
  "kind": "github_releases",
  "url": "https://api.github.com/repos/<owner>/<repo>/releases",
  "provenance": "configured by me",
  "interest": "release notes",
  "cadence_minutes": 360
}
```

The `kind` must be one the bundled parsers support (for example
`github_releases`, `reddit_rss`, `rss_atom`, `web_page`, `nea_forecast`,
`ntu_json`, `ntu_opportunity`, `smrt_notice`, `cna_rss`). Only add public,
keyless endpoints you trust.

### Paths and overrides

- `HERMES_HOME` selects the profile home (default `~/.hermes`).
- `HERMES_PROACTIVE_SECRETARY_DATA` overrides the runtime data root.
- `HERMES_AGENT_SRC` / `SECRETARY_SRC` override the native agent and
  productivity-secretary source locations.
- `HERMES_BIN` overrides the `hermes` binary used for the cron surface.

## Troubleshooting

- **setup: "cron surface unavailable"** — `hermes cron` is not reachable.
  Fix that first; `--no-cron` only performs an offline check and creates nothing.
- **doctor fails** — read the failing check name. Missing launchers or policy
  mean an incomplete plugin tree; a failing cron check means the scheduler is
  unavailable.
- **No alerts** — run `hermes secretary status`, then
  `hermes secretary run-once --check` for a coverage diagnostic. An empty result
  is not proof of healthy collection.
- **Reminder did not fire** — inspect the registry list/status; overlap makes a
  dispatch skip silently by design, and `emitted` is not delivery-confirmed.
- **Corrupt state/preferences** — the runtime fails closed instead of re-sending.
  Restore from a known-good file; do not delete the lock to "fix" a symptom.

## Uninstall

```bash
hermes secretary uninstall          # pause + remove this plugin's two cron jobs
hermes plugins disable hermes-proactive-secretary
hermes plugins remove hermes-proactive-secretary
```

`uninstall` pauses collection/delivery and removes only this plugin's own jobs.
Runtime data — including anything brought in by `--import-from` — is preserved;
add `--purge` to delete it. A native `hermes plugins disable` on its own leaves
the schedules inert but present, so run `hermes secretary disable` before a
native removal to clean up this plugin's own jobs and launchers.

## Development

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
PYTHONPATH=src python3 -m proactive_pilot.selftest
hermes plugins doctor . --ci
hermes plugins validate .
```

## License

MIT — see `LICENSE`.
