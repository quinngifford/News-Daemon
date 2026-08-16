/*  The remaining front-page blocks: the ask, the cost, the wire, a dispatch.  */

import SwiftUI

/// .lock — shown to anyone who has not bought delivery.
struct LockNotice: View {
    @EnvironmentObject private var app: AppState

    var body: some View {
        HStack(spacing: 0) {
            Ink.red.frame(width: 6)
            VStack(alignment: .leading, spacing: 0) {
                Text("Notifications")
                    .font(.mono(10, weight: .bold))
                    .tracking(2.8)
                    .textCase(.uppercase)
                    .foregroundStyle(Ink.red)

                Text("Get told the moment it happens".uppercased())
                    .font(.condensed(25))
                    .foregroundStyle(Ink.ink)
                    .padding(.top, 7)
                    .padding(.bottom, 8)

                Text("The status above is free to check, as often as you like. Push "
                     + "alerts and the live wire — the part that reaches you in seconds, "
                     + "wherever you are — need an account and a one-time payment.")
                    .font(.serif(15))
                    .lineSpacing(15 * 0.5)
                    .foregroundStyle(Ink.inkSoft)
                    .padding(.bottom, 15)

                Button("Unlock alerts — \(app.priceDisplay)") { app.goUnlock() }
                    .buttonStyle(.btnRed)

                if let line = app.buyerLine {
                    BuyerLine(text: line).padding(.top, 11)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(16)
        }
        .background(Ink.paper2)
        .overlay(Rectangle().strokeBorder(Ink.ink, lineWidth: 1))
        .padding(.top, 22)
    }
}

/// .buyers — a factual claim about other people's behaviour, so it is the real
/// count or it is nothing at all.
struct BuyerLine: View {
    let text: String

    var body: some View {
        Text("★ " + text.uppercased())
            .font(.mono(10.5))
            .tracking(1.89)
            .foregroundStyle(Ink.inkFaint)
    }
}

/// .lede-cost — why the thing costs money.
struct LedeCost: View {
    @EnvironmentObject private var app: AppState

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Ink.hair.frame(height: 1).padding(.bottom, 16)
            Text("Why do I charge money for this service?.".uppercased())
                .font(.mono(10.5))
                .tracking(2.31)
                .foregroundStyle(Ink.ink)
                .padding(.bottom, 6)
            Text("I pay out of pocket for early  access to insider information from "
                 + "secondary sources (Hint: Truth Social). Keeping this service running "
                 + "24/7 incurs many costs as well. A detector runs around the clock on "
                 + "its own server, holding an open streaming connection to the social "
                 + "firehose and polling the wires without pause. Paid API access is "
                 + "required, and it bills every month whether or not anything happens. "
                 + "On top of it sits the hosting, the push infrastructure, and the "
                 + "corroboration step that checks a report against independent sources "
                 + "and AI reasoning before it wakes you at four in the morning. Those "
                 + "are standing monthly costs, covered by a single payment of "
                 + "\(app.priceDisplay) — charged once, never renewed.")
                .font(.serif(15.5))
                .lineSpacing(15.5 * 0.62)
                .foregroundStyle(Ink.inkSoft)
        }
        .padding(.top, 22)
    }
}

/// .wire-item list
struct WireList: View {
    @EnvironmentObject private var app: AppState

    var body: some View {
        if let error = app.wireError {
            EmptyNote(text: error)
        } else if app.wire.isEmpty {
            EmptyNote(text: "Loading the wire…")
        } else {
            VStack(spacing: 0) {
                ForEach(Array(app.wire.enumerated()), id: \.offset) { index, item in
                    if index > 0 { Hairline() }
                    WireRow(item: item)
                }
            }
        }
    }
}

struct WireRow: View {
    let item: NewsItem

    var body: some View {
        Group {
            if let url = URL(string: item.url), url.scheme?.hasPrefix("http") == true {
                Link(destination: url) { content }
            } else {
                content
            }
        }
        .buttonStyle(.plain)
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(item.title)
                .font(.serif(16.5, bold: true))
                .lineSpacing(16.5 * 0.35)
                .foregroundStyle(Ink.ink)
                .multilineTextAlignment(.leading)
            Text((item.source?.isEmpty == false ? item.source! : "wire").uppercased())
                .font(.mono(10))
                .tracking(1.6)
                .foregroundStyle(Ink.inkFaint)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 12)
        .contentShape(Rectangle())
    }
}

/// .dispatch — one alert in the wire room. Unused while the Dispatches section
/// is commented out, exactly as renderDispatch() is on the web.
struct DispatchRow: View {
    let event: AlertEvent

    private var when: Date { Fmt.parseISO(event.receivedAt) ?? Date() }

    private var latency: String {
        guard let ms = event.detectLatencyMs else { return "—" }
        return String(format: "%.2fs", ms / 1000)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 10) {
                Text(event.state.uppercased())
                    .font(.mono(10, weight: .bold))
                    .tracking(1.6)
                    .foregroundStyle(.white)
                    .padding(.horizontal, 7)
                    .padding(.vertical, 2)
                    .background(badgeColour)
                    .strikethrough(event.state == "retracted")
                Text(Fmt.stamp(when))
                Text("· detected in \(latency)")
                if let score = event.score {
                    Text("· score \(String(format: "%.3f", score))")
                }
            }
            .font(.mono(10))
            .tracking(1.6)
            .textCase(.uppercase)
            .foregroundStyle(Ink.inkSoft)

            Text(event.headline.uppercased())
                .font(.condensed(21))
                .foregroundStyle(Ink.ink)

            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 4) {
                    Text(event.target)
                    if let raw = event.url, let url = URL(string: raw) {
                        Text("·")
                        Link("read the source", destination: url)
                            .foregroundStyle(Ink.blue)
                    }
                }
                ForEach(Array(event.evidence.prefix(3).enumerated()), id: \.offset) { _, e in
                    Text("corroboration: tier \(e.tier.map(String.init) ?? "?") — \(e.source ?? "unknown")")
                }
            }
            .font(.mono(11))
            .foregroundStyle(Ink.inkFaint)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 16)
    }

    private var badgeColour: Color {
        switch event.state {
        case "confirmed": return Ink.red
        case "likely":    return Color(hex: 0xA8700F)
        case "watch":     return Ink.blue
        default:          return Ink.inkFaint
        }
    }
}
