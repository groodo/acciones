import AppKit
import Foundation
import TradingDeskCore

/// Arranca el servicio Python local al abrir la app y lo detiene al salir.
///
/// Si ya hay un servicio escuchando (por ejemplo, lanzado con `scripts/run_service.sh`), se usa ese y no se toca.
/// El intérprete se busca, por orden, en `TRADINGDESK_SERVICE_PYTHON`, en `service/.venv` del repositorio (donde se
/// instala Kronos) y dentro del bundle (`Contents/Resources/service-runtime`, sin Kronos).
@MainActor
final class ServiceSupervisor {
    enum Origin: Equatable {
        case unknown
        case external
        case ownProcess
        case notFound(String)
        case failed(String)

        var label: String {
            switch self {
            case .unknown: return "Comprobando…"
            case .external: return "Arrancado fuera de la app"
            case .ownProcess: return "Arrancado por la app"
            case .notFound(let detail), .failed(let detail): return detail
            }
        }
    }

    private(set) var origin: Origin = .unknown
    private let config: ServiceConfig
    private let env: [String: String]
    private var process: Process?
    private var lastLaunch: Date?

    init(config: ServiceConfig, env: [String: String] = ProcessInfo.processInfo.environment) {
        self.config = config
        self.env = env
        NotificationCenter.default.addObserver(forName: NSApplication.willTerminateNotification, object: nil,
                                               queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.stop() }
        }
    }

    /// Garantiza que hay un servicio respondiendo. Devuelve `true` si lo hay al terminar.
    @discardableResult
    func ensureRunning() async -> Bool {
        if await isHealthy() {
            if process?.isRunning != true { origin = .external }
            return true
        }
        // Evita relanzar en bucle si el servicio cae nada más arrancar.
        if let last = lastLaunch, Date().timeIntervalSince(last) < 20 { return false }
        guard let python = locatePython() else {
            origin = .notFound("No se encuentra Python del servicio. Vuelve a generar la app con scripts/package_macos.sh.")
            return false
        }
        do {
            try launch(python: python)
        } catch {
            origin = .failed("No se pudo arrancar el servicio: \(error.localizedDescription)")
            return false
        }
        for _ in 0..<40 {
            try? await Task.sleep(nanoseconds: 500_000_000)
            if await isHealthy() {
                origin = .ownProcess
                return true
            }
            if process?.isRunning != true { break }
        }
        origin = .failed("El servicio no arrancó. Revisa logs/service-stdout.log en la carpeta de datos.")
        return false
    }

    func stop() {
        guard let process, process.isRunning else { return }
        process.terminate()
        process.waitUntilExit()
        self.process = nil
    }

    private func launch(python: URL) throws {
        let p = Process()
        p.executableURL = python
        p.arguments = ["-m", "tradingdesk"]
        var childEnv = env
        // La app usa la cuenta Paper de IBKR salvo que se pida otra fuente explícitamente.
        childEnv["TRADINGDESK_DATA_SOURCE"] = env["TRADINGDESK_DATA_SOURCE"] ?? "ibkr"
        childEnv["PYTHONUNBUFFERED"] = "1"
        p.environment = childEnv
        let logDir = config.tokenPath.deletingLastPathComponent().appendingPathComponent("logs", isDirectory: true)
        try FileManager.default.createDirectory(at: logDir, withIntermediateDirectories: true)
        let logURL = logDir.appendingPathComponent("service-stdout.log")
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        let handle = try FileHandle(forWritingTo: logURL)
        handle.seekToEndOfFile()
        p.standardOutput = handle
        p.standardError = handle
        try p.run()
        process = p
        lastLaunch = Date()
    }

    private func isHealthy() async -> Bool {
        var request = URLRequest(url: config.baseURL.appendingPathComponent("health"))
        request.timeoutInterval = 1.5
        guard let (_, response) = try? await URLSession.shared.data(for: request) else { return false }
        return (response as? HTTPURLResponse)?.statusCode == 200
    }

    private func locatePython() -> URL? {
        let fm = FileManager.default
        if let custom = env["TRADINGDESK_SERVICE_PYTHON"], fm.isExecutableFile(atPath: custom) {
            return URL(fileURLWithPath: custom)
        }
        // Primero el entorno del repositorio (service/.venv), que es donde instala Kronos scripts/install_kronos.sh:
        // sirve tanto con `swift run` como con la app empaquetada en dist/.
        var dir = Bundle.main.executableURL?.deletingLastPathComponent()
        while let current = dir, current.path != "/" {
            let candidate = current.appendingPathComponent("service/.venv/bin/python")
            if fm.isExecutableFile(atPath: candidate.path) { return candidate }
            dir = current.deletingLastPathComponent()
        }
        if let resources = Bundle.main.resourceURL {
            let bundled = resources.appendingPathComponent("service-runtime/bin/python3")
            if fm.isExecutableFile(atPath: bundled.path) { return bundled }
        }
        return nil
    }
}
