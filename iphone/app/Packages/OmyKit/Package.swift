// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "OmyKit",
    platforms: [.iOS("27.0"), .macOS("27.0")],
    products: [.library(name: "OmyKit", targets: ["OmyKit"])],
    targets: [
        .target(name: "OmyKit"),
        .testTarget(name: "OmyKitTests", dependencies: ["OmyKit"]),
    ],
    swiftLanguageModes: [.v6]
)
