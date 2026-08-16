/*  The subscription gate.
 *
 *  Stripe Checkout opens in a Safari view controller and the app asks the
 *  server to verify the session afterwards. Nothing here decides entitlement:
 *  the browser returning "success" is trivially forged, so the server checks
 *  with Stripe and the webhook remains the authoritative grant.
 */

import SwiftUI

struct PaywallView: View {
    @EnvironmentObject private var app: AppState

    private let benefits = [
        "Instant dispatch the moment an event is confirmed",
        "Corroborated across independent sources before you are told",
        "Automatic retraction if a report collapses",
        "The full evidence trail behind every dispatch",
        "Live market quote and the related wire",
    ]

    var body: some View {
        VStack(spacing: 0) {
            FlagBar()
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    nameplate
                    RuleDouble()
                    pricebox
                    if let line = app.buyerLine {
                        BuyerLine(text: line).padding(.bottom, 10)
                    }
                    benefitList

                    Button(app.buyBusy ? "Opening Checkout…" : "Purchase Access") {
                        Task { await app.startCheckout() }
                    }
                    .buttonStyle(.btnRedBlock)
                    .disabled(app.buyBusy)
                    .padding(.top, 18)

                    if let error = app.buyError {
                        Notice(text: error).padding(.top, 14)
                    }

                    // Lets you use the product before Stripe exists. The server
                    // refuses this outright when env=prod, so a build pointed
                    // at production simply never sees the button.
                    if app.devGrantAvailable {
                        Button("Dev — Grant Without Payment") {
                            Task { await app.devGrant() }
                        }
                        .buttonStyle(.btnBlock)
                        .padding(.top, 10)
                    }

                    HStack(spacing: 20) {
                        Spacer()
                        Linkish(title: "← Back to the front page") { app.screen = .app }
                        Linkish(title: "Sign out") { app.signOut() }
                        Spacer()
                    }
                }
                .frame(maxWidth: 460)
                .frame(maxWidth: .infinity)
                .padding(.horizontal, 20)
                .padding(.top, 30)
                .padding(.bottom, 60)
            }
        }
        .background(Ink.paper)
    }

    private var nameplate: some View {
        VStack(spacing: 12) {
            Text("Subscribe".uppercased())
                .font(.condensed(84))
                .tracking(-1.7)
                .foregroundStyle(Ink.ink)
            Text("Lifetime Access · One Payment".uppercased())
                .font(.mono(10.5))
                .tracking(3.36)
                .foregroundStyle(Ink.inkSoft)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 26)
    }

    /// .pricebox — 3px double border.
    private var pricebox: some View {
        VStack(spacing: 6) {
            Text(app.priceDisplay)
                .font(.condensed(62))
                .foregroundStyle(Ink.red)
            Text("Paid once · Yours for good".uppercased())
                .font(.mono(10.5))
                .tracking(2.1)
                .foregroundStyle(Ink.inkSoft)
        }
        .frame(maxWidth: .infinity)
        .padding(18)
        .overlay(
            Rectangle().strokeBorder(Ink.ink, lineWidth: 1)
                .padding(2)
                .overlay(Rectangle().strokeBorder(Ink.ink, lineWidth: 1))
        )
        .padding(.vertical, 20)
    }

    private var benefitList: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(benefits.enumerated()), id: \.offset) { index, benefit in
                HStack(alignment: .top, spacing: 10) {
                    Text("★")
                        .font(.system(size: 13))
                        .foregroundStyle(Ink.red)
                        .padding(.top, 2)
                    Text(benefit)
                        .font(.serif(15.5))
                        .foregroundStyle(Ink.ink)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .padding(.vertical, 9)
                if index < benefits.count - 1 { Hairline() }
            }
        }
        .padding(.bottom, 6)
    }
}
