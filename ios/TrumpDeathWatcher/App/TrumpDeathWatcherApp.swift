/*  Entry point.
 *
 *  The app delegate exists for exactly two reasons — the APNs device token
 *  callback and foreground notification presentation — both of which SwiftUI
 *  still has no first-class hook for.
 */

import SwiftUI
import UIKit
import UserNotifications

@main
struct TrumpDeathWatcherApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var app = AppState()
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(app)
                .environmentObject(PushManager.shared)
                .task { await app.boot() }
                .onChange(of: scenePhase) { _, phase in
                    switch phase {
                    case .active:     app.appDidBecomeActive()
                    case .background: app.appDidEnterBackground()
                    default:          break
                    }
                }
        }
    }
}

@MainActor
final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {

    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions options: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        return true
    }

    func application(_ application: UIApplication,
                     didRegisterForRemoteNotificationsWithDeviceToken token: Data) {
        Task { @MainActor in PushManager.shared.received(apnsToken: token) }
    }

    func application(_ application: UIApplication,
                     didFailToRegisterForRemoteNotificationsWithError error: Error) {
        Task { @MainActor in PushManager.shared.failedToRegister(error) }
    }

    /// A dispatch that arrives while the app is open still shows as a banner.
    /// The web service worker raises a notification regardless of whether a tab
    /// is focused, for the same reason: the alert is the product, and silently
    /// swallowing it because the app happens to be frontmost would be the one
    /// failure this system exists to avoid.
    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                willPresent notification: UNNotification)
    async -> UNNotificationPresentationOptions {
        [.banner, .list, .sound]
    }

    /// Tapping the notification opens the source story, as notificationclick
    /// does in sw.js. An empty or "/" url just opens the app.
    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                didReceive response: UNNotificationResponse) async {
        let info = response.notification.request.content.userInfo
        guard let raw = info["url"] as? String, raw != "/", !raw.isEmpty,
              let url = URL(string: raw), url.scheme?.hasPrefix("http") == true
        else { return }
        _ = await UIApplication.shared.open(url)
    }
}
