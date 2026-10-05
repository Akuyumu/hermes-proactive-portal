# Agent-assisted installation and setup

Give this file to your Hermes agent with:

> Install and configure the proactive secretary using this guide. Inspect my environment first, ask only for missing choices or authorisation, preserve existing configuration, and verify the result. Do not claim a connector works merely because it is installed.

This guide uses no original owner's state, delivery address, credentials or interests. The repository is currently **private**: installation requires access to `Akuyumu/hermes-proactive-secretary`. Do not make it public or change repository access automatically.

## 1. Inspect before changing anything

- Confirm Hermes is installed and inspect `hermes plugins install --help` and `hermes cron --help`. This package targets Linux/macOS; Windows and fresh-machine portability have not been independently certified.
- Resolve the active profile. Honour `HERMES_HOME`; do not configure a different profile or assume the default home.
- Inspect existing plugins and scheduled jobs. If already installed, use `hermes secretary status` and `doctor` before reinstalling. Do not use `--force`, remove foreign jobs, or overwrite existing state to make setup succeed.
- Check native model availability, configured public-search availability if requested, and the messaging gateway. Do not expose credentials or alter the user's provider, permissions, or paid services.
- Explain that authorised evidence is sent to the configured model for judgment. Public acquisition uses external websites/search; read-only does not mean offline.

## 2. Gather the few choices that cannot safely be guessed

Ask together, only when not already explicitly established:

1. Delivery: which already authorised channel/chat should receive alerts? Verify its exact destination through the existing gateway. Otherwise use `local`, which saves output without messaging anyone.
2. Timezone and quiet hours: the shipped defaults are `Asia/Singapore`, midnight to 08:00, three ordinary alerts per day and a 30-minute cooldown. These are defaults, not an inferred user location or preference.
3. Coverage: public sources only, or supported, already authorised private connectors too? Ask for public interests/source URLs if the user wants public monitoring.
4. Adaptive discovery: whether to enable model-driven public-interest discovery, with the paired privacy review. It is off in the generic defaults and can add model/search usage.

Do not ask for passwords, tokens or chat authentication secrets in conversation. Use the platform's supported secure login/connection flow if the user authorises one. Missing credentials are a capability limit, not permission to create new access.

## 3. Install and enable

For a new installation, with user approval:

```bash
hermes plugins install Akuyumu/hermes-proactive-secretary --no-enable
hermes plugins enable hermes-proactive-secretary
hermes secretary doctor
```

The private repository must be accessible to the installer's Git transport. If it is not, stop and report the access requirement; do not bypass it. An immutable reviewed release may be pinned with `--ref <full-40-character-commit-sha>`.

The plugin bundles its operational skill at `skills/proactive-secretary/SKILL.md`. Load that installed file through your skill system if available; otherwise read it directly. Do not accidentally load an unrelated skill with the same short name.

## 4. Prepare configuration before arming schedules

```bash
hermes secretary setup --no-cron
hermes secretary status
```

This prepares owner-only policy/state and runs offline checks without installing schedules. The runtime root is `$HERMES_HOME/plugin-data/hermes-proactive-secretary/`, unless explicitly overridden with `HERMES_PROACTIVE_SECRETARY_DATA` or `--data-dir`.

Apply only the user's agreed changes to the plugin's `policy.json`, preserving other fields. Do not hand-edit Hermes's main configuration. Preserve existing policy/preferences; `--force` is not a normal setup step.

- Set the confirmed `timezone`, quiet hours and notification limits.
- Set a neutral or user-approved `persona`/`notify_address`; never assume the original owner's identity.
- Generic source seeds are empty. Add explicitly requested public watches with `hermes secretary watch add "<public topic>"`, then read back `hermes secretary watch list`.
- If adaptive discovery is approved, consult the bundled skill and policy implementation before changing mode. Keep inferred-goal acquisition and semantic privacy review paired; never send raw private context in public queries. Do not claim that adding a watch enables autonomous adaptation.
- Keep location off unless a supported integration and separate authorisation are verified.

### Connector limits — do not hide these

The plugin is profile/path-safe, but it does **not** automatically support arbitrary services:

- Mail currently expects existing Himalaya accounts named `formal`/`informal`. Arbitrary account names/providers are not automatically mapped. Do not rename accounts or expand access without permission.
- Calendar/tasks require the external `productivity-secretary` library and its existing authorised configuration. That library is **not bundled** in this repository. `SECRETARY_SRC` or `PRODUCTIVITY_SECRETARY_SRC` may point to an existing installation; do not create a new dependency or new credentials silently.
- Private obligation sources may depend on additional authorised native connectors. A healthy mail executable is not proof that every private source is available.
- Without supported connectors, public-only operation is possible, but empty seeds/watches and disabled adaptation do not mean useful public coverage is already established.

Read doctor/status capabilities and perform bounded authorised reads when appropriate. Report unavailable, failed, deferred and truncated coverage separately. Never promise that this plugin reads any arbitrary mailbox or calendar.

## 5. Arm the plugin's own schedules

After configuration and capability review:

```bash
hermes secretary setup --deliver '<verified-delivery-target>'
hermes secretary status
hermes secretary doctor
hermes secretary self-test
```

Use `--deliver local` when no messaging destination has been authorised. Never guess a chat ID or use `origin` as though a terminal command necessarily has chat context.

Setup creates and records ownership of:

- `hermes-proactive-secretary-pilot`: every 15 minutes; may use the native LLM for discovery/planning/judgment.
- `hermes-proactive-secretary-reminders`: every minute; deterministic reminder script, no scheduled-agent LLM calls.

Both are native script jobs with `--no-agent`; the monitoring script itself can invoke models. One-minute reminder checks use CPU and sometimes connector requests, not an LLM conversation per tick.

Verify exact script paths, schedules, ownership and delivery target through the native cron listing. Re-run setup with the same destination to verify idempotency; it must not duplicate jobs. A cron-name collision is a reason to stop, not permission to adopt someone else's job.

Run `hermes secretary run-once --fixture healthy_empty` for a no-send fixture check. `run-once` is no-model by default, not proof of successful model-driven monitoring. For a real no-send model check, follow the installed skill using the **Hermes runtime interpreter**, explicit plugin state/policy/preferences paths and `--dry-run`; system Python may lack Hermes dependencies. Do not invoke debug/diagnostic modes as a scheduled delivery script.

Do not create fake user-requested reminders or send a test message without approval. Empty output confirms no emitted message, not delivery or healthy collection. Report actual checks and remaining limits.

## 6. Existing deployment: deliberate migration only

Do not import another person's deployment. For the user's own existing tree:

1. Inspect and pause only the exact legacy recurring jobs; read back their paused state. Preserve unrelated one-shot reminders.
2. Snapshot durable state under owner-only permissions, avoiding in-flight writers.
3. Preview `hermes secretary setup --import-from '<legacy-tree>' --import-dry-run`.
4. Import using `--import-from '<legacy-tree>' --no-cron` and verify byte-level parity for policy, preferences, deduplication and registries before arming anything.
5. Arm plugin jobs, verify their run outcomes, and ensure legacy jobs are not also running.
6. Remove obsolete jobs only with explicit approval. Keep a rollback snapshot unless deletion is separately approved.

Import refuses conflicting target files and does not delete/adopt legacy jobs. If anything fails, report it and restore only the deliberately paused legacy schedules when safe; never reset registries or blindly retry uncertain delivery.

## 7. Pause, toggle and uninstall

```bash
hermes secretary pause
hermes secretary resume
```

Global pause stops collection and output, including requested reminders. Native plugin disable makes the scheduled runners inert at their next invocation; it does not remove their cron entries or cancel work already in flight.

For full uninstall, while the plugin CLI is available:

```bash
hermes secretary uninstall
hermes plugins disable hermes-proactive-secretary
hermes plugins remove hermes-proactive-secretary
```

Read back that only recorded owned jobs/scripts were removed. Runtime policy, preferences and reminder state are preserved by default. `hermes secretary uninstall --purge` deletes that state and requires separate explicit consent. Do not delete unrelated files or disable other plugins.

If the plugin was already disabled and its CLI is unavailable, do not blindly enable it: enabling can reactivate existing schedules. Inspect the jobs and arrange a safe, user-approved cleanup first.

Reinstallation is possible, but preserved global pause may still be active. Review state with the user, rerun setup and use `hermes secretary resume` only when they intend to resume monitoring.

## Completion report

Give one short report: installed version/revision; profile; verified coverage and missing connectors; timezone/quiet hours; destination; exact schedules; model-cost distinction; checks that actually passed; and pause/uninstall commands. Distinguish installation, successful execution, emitted output and confirmed delivery. Do not claim universal compatibility, complete monitoring or feature parity from synthetic tests.
