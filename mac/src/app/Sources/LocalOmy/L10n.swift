import Foundation

/// Italian or English, after the Mac: Italian if it comes before English
/// in System Settings → General → Language & Region, else English. Read at
/// launch; omy gets the same (OMY_LANG) for what it shows.
enum L10n {
    static let english: Bool = {
        for l in Locale.preferredLanguages {
            if l.hasPrefix("it") { return false }
            if l.hasPrefix("en") { return true }
        }
        return true
    }()
    static let code = english ? "en" : "it"
    static let locale = Locale(identifier: english ? "en_US" : "it_IT")
}

/// The text in the app's language, the Italian first.
func tr(_ it: String, _ en: String) -> String { L10n.english ? en : it }
