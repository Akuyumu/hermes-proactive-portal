# Native public rendering (opt-in, isolated)

> **Status: candidate implementation, not yet accepted.** This document
> describes an isolated candidate branch. It does not claim that the renderer is
> production-safe or that real public rendering has passed acceptance. The
> parent harness owns real public rendering and negative acceptance.

## What it is (and is not)

The plugin can render ONE public HTTPS JavaScript-shell page through a
plugin-owned adapter. It is off unless the owner enables **both**
`native_render_fallback_enabled` and `native_render_consent`.

It deliberately does **not** reuse the owner's Hermes browser tool, session,
cookies, storage state or profile. It invents no native host API and embeds no
new endpoint, key or OAuth client.

## Availability

- `native_renderer_availability(policy) -> (bool, reason)` is a cheap policy
  check. It never opens a browser, imports/probes Playwright, reads the store,
  or makes a network/model call. A `True` result means **configured/eligible**,
  not that dependency health was verified; absent Playwright/Chromium fails
  honestly at render time.
- `build_native_renderer(policy)` returns the bounded adapter only when both
  flags are enabled; otherwise it returns `None` so the neutral collector path
  is unchanged.
- An explicit nonpositive, non-finite or malformed residual timeout is rejected
  before any tempdir or child process is created. The configured default applies
  only when the caller omits a timeout.
- If Playwright or a usable Chromium is absent, a real render fails with an
  explicit unavailable error. It never silently substitutes the owner browser
  and never fabricates content.

## Isolation and bounds

Each render runs in a separate worker process started with the same managed
Python (`sys.executable`). Only the public URL and bounded, non-secret limits are
passed on stdin. The worker environment is a minimal allowlist: trusted runtime
and dependency paths plus a temporary `HOME`/`TMPDIR`; owner auth/API-key
variables are not inherited. No `.env`, config or credential store is loaded.

The fresh, non-persistent Chromium context:

- blocks service workers and downloads, and closes popups;
- has no persistent user-data directory and no storage state;
- requests the Chromium sandbox;
- uses a fail-closed proxy with the implicit loopback bypass explicitly removed,
  no host-resolution escape, and disables QUIC and non-proxied WebRTC.

Request mediation is registered at the **BrowserContext** level, not just one
page, so the first navigation of every page/popup in that context is covered
before untrusted content runs. If the context-level request or WebSocket
mediation API is missing, the render fails closed rather than silently ignoring
it.

WebSocket interception is also required. The deny handler does **not** call
`connect_to_server` or `send` and does not synchronously close the route (which
deadlocks Playwright's sync API on real pages). Instead it registers a no-op
message handler, so Playwright keeps a mocked browser-side/blackholed socket:
outbound messages are discarded and no remote WebSocket connection is made.
This is a local mocked socket, not a remotely closed or successfully opened
WebSocket.

Every document, redirect, subresource and XHR request is fulfilled **only**
through a renderer-local bounded subclass of the existing
`PinnedHTTPSTransport`, which re-validates and re-pins each hop. Non-GET actions,
non-HTTPS schemes (including `data:`, `blob:`, `file:` and WebSocket schemes),
private/mixed targets, private redirects, private subresources and DNS rebinding
are aborted. `Set-Cookie` and hop-by-hop/encoding headers are stripped before a
response is handed to the browser.

Required caps are enforced for:

- overall wall-clock time (the caller's residual per-source timeout is honoured,
  never a closure-wide budget when the source has less);
- request count, counting every redirect hop as its own request;
- cumulative response-body bytes across every redirect hop, including the
  transport's clipped-body probe byte, checked BEFORE any later fetch starts;
- rendered output bytes.

Header/TLS wire bytes are deliberately **not** claimed or counted. On timeout or
crash the parent TERM/grace/KILLs and reaps the whole owned worker process group
— even if the group leader already exited — and never signals the parent or an
unowned group. Browser/context cleanup runs in `finally`.

## Residual limits (candid)

- This is bounded supported rendering, not universal rendering. Some pages,
  authentication prompts, paywalls, challenges, unsupported resource types or
  bot defenses will remain unavailable. Challenges/login/paywalls are never
  bypassed. Local/non-HTTPS content is outside the supported boundary.
- A deny-by-default browser configuration plus context-level mediated requests is
  defence in depth; it is not a proof of universal network closure on every
  Chromium build. Request counting is per HTTP hop, not per low-level connect
  retry, and only response-body bytes are budgeted.
- WebSocket denial is a blackholed local/mock socket with no protocol replies;
  it is not a claimed remote close or successful connection.
- The parent acceptance suite has not yet certified real public rendering. Until
  then, treat this as an untested candidate rather than a claim of safety.
- Rendering executes untrusted page JavaScript inside the isolated browser
  sandbox. The worker is disposable and bounded, but no renderer can guarantee
  perfect isolation of a browser engine.
