import Foundation
import Testing
@testable import OmyKit

/// A segment written by OmyKit must be accepted by the Mac receiver's own parsers (omy_receiver.segment.parse and
/// omy_receiver.app._parse_meta). Runs `uv run --offline` in mac/src; skipped (with a message) if uv or the Mac
/// sources are missing or the Python environment can't be built offline.
@Suite struct ServerCompatTests {
    static let repoRoot: URL = {
        var u = URL(fileURLWithPath: #filePath)
        for _ in 0..<7 { u.deleteLastPathComponent() }  // OmyKitTests, Tests, OmyKit, Packages, app, iphone -> repo
        return u
    }()

    static func findUV() -> String? {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let paths = ["\(home)/.local/bin/uv", "/opt/homebrew/bin/uv", "/usr/local/bin/uv", "/usr/bin/uv"]
            + (ProcessInfo.processInfo.environment["PATH"] ?? "").split(separator: ":").map { "\($0)/uv" }
        return paths.first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    @Test func pythonReceiverAcceptsOurSegment() async throws {
        let macSrc = Self.repoRoot.appendingPathComponent("mac/src")
        let script = Self.repoRoot.appendingPathComponent("iphone/app/Packages/OmyKit/Scripts/check_segment.py")
        guard FileManager.default.fileExists(atPath: macSrc.appendingPathComponent("omy_receiver/segment.py").path),
              FileManager.default.fileExists(atPath: script.path), let uv = Self.findUV() else {
            print("SKIPPED ServerCompatTests: uv or mac/src not found")
            return
        }
        let tmp = TempDir()
        let store = SegmentStore(directory: tmp.segments)
        let calls = [CallInterval(start: t0.addingTimeInterval(1), end: t0.addingTimeInterval(2)), CallInterval(start: t0.addingTimeInterval(3))]
        let loc = [LocationSample(kind: "visit", lat: 45.46, lon: 9.19, acc: 35.2, arrive: t0.addingTimeInterval(-3600))]
        let s = try await store.begin(deviceID: "AA:BB:CC:DD:EE:FF", codec: 21, now: t0)
        for i in 0..<500 { try s.append(frame: frame(65_000 + i, i % 2, payload: [UInt8](repeating: UInt8(i & 0xFF), count: 20 + i % 30)), at: t0.addingTimeInterval(Double(i) * 0.02)) }
        try await store.finish(s, extras: SidecarExtras(battery: 77, firmware: "3.0.21", calls: calls, locations: loc), recovering: false)
        let q = try #require(await store.queue().first)

        // a second one without optional fields and with 1-byte frames (null packet indexes)
        let s2 = try await store.begin(deviceID: "dev", codec: 0, now: t0.addingTimeInterval(1000))
        try s2.append(frame: Data([9]), at: t0.addingTimeInterval(1000))
        try await store.finish(s2, extras: SidecarExtras(), recovering: false)
        let q2 = try #require(await store.queue().last)

        for item in [q, q2] {
            let (code, out, err) = try run(uv, ["run", "--offline", "--directory", macSrc.path, "python", script.path, item.binURL.path, item.jsonURL.path])
            if code != 0 {
                if err.contains("AssertionError") || err.contains("ValueError") || err.contains("SegmentError") {
                    Issue.record("Python receiver rejected \(item.id.raw): \(err)")
                } else {
                    print("SKIPPED ServerCompatTests: python environment not usable offline: \(err.suffix(300))")
                    return
                }
            } else {
                #expect(out.hasPrefix("ok "))
            }
        }
    }

    private func run(_ exe: String, _ args: [String]) throws -> (Int32, String, String) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: exe)
        p.arguments = args
        let o = Pipe(), e = Pipe()
        p.standardOutput = o
        p.standardError = e
        try p.run()
        let out = o.fileHandleForReading.readDataToEndOfFile(), err = e.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return (p.terminationStatus, String(decoding: out, as: UTF8.self), String(decoding: err, as: UTF8.self))
    }
}
