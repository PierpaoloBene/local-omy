// swift-tools-version:6.2
import PackageDescription

let package = Package(
    name: "LocalOmy",
    platforms: [.macOS("27.0")],
    targets: [
        .executableTarget(name: "LocalOmy", path: "Sources/LocalOmy",
                          swiftSettings: [.swiftLanguageMode(.v6)]),
    ]
)
