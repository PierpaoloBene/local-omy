import Foundation

/// Italian or English after the iPhone's language: Italian if it comes before English in the preferred
/// languages, else English (default English, as the Flutter app).
public enum OmyLanguage {
    public static let english: Bool = isEnglish(Locale.preferredLanguages)

    public static func isEnglish(_ languages: [String]) -> Bool {
        for l in languages {
            let code = l.lowercased()
            if code == "it" || code.hasPrefix("it-") || code.hasPrefix("it_") { return false }
            if code == "en" || code.hasPrefix("en-") || code.hasPrefix("en_") { return true }
        }
        return true
    }
}

/// The text in the app's language, the Italian first.
public func tr(_ it: String, _ en: String) -> String { OmyLanguage.english ? en : it }

/// "1 sessione", "3 sessioni"; "1 session", "3 sessions".
public func counted(_ n: Int, _ itOne: String, _ itMany: String, _ enOne: String, _ enMany: String) -> String {
    "\(n) \(n == 1 ? tr(itOne, enOne) : tr(itMany, enMany))"
}
