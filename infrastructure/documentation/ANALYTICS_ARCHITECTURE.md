# Analytics Architecture: GTM, GA4 and Consent Mode v2

## Executive Summary

- **Google Tag Manager** is the single tag container for the hosted web frontend. Tags are managed in the GTM console, not in code.
- **GA4 is measured through GTM only.** The frontend contains no GA4 or Firebase Analytics SDK, so there is no second measurement path to double-count.
- **Consent Mode v2** defaults every signal to `denied`. No application event is pushed to `dataLayer` until the visitor answers the consent notice.
- **The whole subsystem is inert unless `REACT_APP_GTM_ID` is set at build time.** With it unset, `gtm.js` is never requested, the consent notice never renders, and `trackEvent` is a no-op.
- **Standalone (local) installs are excluded.** Local builds must leave `REACT_APP_GTM_ID` unset, and the route tracker, event middleware and consent notice all suppress themselves when the backend reports standalone mode.

---

## Key Architectural Principles

1. **The SPA owns 100% of pageviews.** The GA4 configuration tag must have its automatic `page_view` turned OFF. `route_change` is the only pageview signal. Leaving the automatic pageview on double-counts every navigation.
2. **No personal data leaves the browser.** `page_path` and `page_location` are always sanitized: query strings are dropped and numeric path segments are collapsed to `:id`. The GA4 tag must be configured to use these values rather than its defaults.
3. **Consent is fail-closed on the signal side.** The consent update is issued through the `gtag` shim defined in `frontend/public/index.html`. If that shim is missing, the signal stays `denied` rather than being assumed granted. Note this governs the Consent Mode signal, not whether `dataLayer` receives application events - those are gated separately on the stored decision.
4. **Custom events are emitted from one place.** A Redux middleware maps fulfilled thunk action types to event names, so no feature component contains event-tracking code and every present or future dispatcher is covered. Pageviews and the public-repository view are the two exceptions: they are not Redux actions, so they come from their own components.
5. **The container ID is a build-time value.** It is baked into `index.html` by CRA's HTML interpolation, so it cannot be changed at runtime and cannot be varied per request.

---

## Architecture Overview

```
public/index.html  (inline, <head>, before the bundle)
  dataLayer = []  ->  gtag shim  ->  consent default: all denied, wait_for_update 500
  guarded loader:  /^GTM-[A-Z0-9]+$/  ->  gtm.js   (skipped when the ID is absent)
        |
        v
index.tsx
  initAnalyticsConsent()   re-applies a stored decision before the first render
  <ConsentBanner/>         sibling of <App/>, wrapped in ErrorBoundary
        |
        v
utils/analytics.ts   the only writer to dataLayer
  no decision yet  ->  buffer (FIFO, max 10)
  granted          ->  push { ...params, event }
  denied           ->  drop
        ^                        ^
        |                        |
components/common/          store/analyticsMiddleware.ts
RouteChangeTracker          sign_up / login / run_pipeline
route_change                pages/PublicDataview -> view_public_data
```

| Component                                  | Responsibility                                                                    |
| ------------------------------------------ | --------------------------------------------------------------------------------- |
| `public/index.html`                        | Consent Mode defaults, `gtag` shim, guarded container load                        |
| `utils/analytics.ts`                       | Guard, consent state, buffering, path sanitization, the single `dataLayer` writer |
| `components/common/ConsentBanner.tsx`      | Collects and persists the first decision                                          |
| `pages/Account/index.tsx`                  | Analytics Cookies switch: withdraws or re-grants an existing decision             |
| `components/common/RouteChangeTracker.tsx` | Pageviews on route transitions                                                    |
| `store/analyticsMiddleware.ts`             | Maps fulfilled thunks to custom events                                            |
| `infrastructure/scripts/ecr_build_push.sh` | Supplies the per-environment container ID at build time                           |

---

## Implementation Details

The head snippet runs before the deferred CRA bundle, so `window.dataLayer` and the `gtag` shim always exist by the time application code runs, and the Consent Mode default is always registered before `gtm.js` can load. The loader is a no-op unless the compiled-in container ID matches the guard, which is what makes an un-configured build byte-for-byte inert at runtime.

Inside the bundle, `initAnalyticsConsent()` runs at module scope in `index.tsx`, before `root.render`, so a returning visitor's decision is applied before the first event can be produced. `trackEvent` is the single writer to `dataLayer`; it holds events in a FIFO buffer (max 10) while no decision exists, flushes them in order on a grant, and discards them on a denial. That buffer is why a first-time visitor's entry pageview is still recorded if they accept, and why nothing at all is recorded if they decline.

Withdrawal needs no reload. `trackEvent` calls `getAnalyticsConsent()` on every event and that reads the in-memory session value before `localStorage`, so flipping the `/account` switch off stops the very next event. The switch appears only once a decision exists, which is what keeps it and the notice from ever describing different states without any shared state between them.

`RouteChangeTracker` deduplicates on the raw `location.pathname` but pushes the normalized value, so navigating between two different workspace ids is two pageviews rather than one. `analyticsMiddleware` calls `next(action)` first and never alters the action or the dispatch result; it looks up event names by exact action type, so sibling thunks that merely share a type prefix are not captured.

---

## Key Functions Reference

| Function                        | Purpose                                                                                                       |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `isGtmEnabled()`                | Whether a well-formed container ID was compiled in. Every other entry point returns early when false          |
| `getAnalyticsConsent()`         | Current decision: in-memory session value first, then localStorage; unrecognised values read as "no decision" |
| `setAnalyticsConsent(decision)` | Persists, emits the Consent Mode update, and flushes or discards the buffer                                   |
| `initAnalyticsConsent()`        | Re-applies a stored decision at bundle start, before the first event                                          |
| `trackEvent(event, params?)`    | The only `dataLayer` writer. Buffers while undecided, drops when denied                                       |
| `normalizePath(pathname)`       | Collapses numeric segments to `:id` to bound GA4 cardinality and keep internal ids out                        |

---

## Monitoring and Metrics

No CloudWatch metrics or alarms: this subsystem is entirely client-side and emits no backend telemetry. Observability is the GA4 property itself, plus GTM Preview and GA4 DebugView for verification.

---

## Configuration

| Variable                         | Where it is set                           | Effect when unset                                                |
| -------------------------------- | ----------------------------------------- | ---------------------------------------------------------------- |
| `REACT_APP_GTM_ID_<ENVIRONMENT>` | `infrastructure/.env.deploy` (gitignored) | GTM, GA4, the consent notice and all event tracking are disabled |

`ecr_build_push.sh` selects the entry matching the Terraform `environment` output, uppercased: production (`subscr`) reads `REACT_APP_GTM_ID_SUBSCR`, development reads `REACT_APP_GTM_ID_DEVELOPMENT`. Per-environment keys prevent a development build from being published with the production container, which would pollute the property permanently. The file is sourced in a subshell, so unrelated assignments in it cannot affect the build.

```
# infrastructure/.env.deploy
REACT_APP_GTM_ID_SUBSCR=GTM-XXXXXXX
REACT_APP_GTM_ID_DEVELOPMENT=GTM-YYYYYYY
```

The script rewrites `frontend/.env.production` on every build, so editing that file by hand does not survive a deploy.

Format: `GTM-` followed by uppercase letters and digits, enforced identically in three places - the deploy script (hard-fails the build on a malformed value), `isGtmEnabled()` in `frontend/src/utils/analytics.ts`, and the loader guard in `frontend/public/index.html`. The un-interpolated literal `%REACT_APP_GTM_ID%` that CRA leaves behind when the variable is absent fails all three.

---

## Events

| Event              | Emitted by            | Trigger                                                                                                    |
| ------------------ | --------------------- | ---------------------------------------------------------------------------------------------------------- |
| `route_change`     | `RouteChangeTracker`  | Initial location and every subsequent distinct pathname. Carries sanitized `page_path` and `page_location` |
| `sign_up`          | `analyticsMiddleware` | `registerUser.fulfilled`                                                                                   |
| `login`            | `analyticsMiddleware` | `login.fulfilled`, excluding admin impersonation                                                           |
| `run_pipeline`     | `analyticsMiddleware` | `run.fulfilled` or `runByCurrentUid.fulfilled`                                                             |
| `view_public_data` | `PublicDataview`      | Mount of the `/public` page                                                                                |

No event carries parameters other than `route_change`'s sanitized path fields.

---

## Before setting a container ID

Setting `REACT_APP_GTM_ID` is what activates every risk in this document. Until it is set there is nothing to review: no third-party request, no notice, no `dataLayer` writes. The items below are blocking, and none of them can be satisfied from this repository alone.

### Access and supply chain

1. **Restrict GTM container publish rights to a named, minimal set of people, with 2FA enforced on those Google accounts.** GTM is by design a mechanism for injecting arbitrary JavaScript into the page from a console outside this repository. That page runs authenticated scientific workflows and admin impersonation (`proxyLogin`), so publish rights on the container are equivalent to code execution in an authenticated session. This is the single largest security consideration of the whole feature.
2. **Prefer a locked-down container.** Avoid Custom HTML tags; if the threat model warrants it, use server-side GTM.
3. **Decide and record the CSP posture.** There is currently **no Content-Security-Policy anywhere in this repository** - no meta tag in `index.html`, no nginx layer, no CloudFront distribution, and the ALB rules in `infrastructure/terraform/public_alb_rules.tf` only match request headers, they do not add response headers. The absence pre-dates this subsystem (the app already loads `fonts.googleapis.com`), and adding GTM does not preclude a nonce-based CSP later, but GTM materially raises what an injection is worth. Either add one following Google's CSP guidance for GTM, or record the decision to accept the current posture.

### Compliance

4. **Link the privacy policy from the consent notice.** GDPR and ePrivacy expect the notice to link to the policy describing the processing. The Terms of Service and Privacy Policy pages are being added separately; wire the link in once those routes exist.
5. **Have the notice copy reviewed.** It has had no legal review.
6. **Confirm the consent controls still meet "withdrawal as easy as granting".** Decline and Accept carry identical styling in the notice, and `/account` offers an Analytics Cookies switch that both withdraws and re-grants, taking effect immediately without a reload. A restyle of either surface can regress this.

### Verification, once a container exists

The client-side test suite stops at `window.dataLayer`. A correct `dataLayer` does **not** prove correct GA4 data: every automated test still passes if the GA4 tag keeps its automatic `page_view`, if the `page_location` override is missing, or if a custom-event trigger was never created. Run these in GTM Preview and GA4 DebugView / Realtime and record the results.

| #   | Case                                          | Expected result                                                                                                                                 |
| --- | --------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | One client-side route change, GTM Preview     | The GA4 configuration tag fires exactly once and sends `page_view`; `page_location` is the sanitized value with no query string                 |
| 2   | Exercise all five events, GA4 DebugView       | `route_change` (as `page_view`), `sign_up`, `login`, `run_pipeline` and `view_public_data` all appear, with correct names and no PII parameters |
| 3   | Two navigations, GA4 Realtime                 | Exactly two pageviews. Three means the automatic `page_view` was left on                                                                        |
| 4   | Decline at the notice, then navigate          | DebugView receives nothing at all                                                                                                               |
| 5   | Visit `/account-manager?email=...`, DebugView | `page_location` carries no email and no query string                                                                                            |
| 6   | Trigger cross-check in the container          | All four custom-event triggers exist and fire                                                                                                   |

---

## Required GTM container configuration

The code side is inert without these console-side settings. Configure them **before** setting `REACT_APP_GTM_ID`, or measurement will be wrong from the first hit.

1. **GA4 configuration tag: automatic `page_view` OFF.** Add a Custom Event trigger on `route_change` and send `page_view` from it. Leaving the built-in pageview on produces two pageviews per navigation.
2. **Override `page_location` and `page_title` on the GA4 tag** using the `page_location` and `page_path` dataLayer variables. GA4 otherwise defaults `page_location` to `document.location.href`, and two live routes carry personal data in their query strings: `/account-manager?email=...&name=...` and `/subscription/thanks?session_id=...`. The frontend cannot prevent that from the tag's own default.
3. **Enable GA4 "Redact data"** (IP and query-string redaction) as defence in depth.
4. **Create Custom Event triggers** for `sign_up`, `login`, `run_pipeline` and `view_public_data`.
5. **Do not put tags on the Initialization or All Pages triggers.** See the returning-visitor row under Edge Case Handling.
6. **`sign_up` fires when registration is submitted, not when the email is verified.** Verification is a separate later step, so this metric counts attempted registrations.

---

## Edge Case Handling

| Case                                                                        | Behaviour                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Visitor has not answered the notice                                         | Events are buffered in memory (up to 10, in order) and pushed only if the visitor accepts. Declining discards them and every later event is a no-op.                                                                                                                                                                                                                                                                                                                                                                     |
| Visitor declines                                                            | No `route_change`, `sign_up`, `login`, `run_pipeline` or `view_public_data` event is ever pushed. The container is still loaded and `analytics_storage` stays `denied`, so whether a tag on an Initialization or All Pages trigger emits a cookieless ping is decided entirely by the container configuration, not by this code.                                                                                                                                                                                         |
| Returning visitor who accepted                                              | The stored decision is re-applied at bundle start, before any event is pushed. `wait_for_update: 500` only helps a warm cache: the main chunk is ~2 MB gzipped, so on a cold load the wait expires first. Any tag on an Initialization or All Pages trigger will therefore evaluate against `denied` for a returning visitor and produce a cookieless, unattributed hit. Keep such tags out of the container; if one is ever needed, the stored grant must be replayed inline in `index.html` before the loader instead. |
| `localStorage` refuses writes (Safari "block all cookies", private modes)   | The decision is held in memory for the session, so tracking works as chosen; the notice reappears on the next reload.                                                                                                                                                                                                                                                                                                                                                                                                    |
| Corrupted stored value                                                      | Treated as "no decision"; the notice is shown again.                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| Backend is unreachable                                                      | The router never mounts, so no pageview is recorded for the error screen, and the consent notice is not shown over it. Deliberate.                                                                                                                                                                                                                                                                                                                                                                                       |
| Client-side redirect on load, e.g. unauthenticated `/dashboard` to `/login` | Both paths are recorded. Known inflation, inherent to client-side routing.                                                                                                                                                                                                                                                                                                                                                                                                                                               |

---

## Known gaps

- **The notice does not link to a privacy policy.** Terms and privacy pages are being added separately; link them from the notice once they exist.
- **Consent does not sync across tabs.** Granting in one tab leaves another tab's notice up until it reloads.
- **`canonical` and `og:url` in `index.html` are hardcoded to the site root**, so `/login`, `/register` and `/public` in `sitemap.xml` self-canonicalize to `/`. Submitting that sitemap to Search Console will report those URLs as duplicates. Fix the canonical handling before submitting the sitemap.
- **Search Console ownership is not verified.** DNS TXT or a `google-site-verification` meta tag are the robust methods; the GTM method additionally requires publish rights on the container.
- **e2e consent seeding is only partial.** `frontend/e2e/global-setup.ts` seeds `analyticsConsent=denied` into the authenticated storage state, but specs that build their own unauthenticated context (the login and registration flows) will still meet the notice once a container ID is set for the tested build.

---

## Testing

| Suite                                                                  | Covers                                                                                                                                                                                                                                        |
| ---------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `frontend/src/utils/__tests__/analytics.test.ts`                       | Guard, consent storage, buffering and flush, path sanitization. Also reads the inline scripts out of `public/index.html` and evaluates them, so the `gtag` shim, the consent defaults and the loader guard are asserted rather than described |
| `frontend/src/components/common/__tests__/ConsentBanner.test.tsx`      | Visibility rules, both decisions, equal button prominence, accessibility role                                                                                                                                                                 |
| `frontend/src/pages/Account/__tests__/AnalyticsConsent.test.tsx`       | The withdrawal switch: visibility rules, withdrawal and re-grant, and that withdrawal silences the next event without a reload                                                                                                                |
| `frontend/src/components/common/__tests__/RouteChangeTracker.test.tsx` | Initial and subsequent pageviews, dedupe, query-string exclusion, standalone suppression                                                                                                                                                      |
| `frontend/src/store/__tests__/analyticsMiddleware.test.ts`             | Action-to-event mapping, impersonation exclusion, standalone suppression                                                                                                                                                                      |

Verify end to end with GTM Preview and GA4 DebugView after setting the container ID: confirm exactly one pageview per navigation and that no query string appears in `page_location`.
