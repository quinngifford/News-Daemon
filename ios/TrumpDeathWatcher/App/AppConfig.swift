/*  Where the backend is.
 *
 *  Read from Info.plist (TDWAPIBaseURL) rather than hardcoded, so the same
 *  build configuration points at a laptop, a staging box, or production by
 *  changing one string — and so the value is visible in the built app rather
 *  than buried in Swift.
 *
 *  Ship-blocking: the default below is the development server. Set the real
 *  https URL in Info.plist before archiving, or the app will talk to nothing.
 */

import Foundation
import UIKit

enum AppConfig {

    static let apiBaseURL: URL = {
        let raw = (Bundle.main.object(forInfoDictionaryKey: "TDWAPIBaseURL") as? String)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        // A trailing slash makes every `URL(string:relativeTo:)` below resolve
        // one path component short, so it is stripped once, here.
        let cleaned = raw.hasSuffix("/") ? String(raw.dropLast()) : raw
        return URL(string: cleaned) ?? URL(string: "http://127.0.0.1:8000")!
    }()

    /// Sent with the device registration so a support request can be matched to
    /// a build, the same way the web client sends navigator.userAgent.
    @MainActor static var userAgent: String {
        let v = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "?"
        let build = Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "?"
        let device = UIDevice.current
        return "TrumpDeathWatcher/\(v) (\(build); \(device.systemName) \(device.systemVersion))"
    }

    /// The one figure in the app that is not fetched: the subject of the watch.
    static let subject = "Donald J. Trump"
}
