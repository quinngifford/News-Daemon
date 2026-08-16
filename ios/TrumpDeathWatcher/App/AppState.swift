/*  The client, ported from web/app.js.
 *
 *  Three states driven by one fact: signed in? paid? → gate, subscribe, or
 *  paper. Signed out is a valid way to read this paper — the vital-status
 *  board and both charts are public. Only *delivery* is bought: the live
 *  stream, push, and the chime.
 *
 *  Everything entitlement-dependent is decided in one place (`entitled`), the
 *  way applyAccess() does on the web, so the chrome can never disagree with
 *  the token we are actually holding. The server enforces all of it regardless
 *  — this only decides what is worth offering.
 */

import Combine
import Foundation
import SwiftUI

@MainActor
final class AppState: ObservableObject {

    enum Screen { case auth, paywall, app }

    enum Conn {
        case connecting, live, warn, publicView

        var text: String {
            switch self {
            case .connecting: return "connecting"
            case .live:       return "on watch"
            case .warn:       return "reconnecting"
            case .publicView: return "public view"
            }
        }

        var style: StampStyle {
            switch self {
            case .live: return .live
            case .warn: return .warn
            default:    return .plain
            }
        }
    }

    // MARK: - published state

    @Published var screen: Screen = .app
    @Published var me: Me?
    @Published var billing: BillingConfig?
    @Published var conn: Conn = .connecting

    @Published var latest: AlertEvent?
    @Published var dispatches: [AlertEvent] = []
    @Published var lastDispatchAt: Date?
    @Published var watchCount = "—"

    @Published var wire: [NewsItem] = []
    @Published var wireError: String?

    @Published var market: MarketResponse?
    @Published var marketDays = 7
    @Published var marketError = false

    @Published var meme: MemecoinResponse?
    @Published var memeWindow = "24h"
    @Published var memeError = false

    @Published var soundOn = false

    @Published var authMode = "login"
    @Published var authError: String?
    @Published var authBusy = false

    @Published var buyError: String?
    @Published var buyBusy = false
    @Published var checkoutURL: URL?

    var entitled: Bool { me?.entitled == true }
    var priceDisplay: String { billing?.priceDisplay ?? "$49.99" }
    var devGrantAvailable: Bool { billing?.configured == false }

    /// "N people have unlocked alerts". `nil` from the server means "not enough
    /// sales to be worth saying" — the threshold is the server's policy, so the
    /// client has none of its own to drift out of step with.
    var buyerLine: String? {
        guard let sold = billing?.purchaseCount, sold > 0 else { return nil }
        return "\(Fmt.grouped(sold)) people have unlocked alerts"
    }

    // MARK: - private

    private let api = APIClient.shared
    private let stream = EventStream()
    private var seen = Set<String>()
    private var wireTimer: Task<Void, Never>?
    private var booted = false

    init() {
        stream.onState = { [weak self] state in
            guard let self, self.entitled else { return }
            switch state {
            case .connecting:   self.conn = .connecting
            case .open:         self.conn = .live
            case .reconnecting: self.conn = .warn
            }
        }
        stream.onEvent = { [weak self] event in
            self?.handle(event)
        }
    }

    // MARK: - boot

    func boot() async {
        guard !booted else { return }
        booted = true

        async let config: Void = loadBillingConfig()
        if TokenStore.token != nil {
            // An expired or revoked token is not an error state: drop it and
            // browse on as a visitor, exactly as the web client does.
            if let user = await api.me() { me = user } else { TokenStore.token = nil }
        }
        await config
        await enterApp()
    }

    func loadBillingConfig() async {
        // Cosmetic only — never block boot. A 401 or 500 still parses as JSON,
        // and treating that body as config is what once wrongly revealed the
        // dev-grant button and showed an undefined price, so getOptional()
        // checks the status before decoding.
        if let cfg = await api.billingConfig() { billing = cfg }
    }

    /// The web client's enterApp(). Called on boot, and again after any
    /// sign-in, sign-out or purchase.
    func enterApp() async {
        screen = .app
        if !entitled { conn = .publicView }

        // Four independent fetches, in parallel: the board must not wait on
        // the coin chart, and the wire must not wait on either.
        let days = marketDays, window = memeWindow
        async let alerts: Void = loadAlerts()
        async let news: Void = loadWire()
        async let chart: Void = loadChart(days: days)
        async let coin: Void = loadMemecoin(window: window)
        await alerts
        await news
        await chart
        await coin

        // Both of these are the paid product. Calling them while signed out
        // would just collect 401s for a visitor who is doing nothing wrong.
        if entitled {
            conn = .connecting
            stream.connect()
            await PushManager.shared.refresh()
        }
        startWireRefresh()
    }

    private func startWireRefresh() {
        wireTimer?.cancel()
        wireTimer = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 300 * 1_000_000_000)   // 5 min
                if Task.isCancelled { return }
                await self?.loadWire()
            }
        }
    }

    /// iOS keeps no sockets open for a backgrounded app. Push is the channel
    /// while we are away; the stream picks up — and the feed is refetched, so
    /// anything missed in between still lands — on return.
    func appDidBecomeActive() {
        guard entitled else { return }
        stream.connect()
        Task { await loadAlerts() }
    }

    func appDidEnterBackground() {
        stream.disconnect()
    }

    // MARK: - the feed

    func loadAlerts() async {
        guard let page = try? await api.events(limit: 40) else { return }
        let n = page.events.count
        watchCount = "\(n) dispatch\(n == 1 ? "" : "es") on file"
        guard n > 0 else { return }
        // Oldest first into the list, newest first on the board — the same
        // order the web client renders them in.
        for event in page.events.reversed() { seen.insert(event.dedupeKey) }
        dispatches = page.events
        updateVitals(page.events[0])
    }

    private func handle(_ event: AlertEvent) {
        guard !seen.contains(event.dedupeKey) else { return }
        seen.insert(event.dedupeKey)
        dispatches.insert(event, at: 0)
        if dispatches.count > 60 { dispatches.removeLast(dispatches.count - 60) }
        updateVitals(event)

        if event.state == "confirmed" { chime(5) }
        else if event.state == "likely" { chime(2) }
    }

    private func updateVitals(_ event: AlertEvent) {
        latest = event
        lastDispatchAt = Date()
    }

    // MARK: - content

    func loadWire() async {
        do {
            wire = try await api.news(limit: 14).items
            wireError = nil
        } catch {
            wire = []
            wireError = "The wire is unreachable."
        }
    }

    func loadChart(days: Int) async {
        marketDays = days
        do {
            market = try await api.market(days: days)
            marketError = false
        } catch {
            marketError = true
        }
    }

    func loadMemecoin(window: String) async {
        memeWindow = window
        do {
            meme = try await api.memecoin(window: window)
            memeError = false
        } catch {
            meme = nil
            memeError = true
        }
    }

    // MARK: - sound

    func toggleSound() {
        soundOn.toggle()
        if soundOn { Chime.shared.play(times: 1) }   // warm the audio stack
    }

    private func chime(_ times: Int) {
        guard soundOn else { return }
        Chime.shared.play(times: times)
    }

    // MARK: - auth

    func submitAuth(email: String, password: String) async {
        authBusy = true
        authError = nil
        defer { authBusy = false }
        do {
            let out = try await api.auth(mode: authMode, email: email, password: password)
            TokenStore.token = out.accessToken
            me = await api.me()
            // Refresh even when we land on the paywall — otherwise going back
            // to the paper would still show the signed-out buttons.
            if entitled { await enterApp() } else { screen = .paywall }
        } catch {
            authError = (error as? LocalizedError)?.errorDescription
                ?? error.localizedDescription
        }
    }

    func signOut() {
        let manager = PushManager.shared
        Task { await manager.signOut() }
        TokenStore.token = nil
        stream.disconnect()
        me = nil
        seen.removeAll()
        dispatches = []
        latest = nil
        lastDispatchAt = nil
        conn = .publicView
        screen = .app
        Task { await loadAlerts() }
    }

    func goUnlock() { screen = me == nil ? .auth : .paywall }

    // MARK: - purchase

    func startCheckout() async {
        buyBusy = true
        buyError = nil
        do {
            let out = try await api.checkout()
            if out.alreadyEntitled {
                me = await api.me()
                await enterApp()
                buyBusy = false
                return
            }
            guard let raw = out.url, let url = URL(string: raw) else {
                throw APIError.message("Checkout unavailable")
            }
            pendingSessionId = out.id
            checkoutURL = url
        } catch {
            buyError = (error as? LocalizedError)?.errorDescription
                ?? error.localizedDescription
            buyBusy = false
        }
    }

    private var pendingSessionId: String?

    /// Returning from Stripe. Entitlement is normally granted by the webhook,
    /// but we also ask the server to verify the session directly with Stripe —
    /// so a webhook that is misconfigured or briefly down cannot leave you
    /// having paid with nothing to show for it. Nothing here trusts the client:
    /// the server checks with Stripe, and refuses a session belonging to
    /// another account.
    func checkoutDismissed() async {
        checkoutURL = nil
        defer { buyBusy = false }

        if let session = pendingSessionId {
            pendingSessionId = nil
            if let out = try? await api.confirm(sessionId: session), out.entitled,
               let user = await api.me(), user.entitled {
                me = user
                await enterApp()
                return
            }
        }

        // Otherwise wait for the webhook to land.
        for _ in 0..<12 {
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            if let user = await api.me(), user.entitled {
                me = user
                await enterApp()
                return
            }
        }
        if me?.entitled != true {
            buyError = "Payment received, but access has not activated yet. "
                + "Reopen in a moment, or contact support with your receipt."
        }
    }

    func devGrant() async {
        guard let user = try? await api.devGrant() else { return }
        me = user
        await enterApp()
    }

    // MARK: - push

    func enablePush() async {
        await PushManager.shared.enable()
    }
}
