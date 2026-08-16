/*  Where the bearer token lives.
 *
 *  The web client keeps it in localStorage so an installed PWA survives a cold
 *  start. The Keychain is the equivalent here, and it survives a device
 *  restart and an app update without the token ever touching a backup in the
 *  clear (kSecAttrAccessibleAfterFirstUnlock — the app must be able to refresh
 *  its stream in the background, which rules out WhenUnlocked).
 */

import Foundation
import Security

enum TokenStore {
    private static let service = "com.trumpdeathwatcher.app"
    private static let account = "ticker_token"

    static var token: String? {
        get { read() }
        set { newValue.map { write($0) } ?? delete() }
    }

    private static func baseQuery() -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
    }

    private static func read() -> String? {
        var query = baseQuery()
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne

        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data,
              let value = String(data: data, encoding: .utf8),
              !value.isEmpty else { return nil }
        return value
    }

    private static func write(_ value: String) {
        let data = Data(value.utf8)
        let query = baseQuery()

        let update: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlock,
        ]
        let status = SecItemUpdate(query as CFDictionary, update as CFDictionary)
        if status == errSecItemNotFound {
            var insert = query
            insert[kSecValueData as String] = data
            insert[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
            _ = SecItemAdd(insert as CFDictionary, nil)
        }
    }

    private static func delete() {
        _ = SecItemDelete(baseQuery() as CFDictionary)
    }
}
