/*  The paper — one continuous column, in the order the front page runs.
 *
 *  Section order is the order of web/index.html: vital status, what this is,
 *  the ask, the cost, market, coin, the banner, the wire, the emblem,
 *  colophon.
 */

import SwiftUI

struct PaperView: View {
    @EnvironmentObject private var app: AppState
    @EnvironmentObject private var push: PushManager

    /// The Dispatches section is commented out in web/index.html, so it is off
    /// here too. The renderer below is kept and still fed by the same state,
    /// exactly as renderDispatch() is kept in app.js — flip this to bring the
    /// wire room back, nothing else to change.
    private let showDispatches = false

    var body: some View {
        ScrollView {
            VStack(spacing: 0) {
                FlagBar()
                masthead

                VStack(alignment: .leading, spacing: 0) {
                    DatelineBar(left: Fmt.today(), right: app.watchCount)

                    VitalsBoard()

                    Kicker("What This Is")
                    Lede(text: "When it happens, seconds decide everything. Trump Death "
                         + "Watcher keeps a continuous watch on the wire services, the "
                         + "newsrooms, and the public record — and tells you the moment a "
                         + "report is corroborated, not the moment a rumour starts.")

                    // The ask comes before the justification: someone already
                    // sold on it should not have to read a cost breakdown to
                    // find the button. Both retire once there is nothing left
                    // to sell.
                    if !app.entitled {
                        LockNotice()
                        LedeCost()
                    }

                    marketSection
                    coinSection

                    if showDispatches { dispatchSection }

                    Image("Banner")
                        .resizable()
                        .scaledToFit()
                        .frame(maxWidth: .infinity)
                        .overlay(Rectangle().strokeBorder(Ink.ink, lineWidth: 1))
                        .padding(.top, 30)
                        .accessibilityLabel("Trump Death Watcher banner")

                    Kicker("From the Wire")
                    HeadLine("Related Coverage")
                    WireList()

                    Image("Emblem")
                        .resizable()
                        .scaledToFit()
                        .frame(maxWidth: 300)
                        .frame(maxWidth: .infinity)
                        .padding(.top, 30)
                        .accessibilityLabel("Trump Death Watcher emblem")

                    Stars()
                    Colophon(status: app.lastDispatchAt.map { "Last dispatch \(Fmt.time($0))" } ?? "—")
                }
                .column()

                FlagBar(dark: true)
            }
        }
        .background(Ink.paper)
    }

    // MARK: - .masthead

    private var masthead: some View {
        VStack(spacing: 0) {
            // The stylesheet's .masthead-inner is one flex row that simply
            // squeezes on a narrow screen. A row of buttons cannot squeeze —
            // it truncates — so on a phone the controls drop to a second line
            // rather than shrinking the title into nothing.
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 10) {
                    title
                    Stamp(text: app.conn.text, style: app.conn.style)
                    Spacer(minLength: 0)
                    buttons
                }
                VStack(alignment: .leading, spacing: 8) {
                    HStack(spacing: 10) {
                        title
                        Stamp(text: app.conn.text, style: app.conn.style)
                        Spacer(minLength: 0)
                    }
                    HStack(spacing: 8) {
                        buttons
                        Spacer(minLength: 0)
                    }
                }
            }
            .padding(.horizontal, Metrics.gutter)
            .padding(.vertical, 8)
            .frame(maxWidth: Metrics.column)
            .frame(maxWidth: .infinity)
            Ink.ink.frame(height: 2)
        }
        .background(Ink.paper)
    }

    private var title: some View {
        Text("Trump Death Watcher")
            .font(.condensed(22))
            .foregroundStyle(Ink.ink)
            .lineLimit(1)
            .minimumScaleFactor(0.6)
            .layoutPriority(1)
    }

    @ViewBuilder
    private var buttons: some View {
        if app.entitled {
            Button(push.status.label) {
                Task { await app.enablePush() }
            }
            .buttonStyle(.btnSmall)
            .disabled(push.status == .on || push.status == .unavailable
                      || push.status == .enabling)

            Button(app.soundOn ? "Sound On" : "Sound Off") { app.toggleSound() }
                .buttonStyle(.btnSmall)
        } else {
            Button(app.me == nil ? "Get Alerts" : "Unlock Alerts") { app.goUnlock() }
                .buttonStyle(.btnSmallRed)
        }

        if app.me == nil {
            Button("Sign In") { app.screen = .auth }.buttonStyle(.btnSmall)
        } else {
            Button("Exit") { app.signOut() }.buttonStyle(.btnSmall)
        }
    }

    // MARK: - ② market

    private var marketSection: some View {
        VStack(alignment: .leading, spacing: 0) {
            Kicker("Market")
            QuoteSection(
                symbol: "$Trump",
                last: Fmt.price(app.market?.last),
                change: app.market?.changePct,
                series: app.market?.series ?? [],
                sourceNote: marketSource,
                timeLabel: Fmt.chartDate,
                chips: {
                    ChipGroup(options: [("7D", "7"), ("30D", "30"), ("90D", "90")],
                              selection: String(app.marketDays)) { value in
                        Task { await app.loadChart(days: Int(value) ?? 7) }
                    }
                },
                footer: { EmptyView() }
            )
        }
    }

    /// Say plainly when the series is invented. A chart passing synthetic
    /// numbers off as market data would be worse than no chart.
    private var marketSource: String {
        if app.marketError { return "Quote unavailable" }
        guard let source = app.market?.source else { return "" }
        return source == "synthetic" ? "Demo data — live quote unavailable"
                                     : "Source: \(source)"
    }

    // MARK: - ③ the coin

    private var coinSection: some View {
        VStack(alignment: .leading, spacing: 0) {
            Kicker("My Memecoin")
            QuoteSection(
                symbol: "$\(app.meme?.symbol ?? "COIN")",
                last: coinHasData ? Fmt.price(app.meme?.last) : "—",
                change: coinHasData ? app.meme?.changePct : nil,
                series: coinHasData ? (app.meme?.series ?? []) : [],
                sourceNote: coinSource,
                timeLabel: Fmt.chartDateTime,
                chips: {
                    ChipGroup(options: [("1H", "1h"), ("6H", "6h"),
                                        ("24H", "24h"), ("7D", "7d")],
                              selection: app.memeWindow) { value in
                        Task { await app.loadMemecoin(window: value) }
                    }
                },
                footer: { coinMeta }
            )
        }
    }

    /// No data is a state we show honestly rather than paper over: this is a
    /// real token and an invented line could cost somebody money.
    private var coinHasData: Bool {
        guard let meme = app.meme, meme.unavailable == nil,
              let series = meme.series, !series.isEmpty else { return false }
        return true
    }

    private var coinSource: String {
        if app.memeError { return "Live price unavailable." }
        guard let meme = app.meme else { return "" }
        if !coinHasData {
            return meme.unavailable == "no trades in this window"
                ? "No trades in this window yet."
                : "Live price unavailable (\(meme.unavailable ?? "no data"))."
        }
        return meme.points == 1
            ? "Source: GeckoTerminal — one candle so far, too new to plot a line."
            : "Source: GeckoTerminal · \(meme.points ?? 0) candles"
    }

    @ViewBuilder
    private var coinMeta: some View {
        if coinHasData, let meme = app.meme {
            HStack(alignment: .top, spacing: 10) {
                Text("High \(Fmt.price(meme.high)) · Low \(Fmt.price(meme.low)) · "
                     + "Volume $\(Fmt.grouped(meme.volume ?? 0))")
                    .frame(maxWidth: .infinity, alignment: .leading)
                if let raw = meme.url, let url = URL(string: raw) {
                    Link("View on pump.fun", destination: url)
                        .foregroundStyle(Ink.red)
                        .underline()
                }
            }
            .font(.mono(10.5))
            .tracking(1.47)
            .textCase(.uppercase)
            .foregroundStyle(Ink.inkFaint)
            .padding(.top, 4)
        }
    }

    // MARK: - ④ dispatches (off, mirroring index.html)

    private var dispatchSection: some View {
        VStack(alignment: .leading, spacing: 0) {
            Kicker("Dispatches")
            HeadLine("The Wire Room")
            if app.entitled {
                HStack {
                    Spacer()
                    Button("Send Test Dispatch") { Task { await sendTestDispatch() } }
                        .buttonStyle(.btnSmall)
                }
                .padding(.bottom, 2)
            }
            if app.dispatches.isEmpty {
                EmptyNote(text: "No dispatches. That is the ordinary state of things.")
            } else {
                ForEach(app.dispatches) { event in
                    DispatchRow(event: event)
                    Hairline()
                }
            }
        }
    }

    private func sendTestDispatch() async {
        do {
            let out = try await APIClient.shared.testPush()
            push.error = out.ok
                ? "Test dispatch sent to \(out.sent ?? 0) device(s)."
                : "Nothing sent: \(out.error ?? out.errors?.joined(separator: "; ") ?? "unknown")"
        } catch {
            push.error = error.localizedDescription
        }
    }
}

/// `.sheet(item:)` needs an Identifiable; a bare URL is not one.
struct IdentifiedURL: Identifiable {
    let url: URL
    var id: String { url.absoluteString }
    init(_ url: URL) { self.url = url }
}
