import Foundation

/// Runs the `omy` CLI (mac/src) through uv. The app holds no logic of its own:
/// every action is an `omy --json …` command, its stderr is the progress log.
enum Omy {
    /// mac/src. The app is built into mac/build/local-omy.app, so by default
    /// it is ../src next to the bundle; the "macSrcPath" default overrides it.
    static var macSrc: URL {
        if let path = UserDefaults.standard.string(forKey: "macSrcPath") {
            return URL(fileURLWithPath: path)
        }
        return Bundle.main.bundleURL.deletingLastPathComponent()
            .deletingLastPathComponent().appendingPathComponent("src")
    }

    static let home = FileManager.default.homeDirectoryForCurrentUser.path

    /// GUI apps get a minimal PATH; omy needs Homebrew, uv and Xcode tools.
    static let path = [
        "/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", "\(home)/.local/bin",
        "/usr/bin", "/bin", "/usr/sbin", "/sbin",
    ].joined(separator: ":")

    static var uv: String? {
        ["\(home)/.local/bin/uv", "/opt/homebrew/bin/uv", "/usr/local/bin/uv"]
            .first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    struct Failure: Error, LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    /// Run `omy --json <args>`; `log` receives stderr line by line (on a
    /// background thread). Returns stdout (the JSON result).
    static func run(_ args: [String], log: (@Sendable (String) -> Void)? = nil) async throws -> Data {
        guard let uv else { throw Failure(message: tr("uv non trovato (~/.local/bin/uv)", "uv not found (~/.local/bin/uv)")) }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: uv)
        process.arguments = ["run", "--quiet", "--project", macSrc.path, "omy", "--json"] + args
        process.currentDirectoryURL = macSrc
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = path
        // What omy shows (checklist, features) in the app's language.
        env["OMY_LANG"] = L10n.code
        process.environment = env

        let out = Pipe(), err = Pipe()
        process.standardOutput = out
        process.standardError = err
        let lines = LineSplitter(log)
        err.fileHandleForReading.readabilityHandler = { handle in
            lines.feed(handle.availableData)
        }

        let stdout = OutputBuffer()
        out.fileHandleForReading.readabilityHandler = { handle in
            stdout.append(handle.availableData)
        }

        try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Void, Error>) in
            process.terminationHandler = { _ in cont.resume() }
            do { try process.run() } catch { cont.resume(throwing: error) }
        }
        out.fileHandleForReading.readabilityHandler = nil
        err.fileHandleForReading.readabilityHandler = nil
        stdout.append(out.fileHandleForReading.readDataToEndOfFile())
        lines.feed(err.fileHandleForReading.readDataToEndOfFile())
        lines.flush()

        let data = stdout.data
        if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let message = obj["error"] as? String {
            throw Failure(message: message)
        }
        if process.terminationStatus != 0 {
            throw Failure(message: tr("omy \(args.joined(separator: " ")) è uscito con codice \(process.terminationStatus)",
                                      "omy \(args.joined(separator: " ")) exited with code \(process.terminationStatus)"))
        }
        return data
    }

    static func decode<T: Decodable>(_ type: T.Type, _ args: [String]) async throws -> T {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(type, from: try await run(args))
    }
}

private final class OutputBuffer: @unchecked Sendable {
    private let lock = NSLock()
    private var buffer = Data()
    func append(_ d: Data) { lock.lock(); buffer.append(d); lock.unlock() }
    var data: Data { lock.lock(); defer { lock.unlock() }; return buffer }
}

/// Splits a byte stream into lines (also on \r, for curl/xcodebuild progress).
private final class LineSplitter: @unchecked Sendable {
    private let lock = NSLock()
    private var pending = Data()
    private let sink: (@Sendable (String) -> Void)?
    init(_ sink: (@Sendable (String) -> Void)?) { self.sink = sink }

    func feed(_ data: Data) {
        guard !data.isEmpty else { return }
        lock.lock()
        pending.append(data)
        var lines: [String] = []
        while let i = pending.firstIndex(where: { $0 == 0x0A || $0 == 0x0D }) {
            let line = String(decoding: pending[pending.startIndex..<i], as: UTF8.self)
            pending.removeSubrange(pending.startIndex...i)
            if !line.isEmpty { lines.append(line) }
        }
        lock.unlock()
        lines.forEach { sink?($0) }
    }

    func flush() {
        lock.lock()
        let rest = String(decoding: pending, as: UTF8.self)
        pending.removeAll()
        lock.unlock()
        if !rest.isEmpty { sink?(rest) }
    }
}
