/*  The sign-in gate. A destination, not a wall — hence the way back.  */

import SwiftUI

struct AuthView: View {
    @EnvironmentObject private var app: AppState
    @State private var email = ""
    @State private var password = ""
    @FocusState private var focus: FieldID?

    private enum FieldID { case email, password }

    private var isLogin: Bool { app.authMode == "login" }
    private var canSubmit: Bool {
        !email.isEmpty && password.count >= 10 && !app.authBusy
    }

    var body: some View {
        VStack(spacing: 0) {
            FlagBar()
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    nameplate
                    RuleDouble()
                    DatelineBar(left: Fmt.today(), right: "Vol. I · No. 1")
                    Stars()

                    Lede(text: "When it happens, seconds decide everything. Trump Death "
                         + "Watcher keeps a continuous watch on the wire services, the "
                         + "newsrooms, and the public record — and tells you the moment "
                         + "it is confirmed.")
                        .padding(.top, 10)

                    tabs
                    form

                    RuleThin()
                    Text("One payment · \(app.priceDisplay) · No subscription".uppercased())
                        .font(.mono(10.5))
                        .tracking(1.47)
                        .foregroundStyle(Ink.inkFaint)
                        .frame(maxWidth: .infinity)
                        .padding(.bottom, 10)

                    HStack {
                        Spacer()
                        Linkish(title: "← Back to the front page") { app.screen = .app }
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
            Text("Trump Death Watcher".uppercased())
                .font(.condensed(60))
                .tracking(-1.2)
                .foregroundStyle(Ink.ink)
                .multilineTextAlignment(.center)
                .lineSpacing(-8)
            Text("Est. 2026 · Continuous Vital Status".uppercased())
                .font(.mono(10.5))
                .tracking(3.36)
                .foregroundStyle(Ink.inkSoft)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 26)
    }

    private var tabs: some View {
        HStack(spacing: 0) {
            tab("Sign In", mode: "login")
            Ink.ink.frame(width: 2)
            tab("Register", mode: "signup")
        }
        .overlay(Rectangle().strokeBorder(Ink.ink, lineWidth: 2))
        .padding(.top, 22)
        .padding(.bottom, 20)
    }

    private func tab(_ title: String, mode: String) -> some View {
        Button {
            app.authMode = mode
            app.authError = nil
        } label: {
            Text(title.uppercased())
                .font(.mono(11))
                .tracking(1.76)
                .foregroundStyle(app.authMode == mode ? Ink.paper : Ink.inkSoft)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 10)
                .background(app.authMode == mode ? Ink.ink : Ink.paper)
        }
        .buttonStyle(.plain)
    }

    private var form: some View {
        VStack(alignment: .leading, spacing: 0) {
            Field(label: "Electronic Mail") {
                TextField("you@example.com", text: $email)
                    .keyboardType(.emailAddress)
                    .textContentType(.emailAddress)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .submitLabel(.next)
                    .focused($focus, equals: .email)
                    .onSubmit { focus = .password }
            }

            Field(label: "Password") {
                SecureField("Ten characters or more", text: $password)
                    .textContentType(isLogin ? .password : .newPassword)
                    .submitLabel(.go)
                    .focused($focus, equals: .password)
                    .onSubmit { submit() }
            }

            if let error = app.authError {
                Notice(text: error).padding(.bottom, 14)
            }

            Button(app.authBusy ? "Working…" : (isLogin ? "Sign In" : "Register")) {
                submit()
            }
            .buttonStyle(.btnRedBlock)
            .disabled(!canSubmit)
        }
    }

    private func submit() {
        guard canSubmit else { return }
        focus = nil
        Task { await app.submitAuth(email: email, password: password) }
    }
}
