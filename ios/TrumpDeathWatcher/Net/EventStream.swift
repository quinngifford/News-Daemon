/*  Server-Sent Events — what `new EventSource(...)` does in the browser.
 *
 *  This is the lowest-latency channel the app has: no vendor push
 *  infrastructure in the path, so an open app hears about an event in
 *  milliseconds. Push (APNs) covers the app being closed or backgrounded, and
 *  the two are deduped by (event_id, state) exactly as on the web.
 *
 *  Differences from the browser, both forced by iOS rather than chosen:
 *    - the token goes in the Authorization header instead of ?token=, because
 *      URLSession can set headers and EventSource cannot. The backend accepts
 *      either (see `_bearer` in app/security.py).
 *    - reconnection is ours to implement. EventSource retries on its own; here
 *      the loop below does it, and the connection is dropped on backgrounding
 *      because iOS will kill it anyway.
 */

import Foundation

@MainActor
final class EventStream {

    enum State { case connecting, open, reconnecting }

    private var task: Task<Void, Never>?
    private let session: URLSession

    var onState: ((State) -> Void)?
    var onEvent: ((AlertEvent) -> Void)?

    init() {
        let config = URLSessionConfiguration.default
        // The server sends a `: ping` comment every 20s precisely so an idle
        // proxy does not reap the connection; a 60s request timeout would still
        // be cutting it fine on a slow network, and the resource timeout has to
        // be effectively unlimited or the stream would drop on a timer.
        config.timeoutIntervalForRequest = 120
        config.timeoutIntervalForResource = .infinity
        config.waitsForConnectivity = true
        self.session = URLSession(configuration: config)
    }

    func connect() {
        disconnect()
        onState?(.connecting)
        task = Task { [weak self] in
            guard let self else { return }
            var backoff: UInt64 = 2
            while !Task.isCancelled {
                do {
                    try await self.run()
                    // A clean end of stream is still a disconnection: the
                    // server closed, so reconnect rather than going quiet.
                    if Task.isCancelled { return }
                    self.onState?(.reconnecting)
                } catch is CancellationError {
                    return
                } catch {
                    if Task.isCancelled { return }
                    self.onState?(.reconnecting)
                }
                try? await Task.sleep(nanoseconds: backoff * 1_000_000_000)
                backoff = min(backoff * 2, 30)
            }
        }
    }

    func disconnect() {
        task?.cancel()
        task = nil
    }

    private func run() async throws {
        var req = URLRequest(url: APIClient.shared.url("/api/events/stream"))
        req.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        req.setValue("no-cache", forHTTPHeaderField: "Cache-Control")
        if let token = TokenStore.token {
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }

        let (bytes, response) = try await session.bytes(for: req)
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard code == 200 else {
            // 401 (signed out) or 402/403 (not entitled). Retrying changes
            // nothing until the entitlement does, and AppState reconnects on
            // its own when that happens.
            throw APIError.status(code)
        }
        onState?(.open)

        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase

        for try await line in bytes.lines {
            if Task.isCancelled { return }
            // Comments (": ping") and field lines we do not use are skipped;
            // only `data:` carries a dispatch.
            guard line.hasPrefix("data:") else { continue }
            let body = line.dropFirst(5).trimmingCharacters(in: .whitespaces)
            guard !body.isEmpty, let data = body.data(using: .utf8) else { continue }
            guard let event = try? decoder.decode(AlertEvent.self, from: data) else { continue }
            guard event.type == "alert" else { continue }
            onEvent?(event)
        }
    }
}
