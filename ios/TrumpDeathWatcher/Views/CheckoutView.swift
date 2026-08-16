/*  Stripe Checkout, in a Safari view controller.
 *
 *  SFSafariViewController rather than a WKWebView: Stripe Checkout expects a
 *  real browser (Apple Pay, saved cards, 3-D Secure redirects to a bank), and
 *  Apple rejects payment flows driven through an embedded web view. It also
 *  means card details never pass through this app's process.
 *
 *  Dismissal is the only signal we get back — the success URL is our own site,
 *  not a custom scheme — and dismissal proves nothing about payment. What
 *  happens next is in AppState.checkoutDismissed(): ask the server to verify
 *  the session with Stripe, then wait for the webhook.
 */

import SafariServices
import SwiftUI

struct CheckoutView: UIViewControllerRepresentable {
    let url: URL

    func makeUIViewController(context: Context) -> SFSafariViewController {
        let config = SFSafariViewController.Configuration()
        config.entersReaderIfAvailable = false
        let controller = SFSafariViewController(url: url, configuration: config)
        controller.preferredBarTintColor = UIColor(Ink.paper)
        controller.preferredControlTintColor = UIColor(Ink.red)
        controller.dismissButtonStyle = .close
        return controller
    }

    func updateUIViewController(_ controller: SFSafariViewController, context: Context) {}
}
