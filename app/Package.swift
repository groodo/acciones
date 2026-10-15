// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "AITradingDesk",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "AITradingDesk", targets: ["TradingDeskApp"]),
        .library(name: "TradingDeskCore", targets: ["TradingDeskCore"]),
    ],
    targets: [
        // Modelos, cliente de la API local y formato. Sin dependencias de UI para poder probarlo aislado.
        .target(name: "TradingDeskCore"),
        .executableTarget(name: "TradingDeskApp", dependencies: ["TradingDeskCore"]),
        .testTarget(name: "TradingDeskCoreTests", dependencies: ["TradingDeskCore"]),
    ]
)
