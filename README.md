# Proactive Secretary for Hermes

**Keep an eye on what matters, without checking everything yourself.**

Proactive Secretary gives [Hermes](https://hermes-agent.nousresearch.com) a background attention layer: check the sources you choose, look for something worth mentioning, and prepare useful work when you have asked for it. The aim is fewer unnecessary interruptions—not another stream of notifications.

You choose what it can read and how much attention it can ask for. It starts with no connected accounts, assumed interests or assumed location. It can deliver notices through your configured Hermes notification route, but it never sends emails, changes calendars or tasks, books, buys or publishes on your behalf.

> **Project status:** pre-1.0. This local candidate adds optional context refresh, isolated public-page rendering and fairer source rotation beyond the published 0.6.2 release. These changes are not yet published; installing from the repository does not retrieve unpublished local work. Features depend on your permissions, working integrations and Hermes compatibility.

## What can it do for you?

| You want… | The secretary helps by… |
|---|---|
| Less checking | Periodically reviewing authorised mail, calendar, tasks and public sources for things that need attention. |
| A reminder you actually requested | Keeping a durable reminder for a specified time or an event on an authorised calendar. |
| A head start on work | Preparing a private, evidence-backed draft for a responsibility you explicitly accepted. |
| Updates on your interests | Watching chosen public topics and sources, with optional discovery and source upkeep. |
| More relevant monitoring | Using optional, compact advisory notes from your conversations—without treating them as permission. |

These examples describe supported uses, not guarantees of coverage or delivery.

## 1. Notice what deserves your attention

Connect only the sources you want checked. The secretary can review mail metadata and short redacted previews, upcoming calendar events, dated tasks, and supported public feeds or pages. Private sources need separately authorised, working adapters; public-source monitoring can work without a private connector.

Checks are periodic, not real-time. Quiet hours, a daily ceiling, spacing between messages and duplicate suppression help keep ordinary alerts manageable. The ceiling is a maximum, never a quota to fill. When nothing qualifies, silence is the intended result.

It also distinguishes a successful check from a blocked, unavailable or incomplete source. An unreadable source is not evidence that nothing happened.

## 2. Remember what you explicitly asked for

Requested reminders are separate from general monitoring. You can ask your assistant to register a reminder for an exact time or relative to an event on an authorised calendar. Once registered, reminders are stored durably and dispatched without a model decision.

Because you asked for them, they bypass ordinary alert caps, quiet hours and cooldowns. **A global pause still stops them.** They may fire late after you resume, and delivery depends on the scheduler and messaging route; exact punctuality is not guaranteed.

## 3. Prepare privately—without taking over

If you opt into private preparation, the secretary can work on a responsibility you clearly accepted: for example, preparing a renewal checklist or a short briefing from available evidence.

It produces a bounded private draft with citations, and a later notice may tell you the work is ready. It does not accept responsibilities from ambiguous conversation, turn drafts into finished user documents, or execute the next step. Sending, publishing, booking, purchasing or changing a calendar or task remains a separate foreground decision under your control.

Preparation is off by default, needs explicit scope and a compatible model configuration, and has its own optional scheduled job.

## 4. Follow public topics and keep sources useful

Choose public topics, feeds or supported pages you care about. Optional discovery can look for related public material within the permissions and limits you set. Supported formats include release feeds, RSS/Atom, Reddit RSS, ordinary public pages and selected official information feeds—not every website or social platform.

**Less repeated checking, more useful coverage.** Ordinary feeds and adaptive topic checks share the same attempt limit, with reserved turns when both are active. Sources rotate independently for each watch, and checks deferred for lack of time do not spend a source-attempt slot or count as a fresh successful check. Limits still apply: this improves fairness, not a promise to check every source every cycle.

**An optional second look at JavaScript pages.** When enabled with consent, a bounded browser fallback can render supported public HTTPS pages in a fresh isolated session—not your signed-in browser. It requires compatible Playwright and Chromium already installed, has time/request/response-body/output limits, and refuses private-network targets and non-GET requests. It does not log in, bypass challenges or support WebSocket-dependent content. If the browser or a required safeguard is unavailable, the fallback fails rather than claiming it read the page. See [public-page rendering](docs/native-rendering.md) for prerequisites and limits.

With separate consent, public-source upkeep can try to replace a persistently broken source. It must verify an equivalent source with usable evidence; a same-topic search result is not enough. The original is parked rather than deleted, and you can dispute the replacement and restore it.

Feedback can refine an interest or temporarily snooze it. A temporary snooze expires; silence or “thanks” is not a request to stop. Proposed settings changes stay proposals until you approve them.

Public discovery is designed to keep private facts out of search queries. Permission to read public sources never grants access to a mailbox, calendar or other private source. Unsupported routes remain limitations, not hidden successes.

## 5. Keep useful context without importing everything

**Optional context refresh** lets your assistant maintain a small, expiring set of advisory notes from context already available in a private conversation: preferences, interests or ongoing projects that may help the secretary judge relevance.

Enable **Allow assistant context refresh** in the plugin settings to use it. It is off by default. The assistant may add or update notes, but:

- It does not import your whole vault, memory store or chat history.
- Notes have size limits and expiry, and can be revoked.
- Advisory notes cannot overwrite owner-declared notes or grant access.
- The assistant may miss something relevant or interpret it incorrectly.

Refresh adds no separate model or network call. Disabling it or pausing stops new writes; existing notes remain until revoked or expired. See [context refresh](docs/context-refresh.md) for details.

## You control the pace and permissions

Setup is conversational: on a fresh profile, an eligible one-to-one chat starts an interview about your preferences and source permissions. `/secretary setup` is also available. The optional Desktop settings page groups setup, sources and notification controls; the command line remains available for inspection and repair.

| Chat control | What it does |
|---|---|
| `/secretary status` | Show setup, jobs, limits and readiness. |
| `/secretary pause` | Stop collection and delivery, including requested reminders. |
| `/secretary resume` | Clear the pause; it does not invent missing jobs or permissions. |
| `/secretary disable` | Pause and remove only this plugin’s owned jobs and runners. |

**Installing, configuring, scheduling and confirming delivery are different steps.** Finishing onboarding does not schedule checks, and saving a destination does not prove a message reached you.

## Get started

**Desktop:** [Install in Hermes](hermes://plugin/install?repo=Akuyumu/hermes-proactive-secretary&enable=1), or use **Capabilities → Plugins → Install from Git** with `Akuyumu/hermes-proactive-secretary`.

**Terminal:**

```bash
hermes plugins install Akuyumu/hermes-proactive-secretary --enable
hermes secretary doctor
```

The repository is currently private, so the installing machine needs Git access. Follow the native dependency and capability prompts, then complete onboarding and review your settings.

When you are ready to schedule checks, start with local-only output:

```bash
hermes secretary setup --deliver local
```

This schedules work but sends no chat messages. Move to an existing delivery target such as Telegram only after reviewing the destination. Private preparation needs an additional explicit opt-in. The [operations guide](docs/operations.md) covers these steps and delivery verification.

## A few honest limits

This is not a universal personal assistant running unattended. It cannot guarantee complete coverage, correct judgment, successful delivery or that nothing was missed. Private drafts need your review. Some features depend on adapters and compatible Hermes model routing; deterministic reminders and controls can still work when model-based features are unavailable.

Read-only does not mean offline: public checks contact external services, and model-assisted judgment or preparation may send bounded evidence to your configured model provider. Context notes are not a replacement for durable knowledge management.

## Details when you need them

- [Operations guide](docs/operations.md): setup, integrations, settings, reminders and troubleshooting.
- [Context refresh](docs/context-refresh.md): advisory notes, controls and limits.
- [Adaptive maintenance](docs/adaptive-maintenance.md): source replacement and feedback, including qualification limits.
- [Notification contract](docs/notification-contract.md): how interruption limits and delivery claims work.
- [Onboarding design](docs/agentic-onboarding-design.md): the conversational setup experience.
- [Agent setup](AGENT_SETUP.md) and [operational skill](skills/proactive-secretary/SKILL.md): assistant-guided installation.
- [GitHub Releases](https://github.com/Akuyumu/hermes-proactive-secretary/releases): published changes and qualification notes.

## License

MIT. See [LICENSE](LICENSE).
