/*  The vital-status board — the headline verdict.
 *
 *  One status drives the spine, the dot, the verdict colour and the dead-state
 *  inversion, exactly as `.vitals[data-status]` does in the stylesheet.
 *
 *  `retracted` deliberately returns to ALIVE: a report that collapsed is the
 *  entire reason the retraction pipeline exists, and leaving the board reading
 *  DEAD would make it a liar.
 */

import SwiftUI

enum Verdict: String {
    case alive, developing, dead

    var word: String {
        switch self {
        case .alive:      return "Alive"
        case .developing: return "Unconfirmed"
        case .dead:       return "Dead"
        }
    }

    var fallback: String {
        switch self {
        case .alive:      return "No qualifying event detected. You will be told the instant one is."
        case .developing: return "Reports are circulating and have not cleared corroboration."
        case .dead:       return "Confirmed across independent sources."
        }
    }

    var colour: Color {
        switch self {
        case .alive:      return Ink.green
        case .developing: return Ink.amber
        case .dead:       return Ink.red
        }
    }

    var wash: Color { colour.opacity(self == .alive ? 0.06 : self == .developing ? 0.09 : 0.10) }

    /// The verdict for a dispatch state. Anything unrecognised — a state this
    /// build has never heard of — reads as the quiet default rather than
    /// inventing a verdict from a string it cannot interpret.
    static func forState(_ state: String?) -> Verdict {
        switch state {
        case "confirmed": return .dead
        case "likely":    return .developing
        case "retracted": return .alive
        default:          return .alive
        }
    }

    /// The retraction copy differs from the quiet default even though both
    /// read ALIVE, so the fallback cannot come from the verdict alone.
    static func fallback(for state: String?) -> String {
        state == "retracted"
            ? "The earlier report collapsed and has been retracted."
            : forState(state).fallback
    }
}

struct VitalsBoard: View {
    @EnvironmentObject private var app: AppState
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var flashing = false

    private var verdict: Verdict { Verdict.forState(app.latest?.state) }
    private var isDead: Bool { verdict == .dead }

    private var detail: String {
        let headline = app.latest?.headline ?? ""
        return headline.isEmpty ? Verdict.fallback(for: app.latest?.state) : headline
    }

    private var corroboration: String {
        let n = app.latest?.evidence.count ?? 0
        return n > 0 ? "Corroboration — \(n) source\(n == 1 ? "" : "s")"
                     : "Awaiting corroboration"
    }

    private var since: String {
        guard let at = app.lastDispatchAt else { return "Watch established this session" }
        return "Last dispatch \(Fmt.time(at))"
    }

    var body: some View {
        VStack(spacing: 0) {
            rail
            main
            foot
        }
        .background(boardBackground)
        .overlay(alignment: .leading) {        // .vitals::before — the accent spine
            verdict.colour.frame(width: 6)
        }
        .overlay(
            Rectangle().strokeBorder(isDead ? Ink.red : Ink.ink, lineWidth: 3)
        )
        .padding(.top, 20)
        .animation(.easeInOut(duration: 0.45), value: verdict)
        .onChange(of: verdict) { _, new in
            guard new == .dead, !reduceMotion else { return }
            flashing = true
            withAnimation(.easeOut(duration: 1.1).repeatCount(3, autoreverses: false)) {
                flashing = false
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(AppConfig.subject). \(verdict.word). \(detail)")
    }

    private var boardBackground: Color {
        guard isDead else { return Ink.paper }
        return flashing ? Ink.deadFlash : Ink.ink
    }

    // .v-rail
    private var rail: some View {
        HStack(spacing: 10) {
            PulseDot(colour: verdict.colour, animated: !reduceMotion)
            Text("Vital Status · Continuous Watch")
                .frame(maxWidth: .infinity, alignment: .leading)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
            // One second of clock, without a timer of our own: TimelineView
            // redraws this label and nothing else.
            TimelineView(.periodic(from: .now, by: 1)) { context in
                Text(Fmt.clock(context.date))
                    .foregroundStyle(isDead ? Ink.deadClock : Ink.inkSoft)
                    .monospacedDigit()
            }
        }
        .font(.mono(10))
        .tracking(2.4)                                  // .24em
        .textCase(.uppercase)
        .foregroundStyle(isDead ? Ink.deadFaint : Ink.inkFaint)
        .padding(.leading, 20)
        .padding(.trailing, 12)
        .padding(.vertical, 9)
        .background(isDead ? Color.clear : verdict.wash)
        .overlay(alignment: .bottom) {
            (isDead ? Ink.deadRail : Ink.hair).frame(height: 1)
        }
    }

    // .v-main
    private var main: some View {
        VStack(spacing: 0) {
            Text(AppConfig.subject.uppercased())
                .font(.mono(10.5))
                .tracking(3.15)                         // .3em
                .foregroundStyle(isDead ? Ink.deadFaint : Ink.inkFaint)
                .multilineTextAlignment(.center)

            Text(verdict.word.uppercased())
                .font(.condensed(verdict == .developing ? 52 : 92))
                .tracking(-1.8)
                .foregroundStyle(verdict.colour)
                .lineLimit(1)
                .minimumScaleFactor(0.5)
                .padding(.top, 10)

            verdict.colour
                .frame(width: 74, height: 3)
                .padding(.top, 16)

            Text(detail)
                .font(.serif(15.5))
                .lineSpacing(15.5 * 0.5)
                .multilineTextAlignment(.center)
                .foregroundStyle(isDead ? Ink.deadBody : Ink.inkSoft)
                .padding(.top, 15)
                .frame(maxWidth: 460)
        }
        .frame(maxWidth: .infinity)
        .padding(.horizontal, 14)
        .padding(.top, 22)
        .padding(.bottom, 18)
    }

    // .v-foot — stacked under the 620px breakpoint, as in the stylesheet.
    private var foot: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(since.uppercased())
            Text(corroboration.uppercased())
        }
        .font(.mono(10))
        .tracking(1.6)
        .foregroundStyle(isDead ? Ink.deadFaint : Ink.inkFaint)
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.leading, 20)
        .padding(.trailing, 12)
        .padding(.vertical, 10)
        .overlay(alignment: .top) {
            (isDead ? Ink.deadRail : Ink.hair).frame(height: 1)
        }
    }
}

/// .v-dot and its @keyframes vpulse — a ring that expands and fades out.
struct PulseDot: View {
    let colour: Color
    var animated = true
    @State private var expanded = false

    var body: some View {
        Circle()
            .fill(colour)
            .frame(width: 9, height: 9)
            .overlay {
                Circle()
                    .stroke(colour, lineWidth: 3)
                    .scaleEffect(expanded ? 3.6 : 1)
                    .opacity(expanded ? 0 : 0.42)
            }
            .onAppear {
                guard animated else { return }
                withAnimation(.easeOut(duration: 2.4).repeatForever(autoreverses: false)) {
                    expanded = true
                }
            }
    }
}
