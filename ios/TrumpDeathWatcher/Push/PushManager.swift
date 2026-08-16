/*  APNs registration and presentation.
 *
 *  The web client's initPush() registers a Web Push subscription with
 *  /api/push/register as kind "webpush". This does the same with kind "apns",
 *  which is exactly the split the backend was built for — see "Native mobile
 *  app" in backend/README.md and SENDERS in app/push.py.
 *
 *  The endpoint requires an entitlement, not merely a session: registering a
 *  device IS subscribing to notifications, so that is where the paywall is
 *  actually enforced. Hiding the button would enforce nothing.
 */

import Combine
import Foundation
import UIKit
import UserNotifications

@MainActor
final class PushManager: NSObject, ObservableObject {
    static let shared = PushManager()

    enum Status: Equatable {
        case unavailable        // simulator, or the user hard-denied
        case idle               // "Alerts"
        case enabling           // "Enabling…"
        case on                 // "Alerts On"

        var label: String {
            switch self {
            case .unavailable: return "No Push"
            case .idle:        return "Alerts"
            case .enabling:    return "Enabling…"
            case .on:          return "Alerts On"
            }
        }
    }

    @Published private(set) var status: Status = .idle
    /// Surfaced by the view as an alert, mirroring the web client's alert().
    @Published var error: String?

    /// Set by the app delegate when APNs hands back a token.
    private var pendingToken: CheckedContinuation<String, Error>?
    private var deviceToken: String? {
        get { UserDefaults.standard.string(forKey: "apns_device_token") }
        set { UserDefaults.standard.set(newValue, forKey: "apns_device_token") }
    }

    private override init() { super.init() }

    /// The equivalent of `getSubscription()` on boot: if this device is already
    /// registered and permission still stands, the button says so and does
    /// nothing.
    func refresh() async {
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        switch settings.authorizationStatus {
        case .denied:
            status = .unavailable
        case .authorized, .provisional, .ephemeral:
            status = deviceToken != nil ? .on : .idle
            // Permission survives a reinstall; the registration on the server
            // may not. Re-register quietly so a restored device keeps working.
            if deviceToken != nil { UIApplication.shared.registerForRemoteNotifications() }
        default:
            status = .idle
        }
    }

    func enable() async {
        guard status != .on else { return }
        status = .enabling
        error = nil
        do {
            let granted = try await UNUserNotificationCenter.current()
                .requestAuthorization(options: [.alert, .sound, .badge])
            guard granted else { throw APIError.message("Notification permission denied.") }

            let token = try await registerWithAPNs()
            let result = try await APIClient.shared.registerDevice(
                token: token, userAgent: AppConfig.userAgent)
            guard result.ok else {
                throw APIError.message(result.error ?? "Could not register this device.")
            }
            deviceToken = token
            status = .on
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription
                ?? error.localizedDescription
            status = .idle
        }
    }

    /// Forget this device on sign-out, so the next person to sign in on this
    /// phone does not inherit someone else's alerts.
    func signOut() async {
        if let token = deviceToken {
            await APIClient.shared.unregisterDevice(token: token)
        }
        deviceToken = nil
        status = .idle
    }

    private func registerWithAPNs() async throws -> String {
        if let existing = deviceToken { return existing }
        return try await withCheckedThrowingContinuation { continuation in
            pendingToken = continuation
            UIApplication.shared.registerForRemoteNotifications()
        }
    }

    // Called from the app delegate.

    func received(apnsToken data: Data) {
        let hex = data.map { String(format: "%02x", $0) }.joined()
        pendingToken?.resume(returning: hex)
        pendingToken = nil
        // A silent re-register (the refresh() path) has no continuation
        // waiting; keep the server's copy current anyway, because APNs rotates
        // tokens and a stale one is a dropped alert.
        if deviceToken != nil, deviceToken != hex {
            self.deviceToken = hex
            Task {
                _ = try? await APIClient.shared.registerDevice(
                    token: hex, userAgent: AppConfig.userAgent)
            }
        }
    }

    func failedToRegister(_ error: Error) {
        pendingToken?.resume(throwing: error)
        pendingToken = nil
        // The simulator has no APNs. Say that plainly rather than leaving a
        // button that spins forever.
        #if targetEnvironment(simulator)
        status = .unavailable
        self.error = "Push notifications need a real device — the simulator has no APNs."
        #else
        status = .idle
        self.error = error.localizedDescription
        #endif
    }
}
