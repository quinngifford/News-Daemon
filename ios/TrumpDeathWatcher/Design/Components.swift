/*  The furniture: flag bars, rules, kickers, stamps, buttons, fields.
 *
 *  One view per CSS class, named after it, so a change to the stylesheet has
 *  exactly one place to land here.
 */

import SwiftUI

// MARK: - .flagbar

struct FlagBar: View {
    var dark = false

    var body: some View {
        HStack(spacing: 0) {
            ForEach(0..<7, id: \.self) { i in
                (i % 2 == 0 ? Ink.red : (dark ? Ink.blue : Ink.paper2))
                    .frame(maxWidth: .infinity)
            }
        }
        .frame(height: 6)
    }
}

// MARK: - rules

/// .rule-double — 3px over 1px, 4px apart.
struct RuleDouble: View {
    var body: some View {
        VStack(spacing: 4) {
            Ink.ink.frame(height: 3)
            Ink.ink.frame(height: 1)
        }
        .padding(.vertical, 14)
    }
}

/// .rule-thin
struct RuleThin: View {
    var body: some View {
        Ink.hair.frame(height: 1).padding(.vertical, 22)
    }
}

/// A bare hairline, for list separators.
struct Hairline: View {
    var color: Color = Ink.hair
    var body: some View { color.frame(height: 1) }
}

// MARK: - .dateline-bar

struct DatelineBar: View {
    let left: String
    let right: String
    var topBorder = false

    var body: some View {
        VStack(spacing: 0) {
            if topBorder { Ink.ink.frame(height: 1) }
            HStack(spacing: 12) {
                Text(left.uppercased())
                Spacer(minLength: 8)
                Text(right.uppercased()).multilineTextAlignment(.trailing)
            }
            .font(.mono(10.5))
            .tracking(1.26)                    // .12em
            .foregroundStyle(Ink.inkSoft)
            .padding(.vertical, 7)
            Ink.ink.frame(height: 1)
        }
    }
}

// MARK: - .kicker

struct Kicker: View {
    let text: String
    init(_ text: String) { self.text = text }

    var body: some View {
        HStack(spacing: 12) {
            Text(text.uppercased())
                .font(.mono(10.5, weight: .bold))
                .tracking(2.94)                // .28em
                .foregroundStyle(Ink.red)
            Ink.ink.frame(height: 1)
        }
        .padding(.top, 34)
        .padding(.bottom, 10)
    }
}

/// h2.head
struct HeadLine: View {
    let text: String
    init(_ text: String) { self.text = text }

    var body: some View {
        Text(text.uppercased())
            .font(.condensed(34))
            .tracking(-0.34)
            .foregroundStyle(Ink.ink)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.bottom, 6)
    }
}

/// .stars
struct Stars: View {
    var body: some View {
        Text("★ ★ ★")
            .font(.system(size: 12))
            .tracking(8.4)                     // .7em
            .foregroundStyle(Ink.red)
            .frame(maxWidth: .infinity)
            .padding(.top, 16)
            .padding(.bottom, 4)
    }
}

// MARK: - .stamp

enum StampStyle { case plain, live, warn }

struct Stamp: View {
    let text: String
    var style: StampStyle = .plain

    private var colour: Color {
        switch style {
        case .plain: return Ink.inkSoft
        case .live:  return Ink.blue
        case .warn:  return Ink.red
        }
    }

    private var border: Color {
        switch style {
        case .plain: return Ink.inkFaint
        case .live:  return Ink.blue
        case .warn:  return Ink.red
        }
    }

    var body: some View {
        Text(text.uppercased())
            .font(.mono(9.5))
            .tracking(1.52)                    // .16em
            .foregroundStyle(colour)
            .padding(.horizontal, 7)
            .padding(.vertical, 3)
            .overlay(Rectangle().strokeBorder(border, lineWidth: 1))
            .fixedSize()
    }
}

// MARK: - .btn

struct BroadsheetButtonStyle: ButtonStyle {
    enum Kind { case normal, red }
    var kind: Kind = .normal
    var small = false                          // .btn-sm
    var block = false                          // .btn-block

    /// `@Environment` read directly in a ButtonStyle never updates — a style is
    /// not a View — so `.btn:disabled { opacity: .45 }` needs this inner view
    /// to observe isEnabled for real.
    func makeBody(configuration: Configuration) -> some View {
        Chrome(configuration: configuration, kind: kind, small: small, block: block)
    }

    private struct Chrome: View {
        let configuration: Configuration
        let kind: Kind
        let small: Bool
        let block: Bool
        @Environment(\.isEnabled) private var isEnabled

        private var pressed: Bool { configuration.isPressed && isEnabled }

        private var fill: Color {
            switch kind {
            case .red:    return pressed ? Ink.redDark : Ink.red
            case .normal: return pressed ? Ink.ink : Ink.paper
            }
        }

        private var ink: Color {
            switch kind {
            case .red:    return .white
            case .normal: return pressed ? Ink.paper : Ink.ink
            }
        }

        private var edge: Color {
            kind == .red ? (pressed ? Ink.redDark : Ink.red) : Ink.ink
        }

        var body: some View {
            configuration.label
                .font(.condensed(small ? 14 : 19))
                .tracking(small ? 0.84 : 1.14)     // .06em
                .textCase(.uppercase)
                .lineLimit(1)
                .foregroundStyle(ink)
                .padding(.horizontal, small ? 12 : 22)
                .padding(.vertical, small ? 7 : 13)
                .frame(maxWidth: block ? .infinity : nil)
                .background(fill)
                .overlay(Rectangle().strokeBorder(edge, lineWidth: small ? 1 : 2))
                .opacity(isEnabled ? 1 : 0.45)
                .contentShape(Rectangle())
        }
    }
}

extension ButtonStyle where Self == BroadsheetButtonStyle {
    static var btn: BroadsheetButtonStyle { .init() }
    static var btnRed: BroadsheetButtonStyle { .init(kind: .red) }
    static var btnSmall: BroadsheetButtonStyle { .init(small: true) }
    static var btnSmallRed: BroadsheetButtonStyle { .init(kind: .red, small: true) }
    /// .btn-block
    static var btnBlock: BroadsheetButtonStyle { .init(block: true) }
    static var btnRedBlock: BroadsheetButtonStyle { .init(kind: .red, block: true) }
}

/// .linkish — the quiet way back.
struct Linkish: View {
    let title: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(title.uppercased())
                .font(.mono(11))
                .tracking(1.32)
                .foregroundStyle(Ink.inkSoft)
                .underline()
        }
        .buttonStyle(.plain)
        .padding(.top, 18)
    }
}

// MARK: - .notice

struct Notice: View {
    let text: String

    var body: some View {
        HStack(spacing: 0) {
            Ink.red.frame(width: 4)
            Text(text)
                .font(.serif(14.5))
                .foregroundStyle(Ink.ink)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 13)
                .padding(.vertical, 10)
        }
        .background(Ink.red.opacity(0.07))
        .fixedSize(horizontal: false, vertical: true)
    }
}

// MARK: - .field

struct Field<Content: View>: View {
    let label: String
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(label.uppercased())
                .font(.mono(10.5))
                .tracking(1.89)                // .18em
                .foregroundStyle(Ink.inkSoft)
            content
                .font(.serif(17))
                .foregroundStyle(Ink.ink)
                .padding(.horizontal, 12)
                .padding(.vertical, 11)
                .background(Ink.field)
                .overlay(Rectangle().strokeBorder(Ink.ink, lineWidth: 2))
        }
        .padding(.bottom, 15)
    }
}

// MARK: - .lede, with the drop cap

/// `.lede::first-letter` floats a red condensed capital beside the paragraph.
/// SwiftUI has no float, so the remaining copy sits alongside the cap rather
/// than wrapping under it — the one place this port approximates the CSS.
struct Lede: View {
    let text: String

    var body: some View {
        let first = String(text.prefix(1))
        let rest = String(text.dropFirst())

        HStack(alignment: .top, spacing: 0) {
            Text(first)
                .font(.condensed(62))
                .foregroundStyle(Ink.red)
                .padding(.trailing, 8)
                .alignmentGuide(.top) { $0[.top] + 4 }
            Text(rest)
                .font(.serif(19))
                .foregroundStyle(Ink.ink)
                .lineSpacing(19 * 0.5)         // line-height 1.5
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

// MARK: - .colophon

struct Colophon: View {
    let status: String

    var body: some View {
        VStack(spacing: 0) {
            VStack(spacing: 4) {
                Ink.ink.frame(height: 3)
                Ink.ink.frame(height: 1)
            }
            VStack(spacing: 10) {
                Text("Trump Death Watcher · Published Continuously")
                Text(status.uppercased())
                Text("Dispatches are informational only and are not financial advice.")
            }
            .font(.mono(10.5))
            .tracking(1.47)                    // .14em
            .textCase(.uppercase)
            .multilineTextAlignment(.center)
            .foregroundStyle(Ink.inkFaint)
            .padding(.top, 18)
            .padding(.bottom, 44)
        }
        .padding(.top, 40)
    }
}

// MARK: - .empty

struct EmptyNote: View {
    let text: String

    var body: some View {
        Text(text)
            .font(.serif(15))
            .italic()
            .foregroundStyle(Ink.inkFaint)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 26)
    }
}
