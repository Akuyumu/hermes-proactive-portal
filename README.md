# hermes-proactive-secretary

A proactive secretary plugin for [Hermes](https://hermes-agent.nousresearch.com):
read-only collectors over **your own authorised** mail, calendar and tasks plus
optional public sources, a conservative zero-tool evaluator, bounded
notification gates, and a durable requested-reminder registry.

It is deliberately quiet: silence is the default, and an alert only goes out for
a genuine, evidence-backed, time-bound reason. It never sends mail, mutates
calendars or tasks, books, purchases, or expands permissions.

For agent-assisted installation, give your agent [AGENT_SETUP.md](AGENT_SETUP.md).
It covers environment discovery, the few choices that require consent, connector
limits, configuration, verification and safe uninstall.

This repository is currently **private**. Other users need repository access or
an authorised distribution of the package; this is not a public catalog listing.
The package is profile/path-safe. Its configured source/account mapping is
explicit: without configuration it keeps the legacy `formal`/`informal` Himalaya
accounts and the external productivity-secretary library; with a validated user
configuration you name the accounts, calendars and task lists that are read.
Calendar/tasks via the bundled optional Google Workspace adapter still need the
installed skill and its existing authorisation. See the guide before promising
compatibility.

## Quickstart

```bash
# 1. install + enable the plugin (standalone repo, installed under ~/.hermes/plugins)
hermes plugins install Akuyumu/hermes-proactive-secretary --no-enable
hermes plugins enable hermes-proactive-secretary

# 2. prepare policy/state without schedules; review timezone, coverage and interests
hermes secretary setup --no-cron

# 3. write/validate the validated per-user configuration (generic neutral defaults)
hermes secretary configure init
hermes secretary configure validate
hermes secretary configure preview

# 4. arm the verified jobs with an explicitly authorised destination
hermes secretary setup --deliver '<verified-delivery-target>' # or --deliver local

# 5. verify
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

The release was checked with an offline suite and with an installed Linux
profile: setup, state migration, idempotency, native disable and both real
scheduler paths were exercised. This is not certification for every fresh host
or connector. The one-minute reminder runner is deterministic and model-free;
the 15-minute monitoring runner may invoke LLMs internally even though both
cron jobs use `--no-agent`.

Each new installation still needs its own capability, destination and schedule
verification. Migration does not pause or remove old jobs automatically.

## Agent-guided alternative

If you'd rather have the agent drive it, ask it to "load the proactive-secretary
skill and set the plugin up". The bundled skill walks through the same steps,
what to check after each, the exact commands, and the honest limits. The agent
can then explain the coverage on your machine.

## What it does

- Collects (read-only, bounded, fixed-argv) from whichever connectors are
  available:
  - calendar + tasks through an external productivity-secretary library when
    installed and authorised (not bundled with this plugin), or through the
    optional Google Workspace calendar adapter when explicitly enabled;
  - mail metadata through the Himalaya CLI using exactly the accounts named in
    the validated user configuration (legacy deployments keep `formal`/
    `informal`), never message bodies beyond a short redacted preview;
  - public feeds/pages when a user saves a public watch.
- Builds a bounded evidence packet and asks the native evaluator (resolved to a
  **zero-tool** toolset) whether anything merits interrupting you now. Every
  judgment lane -- evaluator, semantic planner, inferred-interest proposer,
  privacy verifier and conversational intake -- prefers the host-owned `ctx.llm`
  lane (the user's active model) and reports which route was used; the native
  zero-tool subprocess route remains only an explicit compatibility fallback for
  an older runtime that cannot provide a context.
- On the normal schedule the pilot runner does **not** import the pipeline
  directly: it invokes the registered `hermes secretary pipeline` handler with
  fixed argv and the same profile/data paths, so the host-built `ctx` from native
  plugin discovery (and therefore `ctx.llm` plus native `get_config`) is
  authoritative. The reminder runner likewise enters the registered
  `hermes secretary reminders` handler: it is deterministic, makes zero model
  calls and never touches `ctx.llm`. A subprocess timeout/error AFTER the native
  handler was started fails closed (no second collector/model run and no
  fallback); the direct-import compatibility path is used only when the native
  CLI is verifiably unavailable before any work, and that is reported.
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

## Source health and bounded recovery (limited, not self-healing)

Ordinary scheduled scans no longer emit a coverage notice every time a source
fails. Instead the plugin keeps a schema-validated per-source incident (actual
attempted checks, first/last failure, success and streak) in durable state:

- Only an **attempted** read counts. A missing, cached, partial, truncated,
  deferred or busy observation is neither a success nor a failure and can never
  be recorded as a recovery.
- A retryable timeout/503 stays **silent without an identifiable owner fix**. A `429` honours its
  `Retry-After`/backoff: the per-source dispatch gate blocks the next real call
  until that deadline, then permits exactly one **half-open** probe.
- A single critical-source alert is emitted **at most once per unresolved
  incident**, even across restarts, and only when the source was explicitly
  named critical in configuration, has failed repeatedly, has stayed failed for
  the persistent window (>= 2h by default), safe recovery is exhausted, and
  an explicitly classified revoked/missing grant requires owner action. A
  new incident can only qualify after an authoritative attempted success.
  Scoped clients are gated before requests; unrelated accounts continue under
  their existing budgets. Two bounded next-scheduled probes are the default;
  later cooldown-spaced checks are ordinary reads, not claimed auth repair.
  Health emission uses a durable incident-specific outbox: interruption after
  emission starts is marked uncertain across restart and is not blindly resent.

This is deliberately **limited repair**, not universal self-healing: the plugin
cannot fix a revoked credential, an outage at a provider, or a network fault. It
only stops ordinary scan spam, retries boundedly, and tells you once when an
actionable source you marked critical is persistently lost. Mail and calendar are NOT
critical by default; name them under `health.critical_sources` if you want an
alert. A provider overload or timeout alone is never presented as something you
can fix yourself.

## Permissions and privacy

- Collectors, searches and fetches can contact external authorised services and
  public sources. Native LLM calls for judgment and optional adaptive planning /
  privacy review use the user's configured provider; this is not an offline-only
  automation.
- Source content is treated as untrusted evidence, never as instructions.
- Credential/OTP messages are never fetched; URLs and codes are redacted from
  previews.
- Runtime state lives in an owner-only directory (`0700` dir / `0600` files)
  under `$HERMES_HOME/plugin-data/hermes-proactive-secretary/` (state, policy
  copy, preferences). It is never written into the installed plugin tree.
- The plugin declares no privileged capabilities and overrides no built-in tool.

## Assistant-mediated context snapshot

The conversational assistant can hand the scheduled pipeline a **bounded,
expiring delivery cache** of context it already knows from the user's authorised
memory/knowledge. This is deliberately *not* automatic whole-memory inheritance:
the pipeline never scrapes USER/MEMORY/Hindsight/Library, credentials, connectors
or a new allowlist on its own, and it never dumps an entire vault.

```bash
hermes secretary feedback context-list                     # metadata only (no text)
hermes secretary feedback context-show --id <entry id>     # explicit content read
hermes secretary feedback context-update --note "<a short fact>" \
  --provenance "assistant:memory" --kind interest [--expires-at <ISO>] [--owner-declared]
hermes secretary feedback context-revoke --id <entry id>   # durable revoke (tombstoned)
```

Properties:

- Typed entries with a stable id, provenance label, created/updated/expiry
  timestamps and an advisory-vs-owner-declared flag; entry and total-size caps;
  owner-only atomic writes under a profile-local path. `--provenance` is a fixed
  low-sensitivity enum (`user-stated`, `assistant:memory`, `assistant:knowledge`
  — the default, `owner-config`, `synthetic-test`) so metadata is generic and
  portable and never carries an owner/account/path/request excerpt. Default is
  **empty**, so a fresh install behaves exactly as before.
- Expiry is **mandatory and bounded**: a missing `--expires-at` defaults to 7
  days and nothing may outlive 30 days measured from `updated_at` (a refresh may
  preserve an older `created_at`). The **read boundary is authoritative**: exact
  shapes/types/kind/id/provenance and every timestamp are validated, the order
  `created_at <= updated_at <= expires_at` and a non-far-future `updated_at` are
  enforced, naive timestamps are normalised to UTC-aware rather than crashing,
  and any malformed entry (or an invalid tombstone) marks the whole snapshot
  invalid. Missing/empty/malformed/expired means **unavailable** with a bounded
  health reason; values are never fabricated and nothing is refreshed in the
  background. Injection/secret-bearing entries are refused at write time *and*
  re-checked at read time; diagnostics never echo untrusted ids/values/text.
- Treated as **untrusted advisory evidence** everywhere downstream. It reaches
  both the evaluator prompt and the inferred-interest proposer, but can never
  change authority, settings or gates, and copied identifiers/names/account
  details are structurally screened by the same privacy boundary. That boundary
  is a conservative, fail-closed screen — not a proof that no possible
  identifier can ever escape. Raw snapshot text is never written to status/log
  output.
- `pause`/disable and the model-free reminder dispatcher read no snapshot, model
  or network.

Feedback itself stays **assistant-mediated**: the assistant turns an explicit
user correction into a typed command, reads the exact record back (`show --id` or
the write's `receipt`) and shows a receipt. "Thanks"/silence is a no-op, not a
recorded preference; there is no automatic capture hook or conversation
surveillance. Existing explicit/inferred rules, snoozes, conditional exceptions,
expiries, provenance and revocation tombstones keep their semantics.

## If you have no connectors

Without an authorised mail/calendar/task connector you get **public-source
monitoring only** (`feed`/`obligation` evidence from public pages you
explicitly save). No personal sources are read.

## Validated per-user configuration

Beyond the runtime policy, the plugin reads an optional **validated per-user
configuration** from `plugins.entries.hermes-proactive-secretary.settings.user_config`
(native settings, preferred) or from `user_config.json` beside `policy.json`.
The generic neutral example ships at `policy/user-config.example.json` (timezone
`UTC`, no accounts, no seeds, adaptive off, no owner identifiers).

```bash
hermes secretary configure init        # write the neutral example, idempotent
hermes secretary configure show        # schema + effective config + overrides
hermes secretary configure validate    # fail-closed validation (non-zero on error)
hermes secretary configure preview     # policy keys that would change
hermes secretary configure migrate --from-policy <legacy-policy.json> [--dry-run]
```

Rules the configuration layer enforces:

Compatibility source scope: task accounts are independent of calendar and mail
accounts; a task-only configuration works with calendars disabled. Empty account
arrays read no connector. Within configured calendar accounts, an empty
`calendar.calendars` array retains the library's selected/primary/holiday
calendars; a nonempty array intersects those with exact calendar IDs **before**
event reads, including the birthday pass and page-token requests. Requested
reminders use the same selected-calendar and ID boundary. Within configured task
accounts, empty `tasks.lists` retains all visible lists; nonempty entries match
exact case-sensitive IDs or titles before task reads. Use `id:LIST_ID` or
`name:LIST_TITLE` to disambiguate. `tasks.adapter` supports `""`, `"tasks"` and
`"productivity_secretary"` (the same compatibility client); other values fail
closed. The compatibility route also requires `adapters.productivity_secretary`.

Migration inputs override prior fields; omission preserves prior explicit
accounts, aliases, IDs, lists, adapters and interests, including empty arrays.
For old user configs omitting `tasks.accounts`, validation copies only their
explicit calendar accounts and reports a warning; setting `tasks.accounts: []`
opts out. Explicit legacy-policy migration records formerly omitted calendar/task
account scopes in `advanced.legacy_account_discovery`, preserving the original
library alias discovery without reading connectors during migration. This marker
is absent from neutral defaults. Review `report.source_mappings` before writing.

- **Versioned and fail-closed.** An unknown/corrupt or newer version is refused;
  a stale version needs the explicit `migrate` step and is never rewritten
  silently. A nonempty configuration is not overwritten without `--force`.
  A present-but-malformed native `user_config` setting (bad JSON, non-object, or
  an unreadable settings accessor) fails closed; it never silently falls back to
  a local file or built-in defaults. An *absent* setting is simply unset.
- **Complete migration parity.** `migrate` retains every remaining effective
  legacy policy field as an explicit, reported compatibility passthrough (see
  `advanced.legacy_policy` and `report.retained_effective_fields`), and explicit
   `--mail-account`/`--calendar-account`/`--calendar-id`/`--task-account`/`--task-list`/`--adapter`
  inputs preserve an existing configured scope verbatim instead of hardcoding it.
- **One semantics for monitoring and reminders.** The deterministic reminder
  dispatcher loads/validates the exact same configuration precedence, timezone,
  quiet hours, limits and authorised scope, and never falls back to a broad
  account list when an explicit configuration restricts it. Event reads use ONLY
  the explicit `calendar.accounts`/`calendar.calendars`; mail accounts are never
  unioned into calendar authorisation. A disabled calendar or an empty explicit
  calendar account set makes zero event getter/source calls (the reminder stays
  active), while exact-time reminders keep working.
- **Production entries require configuration.** The scheduled pipeline and the
  `run-once`/`reminders` production commands thread the host `ctx` and apply the
  neutral generic defaults when no configuration is present (or fail closed on a
  malformed one), so a deleted/missing `user_config.json` can never silently
  re-enable the legacy private `formal`/`informal` sources. The legacy low-level
  API remains available only through an explicit flag for tests/back-compat.
- **No credential values.** Credential-shaped keys (`token`, `api_key`,
  `client_secret`, `password`, ...) are rejected; no token store lives here.
- **Explicit access.** `mail.accounts`, `calendar.accounts`, `calendar.calendars`,
   `tasks.accounts` and `tasks.lists` name what may be read. Credential presence alone never widens
  access. Unknown account names are supported through bounded fixed-argv calls
  once you name them.
- **Configuration ≠ preferences ≠ state.** Learned preferences stay in
  `preferences.json` and mutable state in `var/`; migration never moves a global
  pause, so it can never silently pause or unpause.

| Section | Example | Meaning |
|---|---|---|
| `timezone` | `"UTC"` | IANA timezone; neutral `UTC` when unset |
| `notification.destination` | `"local"` | Native `--deliver` target (empty = keep existing) |
| `notification.quiet_start_hour` / `quiet_end_hour` | `0` / `8` | Quiet hours |
| `notification.daily_normal_cap` / `cooldown_minutes` | `3` / `30` | Envelope limits |
| `sources.*` | `false` | Per-source enablement (public feeds, obligations, mail, calendar, tasks) |
| `mail.accounts` | `[]` | Explicit Himalaya accounts (empty = no private mail) |
| `calendar.accounts` / `calendar.calendars` | `[]` | Explicit calendar account/ID selection |
| `tasks.lists` / `tasks.adapter` | `[]` / `""` | Task lists and adapter name |
| `public_interests` / `public_sources` | `[]` | Public interests and vetted source specs |
| `adaptive.mode` / `privacy_review` / `inferred_goals` | `"disabled"` | Adaptive discovery + paired privacy review |
| `bounds.*` | `60` | Collector budget, candidate cap, per-run source cap |
| `adapters.productivity_secretary` | `false` | Optional compatibility calendar/tasks library |
| `adapters.gws_calendar` | `false` | Optional Google Workspace calendar adapter |

### Optional Google Workspace calendar adapter

When `adapters.gws_calendar` is enabled and calendars are named, calendar reads
go through the already-installed `productivity/google-workspace` skill CLI
(`google_api.py calendar list`) using the active `HERMES_HOME` authorisation.
The adapter runs a fixed, read-only argv, parses the native JSON contract and
requires absolute offsets; it fails closed (naming the disabled capability) on a
missing skill, missing dependency, absent token, unsupported account routing,
malformed response, non-zero exit or timeout. Readiness probing never refreshes
or reads a token value and never calls a connector API; a real source call only
happens after you explicitly enable the adapter. There is no Google Tasks
command in that surface, so no Tasks adapter is invented.

## Configuration reference

A generic default ships at `policy/policy.default.json`. Setup copies it to the
data directory as `policy.json`. This legacy policy file remains the runtime
default (its shipped timezone is `Asia/Singapore`); the neutral user
configuration above defaults to `UTC` and only overrides what it defines.
Notable keys:

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
| `native_render_fallback_enabled` / `native_render_consent` | `false` | Opt-in bounded JS-shell rendering (needs a host renderer) |
| `firecrawl_enabled` / `firecrawl_consent` | `false` | Opt-in Firecrawl extract via native `ctx.call_mcp` (host OAuth) |
| `firecrawl_mcp_server` / `firecrawl_mcp_tool` | `""` | Named MCP server/tool (never an endpoint or a key) |
| `firecrawl_max_attempts_per_cycle` / `firecrawl_max_attempts_per_day` | `1` / `10` | Local metered-attempt ceilings (not paid credits) |
| `firecrawl_concurrency` / `firecrawl_timeout_seconds` | `1` / `30` | Bounded concurrent calls / call timeout |
| `firecrawl_cooldown_minutes` / `firecrawl_cache_ttl_minutes` | `60` / `60` | 429 cooldown / repeat-scrape freshness |
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

### Routed retrieval fallbacks (v0.4.0, opt-in, off by default)

The direct DNS-pinned public HTTPS transport is always the default. A 200
JavaScript shell on a public page may *optionally* be rendered by a
host-supplied, constrained renderer, and Firecrawl may *optionally* be used as a
metered extraction backend — but **only** when you set the enable **and** consent
flag for that route and a verified host interface exists. Both are reported
candidly by `hermes secretary status` / `doctor`: when unavailable, the honest
reason is shown and nothing is faked.

```json
{
  "native_render_fallback_enabled": false,
  "native_render_consent": false,
  "firecrawl_enabled": false,
  "firecrawl_consent": false,
  "firecrawl_mcp_server": "",
  "firecrawl_mcp_tool": "",
  "firecrawl_max_attempts_per_cycle": 1,
  "firecrawl_max_attempts_per_day": 10,
  "firecrawl_concurrency": 1,
  "firecrawl_timeout_seconds": 30,
  "firecrawl_cooldown_minutes": 60,
  "firecrawl_cache_ttl_minutes": 60,
  "retrieval_fallback_max_bytes": 2097152
}
```

- A `blocked` challenge/interstitial/paywall/login page is **terminal**: it is
  never rendered, bypassed, or treated as evidence. Fallback applies only to a
  `dynamic` JS shell. A private/SSRF-rejected URL is denied *before* any metered
  attempt is reserved.
- The generic shared Hermes browser is deliberately **not** reused (owner
  session/cookies, SSRF/subresource surface). Without a host-injected
  constrained renderer the native-render capability is reported unavailable.
- Firecrawl goes through the **supported native plugin contract**
  `ctx.call_mcp(server, tool, arguments, timeout)`: the host owns the OAuth
  connection and the MCP allowlist. This plugin embeds no endpoint, key or OAuth
  client, and sends only one narrow request
  (`{"url": <public-https-url>, "formats": ["markdown"]}`) to the one configured
  server/tool. The remote tool schema is host-owned; until a verified reference
  is supplied it is reported as unverified rather than guessed.
- Every Firecrawl call is a **reserved, persisted local attempt**: ceilings are
  enforced per cycle and per UTC day, shared across collectors and restarts, with
  a provider 429 cooldown. A failed attempt still consumes the reserved attempt.
  Local attempt counts are **not** provider billing credits, and the plugin never
  switches to another metered service when a budget is exhausted — it defers.


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
add `--purge` only with explicit approval to delete it. A native `hermes plugins disable` on its own leaves
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
