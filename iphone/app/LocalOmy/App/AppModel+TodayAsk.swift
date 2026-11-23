import Foundation
import OmyKit

extension AppModel {
    /// A model for previews: temporary storage, no Mac configured.
    static func preview() -> AppModel {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("omy-preview", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return AppModel(documents: dir, defaults: UserDefaults(suiteName: "omy-preview") ?? .standard)
    }
}
