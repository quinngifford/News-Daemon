/*  Theme — the broadsheet, ported from web/styles.css.
 *
 *  Same palette, same type scale, same hard hairline rules and square corners.
 *  Old Glory Red and Old Glory Blue are taken from the flag specification, as
 *  in the stylesheet; nothing here is a fresh design decision.
 *
 *  The app is pinned to light appearance in Info.plist (UIUserInterfaceStyle),
 *  because the stylesheet has no dark mode either — newsprint is newsprint.
 */

import SwiftUI

extension Color {
    init(hex: UInt32) {
        self.init(
            .sRGB,
            red:   Double((hex >> 16) & 0xFF) / 255,
            green: Double((hex >> 8) & 0xFF) / 255,
            blue:  Double(hex & 0xFF) / 255,
            opacity: 1
        )
    }
}

enum Ink {
    static let paper     = Color(hex: 0xF5F1E8)   // --paper
    static let paper2    = Color(hex: 0xEBE5D8)   // --paper-2
    static let ink       = Color(hex: 0x16130F)   // --ink
    static let inkSoft   = Color(hex: 0x4A443C)   // --ink-soft
    static let inkFaint  = Color(hex: 0x877E70)   // --ink-faint
    static let red       = Color(hex: 0xB22234)   // --red
    static let redDark   = Color(hex: 0x8D1A29)   // .btn-red:hover
    static let blue      = Color(hex: 0x3C3B6E)   // --blue
    static let hair      = Color(hex: 0xC9C0AD)   // --hair
    static let green     = Color(hex: 0x1F6F43)   // --green (verdict)
    static let amber     = Color(hex: 0xA66E10)   // --amber (verdict)
    static let up        = Color(hex: 0x17632F)   // .q-chg.up / chart up
    static let field     = Color(hex: 0xFFFDF7)   // .field input background

    /// Inverted board, .vitals[data-status="dead"]
    static let deadRail  = Color(hex: 0x3A332B)
    static let deadFaint = Color(hex: 0x9C9282)
    static let deadClock = Color(hex: 0xC8BFAE)
    static let deadBody  = Color(hex: 0xE6DFD2)
    static let deadFlash = Color(hex: 0x45111A)
}

extension Font {
    /// --serif. Iowan Old Style ships with iOS, which is why the stylesheet
    /// names it first; Georgia is the fallback there and here.
    static func serif(_ size: CGFloat, bold: Bool = false) -> Font {
        .custom(bold ? "IowanOldStyle-Bold" : "IowanOldStyle-Roman", size: size)
    }

    /// --condensed. No Haettenschweiler on iOS, so this is the system face at
    /// its narrowest width — the same job: heavy, tight, all caps.
    static func condensed(_ size: CGFloat) -> Font {
        .system(size: size, weight: .black).width(.compressed)
    }

    /// --mono
    static func mono(_ size: CGFloat, weight: Font.Weight = .regular) -> Font {
        .system(size: size, weight: weight, design: .monospaced)
    }
}

enum Metrics {
    /// --col: 720px, minus the .wrap padding. Phones never reach it; iPads do,
    /// and without the cap the column would run the full width of the screen.
    static let column: CGFloat = 680
    /// .wrap padding — 20px, 14px under the 620px breakpoint. Every phone is
    /// under it, so the narrow value is the one that applies.
    static let gutter: CGFloat = 14
}

/// Body copy — the `body` rule, at the ≤620px size.
struct BodyText: ViewModifier {
    func body(content: Content) -> some View {
        content
            .font(.serif(16))
            .foregroundStyle(Ink.ink)
            .lineSpacing(16 * 0.58)      // line-height 1.58
    }
}

extension View {
    func bodyText() -> some View { modifier(BodyText()) }

    /// The column itself: capped width, centred, gutters either side.
    func column() -> some View {
        frame(maxWidth: Metrics.column)
            .padding(.horizontal, Metrics.gutter)
            .frame(maxWidth: .infinity)
    }
}
