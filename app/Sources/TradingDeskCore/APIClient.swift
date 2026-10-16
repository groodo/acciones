import Foundation

public enum APIError: Error, LocalizedError, Equatable {
    case serviceUnavailable(String)
    case unauthorized
    case missingToken(String)
    case blocked(String)
    case http(Int, String)
    case decoding(String)

    public var errorDescription: String? {
        switch self {
        case .serviceUnavailable(let detail):
            return "Servicio local no disponible. La app intenta arrancarlo sola; si no lo consigue, reiníciala. (\(detail))"
        case .unauthorized:
            return "El servicio local rechazó el token. Reinicia el servicio y la aplicación."
        case .missingToken(let path):
            return "No se encuentra el token del servicio local en \(path). Arranca el servicio primero."
        case .blocked(let message):
            return message
        case .http(let code, let message):
            return message.isEmpty ? "Error \(code) del servicio local." : message
        case .decoding(let detail):
            return "Respuesta del servicio no válida: \(detail)"
        }
    }
}

public struct ServiceConfig: Sendable {
    public var baseURL: URL
    public var tokenPath: URL

    public init(baseURL: URL, tokenPath: URL) {
        self.baseURL = baseURL
        self.tokenPath = tokenPath
    }

    /// Valores por defecto alineados con el servicio Python (127.0.0.1:8765, token en Application Support).
    public static func fromEnvironment(_ env: [String: String] = ProcessInfo.processInfo.environment) -> ServiceConfig {
        let port = env["TRADINGDESK_PORT"].flatMap(Int.init) ?? 8765
        let dataDir: URL
        if let custom = env["TRADINGDESK_DATA_DIR"], !custom.isEmpty {
            dataDir = URL(fileURLWithPath: (custom as NSString).expandingTildeInPath, isDirectory: true)
        } else {
            dataDir = FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("Library/Application Support/AITradingDesk", isDirectory: true)
        }
        return ServiceConfig(
            baseURL: URL(string: "http://127.0.0.1:\(port)")!,
            tokenPath: dataDir.appendingPathComponent("api-token")
        )
    }
}

public protocol HTTPTransport: Sendable {
    func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse)
}

public struct URLSessionTransport: HTTPTransport {
    public init() {}

    public func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw APIError.serviceUnavailable("respuesta no HTTP")
        }
        return (data, http)
    }
}

/// Cliente de la API local. No contiene ninguna llamada capaz de enviar órdenes a un bróker.
public final class APIClient: Sendable {
    public let config: ServiceConfig
    private let transport: HTTPTransport
    private let tokenProvider: @Sendable () throws -> String

    public init(config: ServiceConfig = .fromEnvironment(), transport: HTTPTransport = URLSessionTransport(),
                tokenProvider: (@Sendable () throws -> String)? = nil) {
        self.config = config
        self.transport = transport
        let path = config.tokenPath
        self.tokenProvider = tokenProvider ?? {
            guard let token = try? String(contentsOf: path, encoding: .utf8) else {
                throw APIError.missingToken(path.path)
            }
            return token.trimmingCharacters(in: .whitespacesAndNewlines)
        }
    }

    public func status() async throws -> Envelope<SystemStatus> { try await get("/v1/status") }
    public func account() async throws -> Envelope<AccountSnapshot> { try await get("/v1/account") }
    public func positions() async throws -> Envelope<[Position]> { try await get("/v1/positions") }
    public func signals() async throws -> Envelope<[Signal]> { try await get("/v1/signals") }
    public func orders() async throws -> Envelope<[OrderRecord]> { try await get("/v1/orders") }
    public func killSwitch() async throws -> Envelope<KillSwitchState> { try await get("/v1/kill-switch") }
    public func modelStatus() async throws -> ModelStatus { try await get("/v1/model") }
    public func opportunities() async throws -> Envelope<[Signal]> { try await get("/v1/opportunities") }
    public func watchlist() async throws -> Watchlist { try await get("/v1/watchlist") }
    public func orderLimit() async throws -> OrderLimit { try await get("/v1/order-limit") }

    public func setWatchlist(_ symbols: [String]) async throws -> Watchlist {
        struct Body: Encodable { let symbols: [String] }
        return try await send("PUT", "/v1/watchlist", body: try JSON.encoder().encode(Body(symbols: symbols)))
    }

    public func setOrderLimit(eur: Decimal) async throws -> OrderLimit {
        struct Body: Encodable { let maxRealOrderEur: String }
        let body = Body(maxRealOrderEur: NSDecimalNumber(decimal: eur).stringValue)
        return try await send("PUT", "/v1/order-limit", body: try JSON.encoder().encode(body))
    }

    public func equityCurve(range: String) async throws -> Envelope<EquityCurve> {
        try await get("/v1/equity-curve", query: [URLQueryItem(name: "range", value: range)])
    }

    public func orderEvents(_ clientOrderId: String) async throws -> Envelope<[OrderEvent]> {
        try await get("/v1/orders/\(clientOrderId)/events")
    }

    public func createSimulatedOrder(_ request: SimulatedOrderRequest) async throws -> Envelope<OrderRecord> {
        try await send("POST", "/v1/orders/simulated", body: try JSON.encoder().encode(request))
    }

    public func createPaperOrder(_ request: PaperOrderRequest) async throws -> Envelope<OrderRecord> {
        try await send("POST", "/v1/orders/paper", body: try JSON.encoder().encode(request))
    }

    /// Orden limitada REAL: solo funciona si el servicio tiene la cuenta real con órdenes habilitadas.
    public func createRealOrder(_ request: PaperOrderRequest) async throws -> Envelope<OrderRecord> {
        try await send("POST", "/v1/orders/real", body: try JSON.encoder().encode(request))
    }

    public func cancelOrder(_ clientOrderId: String) async throws -> Envelope<OrderRecord> {
        try await send("POST", "/v1/orders/\(clientOrderId)/cancel", body: nil)
    }

    public func setKillSwitch(active: Bool, reason: String?) async throws -> Envelope<KillSwitchState> {
        struct Body: Encodable { let active: Bool; let reason: String? }
        return try await send("POST", "/v1/kill-switch", body: try JSON.encoder().encode(Body(active: active, reason: reason)))
    }

    // MARK: - Transporte

    private func get<T: Decodable>(_ path: String, query: [URLQueryItem] = []) async throws -> T {
        try await send("GET", path, body: nil, query: query)
    }

    func makeRequest(_ method: String, _ path: String, body: Data?, query: [URLQueryItem] = []) throws -> URLRequest {
        var components = URLComponents(url: config.baseURL.appendingPathComponent(path), resolvingAgainstBaseURL: false)!
        if !query.isEmpty { components.queryItems = query }
        var request = URLRequest(url: components.url!)
        request.httpMethod = method
        request.timeoutInterval = 10
        request.setValue("Bearer \(try tokenProvider())", forHTTPHeaderField: "Authorization")
        if let body {
            request.httpBody = body
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        return request
    }

    private func send<T: Decodable>(_ method: String, _ path: String, body: Data?, query: [URLQueryItem] = []) async throws -> T {
        let request = try makeRequest(method, path, body: body, query: query)
        let data: Data
        let response: HTTPURLResponse
        do {
            (data, response) = try await transport.send(request)
        } catch let error as APIError {
            throw error
        } catch {
            throw APIError.serviceUnavailable(error.localizedDescription)
        }
        switch response.statusCode {
        case 200..<300:
            do {
                return try JSON.decoder().decode(T.self, from: data)
            } catch {
                throw APIError.decoding(String(describing: error))
            }
        case 401:
            throw APIError.unauthorized
        case 423:
            throw APIError.blocked(Self.detail(from: data) ?? "Operativa detenida")
        default:
            throw APIError.http(response.statusCode, Self.detail(from: data) ?? "")
        }
    }

    static func detail(from data: Data) -> String? {
        struct Detail: Decodable { let detail: String }
        struct Validation: Decodable {
            struct Item: Decodable { let msg: String }
            let detail: [Item]
        }
        if let text = (try? JSONDecoder().decode(Detail.self, from: data))?.detail { return text }
        // Errores de validación de FastAPI (422): lista de mensajes.
        return (try? JSONDecoder().decode(Validation.self, from: data))?.detail.map(\.msg).joined(separator: " ")
    }
}
