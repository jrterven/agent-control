// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "AgentControlSetup",
    platforms: [.macOS(.v13)],
    products: [
        .executable(name: "AgentControl", targets: ["AgentControl"]),
        .executable(name: "AgentControlService", targets: ["AgentControlService"]),
        .executable(name: "AgentControlUpdater", targets: ["AgentControlUpdater"]),
    ],
    targets: [
        .executableTarget(name: "AgentControl"),
        .executableTarget(name: "AgentControlService"),
        .executableTarget(name: "AgentControlUpdater"),
    ]
)
