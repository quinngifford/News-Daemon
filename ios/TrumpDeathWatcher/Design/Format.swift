/*  Formatting — ports fmtPrice / fmtPct and the date strings from app.js.
 *
 *  These are deliberately literal translations. A price rendered differently
 *  here than on the web would be a second source of truth for the same number.
 */

import Foundation

enum Fmt {

    private static let subscripts = Array("₀₁₂₃₄₅₆₇₈₉")

    /// A memecoin trades around 0.0000003. "%.4f" renders that as "0.0000",
    /// and exponent notation reads like a bug in a price field. 0.0₆295 is the
    /// notation every Solana chart uses, and it stays honest at both ends of
    /// the scale — so one formatter serves both the $8 token and the sub-cent
    /// one.
    static func price(_ value: Double?) -> String {
        guard let n = value, n.isFinite, n > 0 else { return "—" }
        if n >= 1     { return String(format: "$%.2f", n) }
        if n >= 0.01  { return String(format: "$%.4f", n) }
        if n >= 1e-4  { return String(format: "$%.6f", n) }

        let exp = Int(floor(log10(n)))              // -7 for 2.95e-7
        let zeros = -exp - 1                        // zeros after "0."
        let scaled = (n * pow(10, Double(-exp + 3))).rounded()
        let digits = String(String(Int(scaled)).prefix(4))
        let marker = String(String(zeros).compactMap { ch -> Character? in
            guard let d = ch.wholeNumberValue, (0...9).contains(d) else { return nil }
            return subscripts[d]
        })
        return "$0.0\(marker)\(digits)"
    }

    static func pct(_ value: Double?) -> String {
        let n = value ?? 0
        return String(format: "%@ %.2f%%", n >= 0 ? "▲" : "▼", abs(n))
    }

    /// JS `Number.toLocaleString()` — grouped, no decimals.
    static func grouped(_ value: Double) -> String {
        let f = NumberFormatter()
        f.numberStyle = .decimal
        f.maximumFractionDigits = 0
        return f.string(from: NSNumber(value: value.rounded())) ?? String(Int(value))
    }

    static func grouped(_ value: Int) -> String { grouped(Double(value)) }

    // --- dates -------------------------------------------------------------
    // `toLocaleDateString(undefined, {weekday, year, month, day})` uppercased.
    private static let longDate: DateFormatter = {
        let f = DateFormatter()
        f.dateStyle = .full
        f.timeStyle = .none
        return f
    }()

    /// `toLocaleTimeString()`
    private static let timeOfDay: DateFormatter = {
        let f = DateFormatter()
        f.dateStyle = .none
        f.timeStyle = .medium
        return f
    }()

    /// `toLocaleTimeString([], { hour12: false })` — the vital-status clock.
    private static let clock24: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_GB")   // forces 24-hour regardless of region
        f.dateFormat = "HH:mm:ss"
        return f
    }()

    /// `toLocaleString()` — dispatch timestamps.
    private static let stampFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateStyle = .short
        f.timeStyle = .medium
        return f
    }()

    /// Chart tooltip on the market series: `toLocaleDateString()`.
    private static let shortDate: DateFormatter = {
        let f = DateFormatter()
        f.dateStyle = .short
        f.timeStyle = .none
        return f
    }()

    /// Chart tooltip on the coin series:
    /// `{month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'}`.
    private static let dayAndTime: DateFormatter = {
        let f = DateFormatter()
        f.setLocalizedDateFormatFromTemplate("MMM d, jj:mm")
        return f
    }()

    static func today() -> String { longDate.string(from: Date()).uppercased() }
    static func time(_ date: Date) -> String { timeOfDay.string(from: date) }
    static func clock(_ date: Date) -> String { clock24.string(from: date) }
    static func stamp(_ date: Date) -> String { stampFormatter.string(from: date) }
    static func chartDate(_ ms: Double) -> String {
        shortDate.string(from: Date(timeIntervalSince1970: ms / 1000))
    }
    static func chartDateTime(_ ms: Double) -> String {
        dayAndTime.string(from: Date(timeIntervalSince1970: ms / 1000))
    }

    /// ISO-8601 out of the backend, with or without fractional seconds.
    static func parseISO(_ value: String?) -> Date? {
        guard let value else { return nil }
        let withFraction = ISO8601DateFormatter()
        withFraction.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = withFraction.date(from: value) { return d }
        let plain = ISO8601DateFormatter()
        plain.formatOptions = [.withInternetDateTime]
        if let d = plain.date(from: value) { return d }
        // The backend emits `datetime.isoformat()`, which omits the timezone
        // when the column is naive. Treat that as UTC rather than dropping it.
        let naive = DateFormatter()
        naive.locale = Locale(identifier: "en_US_POSIX")
        naive.timeZone = TimeZone(identifier: "UTC")
        for format in ["yyyy-MM-dd'T'HH:mm:ss.SSSSSS", "yyyy-MM-dd'T'HH:mm:ss"] {
            naive.dateFormat = format
            if let d = naive.date(from: value) { return d }
        }
        return nil
    }
}
