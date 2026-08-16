/*  HTTP to the backend.
 *
 *  The mirror of `api()` in web/app.js: JSON in, JSON out, bearer token
 *  attached when we hold one. The backend README calls this out explicitly —
 *  the API is bearer-token based so a native app can use it unchanged, so
 *  nothing here is app-specific.
 *
 *  Errors are unwrapped the same way the web client unwraps them, including
 *  FastAPI's habit of returning validation failures as a list of objects.
 */

import Foundation

enum APIError: LocalizedError {
    case message(String)
    case status(Int)

    var errorDescription: String? {
        switch self {
        case .message(let m): return m
        case .status(let code): return "Request failed (\(code))"
        }
    }
}

final class APIClient {
    static let shared = APIClient()

    let baseURL: URL
    private let session: URLSession
    private let decoder: JSONDecoder

    private init() {
        self.baseURL = AppConfig.apiBaseURL

        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 20
        config.waitsForConnectivity = true
        self.session = URLSession(configuration: config)

        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        self.decoder = d
    }

    // MARK: - plumbing

    func url(_ path: String) -> URL {
        URL(string: path, relativeTo: baseURL) ?? baseURL
    }

    private func request(_ path: String,
                         method: String = "GET",
                         body: [String: Any]? = nil) -> URLRequest {
        var req = URLRequest(url: url(path))
        req.httpMethod = method
        if let body {
            req.httpBody = try? JSONSerialization.data(withJSONObject: body)
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        if let token = TokenStore.token {
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        return req
    }

    /// FastAPI returns `{"detail": "..."}` for handled errors and
    /// `{"detail": [{"msg": "..."}]}` for validation ones. app.js handles both;
    /// so does this, or a bad password would read as "Something went wrong".
    private func detail(from data: Data, fallback: String) -> String {
        guard let obj = try? JSONSerialization.jsonObject(with: data),
              let dict = obj as? [String: Any] else { return fallback }
        if let s = dict["detail"] as? String { return s }
        if let list = dict["detail"] as? [[String: Any]],
           let msg = list.first?["msg"] as? String { return msg }
        return fallback
    }

    @discardableResult
    private func send(_ req: URLRequest, fallback: String) async throws -> Data {
        let (data, response) = try await session.data(for: req)
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(code) else {
            throw APIError.message(detail(from: data, fallback: "\(fallback) (\(code))"))
        }
        return data
    }

    private func decode<T: Decodable>(_ type: T.Type, from data: Data) throws -> T {
        try decoder.decode(T.self, from: data)
    }

    func get<T: Decodable>(_ path: String, as type: T.Type) async throws -> T {
        let data = try await send(request(path), fallback: "Request failed")
        return try decode(T.self, from: data)
    }

    func post<T: Decodable>(_ path: String,
                            body: [String: Any]? = nil,
                            as type: T.Type,
                            fallback: String = "Request failed") async throws -> T {
        let data = try await send(request(path, method: "POST", body: body),
                                  fallback: fallback)
        return try decode(T.self, from: data)
    }

    /// `GET`, but a non-2xx is `nil` rather than a throw — for the calls the
    /// web client deliberately swallows (billing config, an expired token on
    /// boot), where failing loudly would block a page that works fine without.
    func getOptional<T: Decodable>(_ path: String, as type: T.Type) async -> T? {
        guard let (data, response) = try? await session.data(for: request(path)),
              let code = (response as? HTTPURLResponse)?.statusCode,
              (200..<300).contains(code) else { return nil }
        return try? decode(T.self, from: data)
    }

    // MARK: - endpoints

    func me() async -> Me? { await getOptional("/api/auth/me", as: Me.self) }

    func billingConfig() async -> BillingConfig? {
        await getOptional("/api/billing/config", as: BillingConfig.self)
    }

    func auth(mode: String, email: String, password: String) async throws -> TokenOut {
        try await post("/api/auth/\(mode)",
                       body: ["email": email, "password": password],
                       as: TokenOut.self,
                       fallback: "Something went wrong")
    }

    func devGrant() async throws -> Me {
        try await post("/api/auth/dev-grant", as: Me.self, fallback: "Not available")
    }

    func checkout() async throws -> CheckoutOut {
        try await post("/api/billing/checkout", as: CheckoutOut.self,
                       fallback: "Checkout unavailable")
    }

    func confirm(sessionId: String) async throws -> ConfirmOut {
        try await post("/api/billing/confirm", body: ["session_id": sessionId],
                       as: ConfirmOut.self, fallback: "Could not verify payment")
    }

    func events(limit: Int = 40) async throws -> EventsPage {
        try await get("/api/events?limit=\(limit)", as: EventsPage.self)
    }

    func news(limit: Int = 14) async throws -> NewsResponse {
        try await get("/api/news?limit=\(limit)", as: NewsResponse.self)
    }

    func market(symbol: String = "TRUMP", days: Int) async throws -> MarketResponse {
        try await get("/api/market/\(symbol)?days=\(days)", as: MarketResponse.self)
    }

    func memecoin(window: String) async throws -> MemecoinResponse {
        let w = window.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? "24h"
        return try await get("/api/memecoin?window=\(w)", as: MemecoinResponse.self)
    }

    func registerDevice(token: String, userAgent: String) async throws -> PushRegisterOut {
        try await post("/api/push/register",
                       body: ["kind": "apns", "token": token, "user_agent": userAgent],
                       as: PushRegisterOut.self,
                       fallback: "Could not register this device.")
    }

    func unregisterDevice(token: String) async {
        _ = try? await post("/api/push/unregister", body: ["token": token],
                            as: PushRegisterOut.self)
    }

    func testPush() async throws -> PushTestOut {
        try await post("/api/push/test", as: PushTestOut.self)
    }
}
