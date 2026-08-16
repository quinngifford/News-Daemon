# Trump Death Watcher — iPhone app

A native SwiftUI port of the web app in `backend/web/`. Same screens, same
copy, same broadsheet, same API — the backend was built for this: auth is a
bearer token and there are no server-side sessions, so nothing here needed a
new endpoint.

```bash
open ios/TrumpDeathWatcher.xcodeproj
```

Requires Xcode 16 or newer and iOS 17 on the device. **It has never been
compiled** — it was written on Linux, where no iOS toolchain exists. Expect to
fix a small number of build errors on the first run; the logic and layout are
what carry over, not a green build badge.

## Point it at a backend

`TrumpDeathWatcher/Info.plist` → `TDWAPIBaseURL`. It ships as
`http://127.0.0.1:8000`, which is a laptop running:

```bash
cd backend && .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

The simulator reaches `127.0.0.1` directly. A real device needs the Mac's LAN
address (`http://192.168.x.x:8000`) and an ATS exception for it — the plist
grants insecure HTTP to `localhost` and `127.0.0.1` only, deliberately, because
a bearer token over cleartext on a hostile network hands someone else's alerts
away. Set the public https URL before archiving.

Without Stripe keys the paywall shows **Dev — Grant Without Payment**, exactly
as the web app does, so the whole flow is usable immediately.

## What it does

| | |
|---|---|
| Vital status board | `ALIVE` / `UNCONFIRMED` / `DEAD`, live clock, corroboration count, the dead-state inversion and flash |
| Market chart | `$TRUMP`, 7D/30D/90D, hatched fill, drag to inspect |
| Memecoin chart | `$TRUMPDEAD`, 1H/6H/24H/7D, high/low/volume, pump.fun link |
| The wire | Related coverage, refreshed every five minutes |
| Sign in / register | Same validation, same errors |
| Purchase | Stripe Checkout in a Safari view, then server-side verification |
| Live stream | SSE while the app is open |
| Push | APNs, registered as `kind: "apns"` |
| Chime | The same 880→1180 Hz square wave, five for confirmed, two for likely |

Signed out is a valid way to read the paper. The status board and both charts
are public; only delivery — push, the live stream, the chime — is bought. That
is the web app's rule and the server enforces it either way.

The **Dispatches** section is off, because it is commented out in
`web/index.html`. `DispatchRow` and the state behind it are still here, and
`showDispatches` in `PaperView.swift` turns it back on.

## Notifications

The app registers with `POST /api/push/register` as `kind: "apns"`, and
`_send_apns` in `backend/app/push.py` delivers. Set on the backend:

| Key | Where it comes from |
|---|---|
| `APNS_KEY_ID`, `APNS_PRIVATE_KEY` | Apple Developer → Keys → new key with APNs enabled. The `.p8` downloads once. `APNS_PRIVATE_KEY` holds its contents, not a path. |
| `APNS_TEAM_ID` | Apple Developer → Membership |
| `APNS_TOPIC` | The bundle id, `com.trumpdeathwatcher.app` |
| `APNS_USE_SANDBOX` | `true` for development builds, `false` for TestFlight and the App Store |

`APNS_USE_SANDBOX` must match `aps-environment` in
`TrumpDeathWatcher.entitlements`. Mismatched, every push fails with
`BadDeviceToken` — and the device still reports a successful registration, so
the person believes they are covered.

Confirmed dispatches are sent **time-sensitive**, which breaks through Focus.
That is the ceiling without asking Apple for anything.

**Critical alerts** — the ones that pierce silent mode, which is the reason
this app exists rather than the PWA — need
`com.apple.developer.usernotifications.critical-alerts`, granted by
[application to Apple](https://developer.apple.com/contact/request/notifications-critical-alerts-entitlement/).
Once granted, add that key to the entitlements file and change the `aps` block
in `_send_apns`:

```python
"interruption-level": "critical",
"sound": {"critical": 1, "name": "default", "volume": 1.0},
```

Push cannot be tested in the simulator. It has no APNs; the button says so.

## Signing

Automatic signing, no team set — pick yours in **Signing & Capabilities**, and
change `PRODUCT_BUNDLE_IDENTIFIER` if `com.trumpdeathwatcher.app` is taken.

Two entitlements need a paid Apple Developer account: push notifications, and
time-sensitive notifications. If signing fails on either, delete
`CODE_SIGN_ENTITLEMENTS` from the target to run everything except push.

## Before it can ship

- **`TDWAPIBaseURL` must be the production https URL.** Nothing else in the
  app knows where the backend is.
- **Apple takes 30% of digital goods sold inside an app, and requires that
  they be sold through In-App Purchase.** This app opens Stripe Checkout,
  which App Review rejects for unlocking in-app functionality. Selling
  lifetime access on the web and having the app only *recognise* an
  entitlement bought elsewhere is the usual shape; a StoreKit product that
  grants the same entitlement server-side is the other. That is a business
  decision, so it is flagged rather than chosen — but the app cannot pass
  review as written.
- App icon, launch colour, `NSUserTrackingUsageDescription` if analytics are
  ever added, and a privacy manifest (`PrivacyInfo.xcprivacy`) declaring the
  Keychain and UserDefaults use.

## Layout of the source

```
App/          entry point, configuration, and AppState — the port of app.js
Net/          API client, models, SSE stream, Keychain
Push/         APNs registration
Audio/        the chime
Design/       the stylesheet, as Swift: colours, type, furniture, formatting
Views/        the three screens and their sections
```

`Design/Theme.swift` and `Design/Components.swift` are a direct port of
`backend/web/styles.css` — one view per CSS class, named after it, so a change
to the stylesheet has one place to land here.

The Xcode project is generated:

```bash
python3 ios/tools/generate_project.py
```

Run it after adding, removing or renaming a Swift file. A `.pbxproj` lists
every file three times, and hand-editing it is how a source file ends up
silently not compiled.

## Where it differs from the web app, and why

- **Drop caps.** `.lede::first-letter` floats; SwiftUI has no float, so the
  paragraph sits beside the capital instead of wrapping under it.
- **The stream disconnects on backgrounding.** iOS closes the socket anyway.
  Push covers that window, and the feed is refetched on return, so a dispatch
  that arrived while away still lands on the board.
- **The token lives in the Keychain**, not `localStorage`. It therefore
  survives a reinstall, which `localStorage` does not.
- **SSE authenticates with a header**, not `?token=`. `EventSource` cannot set
  headers; `URLSession` can. The backend accepts both.
