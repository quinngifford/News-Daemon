/*  Which of the three screens is on show.
 *
 *  The gates are destinations, not a wall you wake up behind: the paper is the
 *  landing screen, and Sign In / Subscribe are places you go and come back
 *  from. That is why every gate here has a way back to the front page.
 *
 *  Checkout and the push alert live at this level rather than inside a screen,
 *  because a purchase begun on the paywall must survive the app returning to
 *  the paper the moment entitlement lands.
 */

import SwiftUI

struct RootView: View {
    @EnvironmentObject private var app: AppState
    @EnvironmentObject private var push: PushManager

    var body: some View {
        ZStack {
            Ink.paper.ignoresSafeArea()
            switch app.screen {
            case .app:     PaperView()
            case .auth:    AuthView()
            case .paywall: PaywallView()
            }
        }
        .preferredColorScheme(.light)
        .animation(.default, value: app.screen)
        .sheet(item: Binding(
            get: { app.checkoutURL.map(IdentifiedURL.init) },
            set: { if $0 == nil { Task { await app.checkoutDismissed() } } }
        )) { item in
            CheckoutView(url: item.url).ignoresSafeArea()
        }
        .alert("Alerts", isPresented: Binding(
            get: { push.error != nil },
            set: { if !$0 { push.error = nil } }
        )) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(push.error ?? "")
        }
    }
}
