# Engineering

This document is the developer and host-owner view of Hermes Proactive Portal. It complements [Installation](INSTALLATION.md) and the read-only [portal behaviour guide](NATIVE-PORTAL.md). It contains no owner-specific identifiers, accounts, production summaries or private filesystem roots.

The package version is **1.0.0**. The native plugin id is `hermes-proactive-secretary`; the product and repository are now `hermes-proactive-portal`. Existing job names, state paths and CLI commands remain compatible. This is a branding and repository change, not an installed-state identity migration.

## Architecture ownership

The plugin is a real Hermes plugin. Its `plugin.yaml` registers:

- a native CLI command group (`hermes secretary`);
- native tools (`secretary_onboarding`, `secretary_context`, `secretary_responsibility`, `secretary_propose_notice`);
- a `pre_llm_call` hook;
- an operational skill and ordinary native Desktop settings contributions.

The plugin owns its configuration, policy copies, preferences, reminder registry, scheduler/runner ownership ledgers and preparation artifacts. It does **not** own Hermes's profile memory, permissions, connector credentials or approval system. There is no parallel memory or permission engine.

Runtime state is owner-only under the active profile's `plugin-data/hermes-proactive-secretary/`, unless an explicit operator override is used. Foreground and scheduled work resolve the same active profile. Missing profile context fails closed rather than borrowing the launch profile.

## Native session versus collectors

Two lanes are distinct:

1. **Collectors and deterministic runners.** The monitoring and reminder jobs are native `--no-agent` script jobs. The reminder runner is deterministic and makes no model call. Monitoring may invoke a host-owned model lane for discovery and judgment. Optional private preparation is a separate opt-in script job.
2. **The native portal session.** One optional, plugin-owned Hermes cron job runs as a normal native agent. It inherits the owning profile's identity, tools, connectors, skills, memory and standing approval rules. It is a separate scheduled run, not a literal foreground session id.

The portal's native final output stays local. When something warrants the user's attention, the session uses the guarded `secretary_propose_notice` tool to queue a proposal for the existing monitoring lane. A proposal is not completion or delivery. Notice delivery remains subject to quiet hours, cooldowns and the optional daily cap. The portal must not send through two paths.

Setup, settings saves and registration do not arm the portal. Preview and enable are explicit and require a schedule; there is no hidden default cadence. Pause stops later runs but is not a proven hard cancellation of a run already in progress in the native transport.

## Host prerequisites

### Configured-only auxiliary contract

Model-dependent preparation and other strict tasks require a host that exposes the configured-only auxiliary contract — specifically an auxiliary task registration/read path honouring `fallback_policy="configured_only"`.

When that contract is absent, the plugin must report the lane as unsupported and make no model call. It must not fabricate an artifact, a fallback ladder or a model-outage notice. Current tooling deliberately avoids blanket route fallback. A change must not reintroduce an implicit main/global/chat escalation.

### Native list readback

Portal reconciliation is bounded by what the native job listing actually exposes. Report state honestly:

- **Mode** — a missing mode is fail-closed: `enable` must refuse rather than infer native agent mode.
- **Prompt** — a stored single-line prompt may be exposed as `Prompt`. A multi-line prompt is not collapsed, rewritten or truncated merely to fit that row, so it remains inconclusive there.
- **Context from** — nonempty native context references appear as `Context from`, for example `self`; an absent self/context reference is inconclusive.

If the listing does not expose a field, `preview` must mark it inconclusive and must not present settings as applied state. Missing mode fails enable; absent prompt or self-context is not evidence of another mode or context. Verify the persisted record through the native job surface where available.

### Approval and MCP semantics

The portal inherits native MCP trust and approval behaviour. Native MCP tools retain full trust outside `cron_mode`; consequential external effects still require the applicable foreground approval. The work contract prompt is guidance, **not enforcement**, and the portal is not a security sandbox. Any external send, purchase, publication, booking or calendar/task mutation remains an approval-required action.

## Host compatibility patch

Some Hermes versions need a host-side compatibility update for the strict configured-only auxiliary lane. The patch included with this package is [docs/compatibility/hermes-host-portal.patch](compatibility/hermes-host-portal.patch). It was reviewed against Hermes base:

```text
2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19
```

This is not a universal or zero-core-change claim. Do not copy or apply the patch to a different or newer checkout without re-review.

### Safe application

Before applying, confirm all of the following:

- the host source tree is clean and owned by the operator making the change;
- you have explicit authority to change the host;
- the expected base commit matches the reviewed base above;
- a rollback copy or owned diff is available.

Then check first, without writing:

```bash
git -C <hermes-source> apply --check <path-to-docs/compatibility/hermes-host-portal.patch>
```

Apply only after a clean check and explicit approval:

```bash
git -C <hermes-source> apply <path-to-docs/compatibility/hermes-host-portal.patch>
```

Record the exact files changed. A local host patch must survive future Hermes updates: after each host upgrade, re-check compatibility and re-apply only a re-reviewed patch. Do not assume the change carries forward.

### Reverse and rollback

Reverse only the exact owned diff. Check first:

```bash
git -C <hermes-source> apply --reverse --check <path-to-docs/compatibility/hermes-host-portal.patch>
```

Then reverse when it is safe:

```bash
git -C <hermes-source> apply --reverse <path-to-docs/compatibility/hermes-host-portal.patch>
```

Never use `git reset --hard`, `git clean`, or a blanket checkout to undo this change. If the working tree has diverged, stop and reconcile the owned diff manually rather than discarding unrelated work.

## Release and packaging

Package version and metadata are authoritative in `plugin.yaml` and the release-export inputs; this documentation does not set them. `main` is the production/stable branch; a `dev` branch, if used later, is experimental and not the live install source. Release tag `v1.0.0` marks the tested `main` commit. For an immutable pin, resolve the immutable 40-character commit SHA and pass it through the native `--ref` argument.

The exporter builds a deterministic code-only archive and excludes tests, runtime state and `.build-context`. Its scan and doctor checks validate packaging only. They do not establish live connector access, model availability, message delivery or Desktop/device behaviour. Do not claim that every service or source always works, and do not infer universal third-party availability from a green packaging run.

## Responsible changes

Preserve existing approvals, private state and profile scope. Do not broaden permissions or connectors implicitly, do not introduce a separate restricted assistant, and do not weaken sanitiser, export or privacy boundaries to make an artifact pass. Keep documentation honest about unavailable, failed, deferred and truncated coverage.
