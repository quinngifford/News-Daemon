/*  The wire format, exactly as the backend serves it.
 *
 *  Field names match `client_payload()` in backend/app/routers/ingest.py and
 *  the response bodies in app/routers/*.py. Decoding uses
 *  `.convertFromSnakeCase`, so `event_id` arrives as `eventId`.
 *
 *  Everything the detector may or may not send is Optional. The backend stores
 *  detector fields verbatim and forwards fields it has never heard of; a
 *  client that refuses to decode an unexpected shape would be the one thing
 *  standing between a real alert and the person waiting for it.
 */

import Foundation

// MARK: - auth

struct Me: Decodable {
    let id: String
    let email: String
    let entitled: Bool
    let isAdmin: Bool
    let createdAt: String?
}

struct TokenOut: Decodable {
    let accessToken: String
    let tokenType: String?
    let entitled: Bool
}

// MARK: - billing

struct BillingConfig: Decodable {
    let purchaseCount: Int?
    let priceCents: Int
    let priceDisplay: String?
    let currency: String?
    let publishableKey: String?
    let configured: Bool?
    let liveMode: Bool?
    let entitled: Bool?
}

struct CheckoutOut: Decodable {
    let alreadyEntitled: Bool
    let url: String?
    let id: String?
}

struct ConfirmOut: Decodable {
    let entitled: Bool
    let granted: Bool?
    let reason: String?
}

// MARK: - events

struct AlertEvent: Decodable, Identifiable {
    let type: String?
    let id: String
    let eventId: String
    let state: String
    let target: String
    let headline: String
    let url: String?
    let score: Double?
    let detectLatencyMs: Double?
    let occurredAt: String?
    let receivedAt: String?
    let payload: DetectorPayload?

    /// Dedupe key. The web client uses `${event_id}:${state}` for the same job:
    /// a retraction of an event already seen is a new dispatch, a redelivery of
    /// the same state is not.
    var dedupeKey: String { "\(eventId):\(state)" }

    var evidence: [Evidence] { payload?.evidence ?? [] }

    struct DetectorPayload: Decodable {
        let evidence: [Evidence]?
    }

    struct Evidence: Decodable {
        let tier: Int?
        let source: String?
    }
}

struct EventsPage: Decodable {
    let events: [AlertEvent]
    let nextBefore: String?
}

// MARK: - content

struct NewsItem: Decodable, Identifiable {
    let title: String
    let url: String
    let source: String?
    let published: String?
    let summary: String?

    var id: String { url.isEmpty ? title : url }
}

struct NewsResponse: Decodable {
    let items: [NewsItem]
}

/// One point on either chart. The market series carries `t`/`c` only; the coin
/// series is full OHLCV. Same struct serves both, so one chart renderer does.
struct SeriesPoint: Decodable {
    let t: Double
    let c: Double
    let o: Double?
    let h: Double?
    let l: Double?
    let v: Double?
}

struct MarketResponse: Decodable {
    let symbol: String
    let days: Int?
    let source: String?
    let series: [SeriesPoint]
    let last: Double?
    let changePct: Double?
}

struct MemecoinResponse: Decodable {
    let symbol: String?
    let name: String?
    let window: String?
    let mint: String?
    let pool: String?
    let url: String?
    let source: String?
    let series: [SeriesPoint]?
    let points: Int?
    let last: Double?
    let high: Double?
    let low: Double?
    let volume: Double?
    let changePct: Double?
    let unavailable: String?
}

// MARK: - push

struct PushRegisterOut: Decodable {
    let ok: Bool
    let id: String?
    let reused: Bool?
    let error: String?
}

struct PushTestOut: Decodable {
    let ok: Bool
    let sent: Int?
    let devices: Int?
    let error: String?
    let errors: [String]?
}
