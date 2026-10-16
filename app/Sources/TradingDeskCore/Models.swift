import Foundation

/// Decimal transportado como texto en JSON para no perder precisión contable.
public struct Dec: Codable, Hashable, Comparable, Sendable {
    public let value: Decimal

    public init(_ value: Decimal) { self.value = value }

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        let text = try container.decode(String.self)
        guard let parsed = Decimal(string: text, locale: Locale(identifier: "en_US_POSIX")) else {
            throw DecodingError.dataCorruptedError(in: container, debugDescription: "Importe no decimal: \(text)")
        }
        value = parsed
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(NSDecimalNumber(decimal: value).stringValue)
    }

    public static func < (lhs: Dec, rhs: Dec) -> Bool { lhs.value < rhs.value }
}

public struct Money: Codable, Hashable, Sendable {
    public let amount: Dec
    public let currency: String

    public init(amount: Decimal, currency: String) {
        self.amount = Dec(amount)
        self.currency = currency
    }

    /// Identificador idempotente: reenviar la misma petición no duplica la orden.
    public static func newClientOrderId() -> String {
        let raw = UUID().uuidString.replacingOccurrences(of: "-", with: "")
        return "UI-" + String(raw.prefix(16))
    }
}

/// Toda respuesta del servicio indica su fuente. Los datos simulados nunca se ocultan.
public struct Envelope<T: Decodable>: Decodable {
    public let source: String
    public let isMock: Bool
    public let mode: String
    public let generatedAt: Date
    public let notice: String?
    public let data: T
}

public enum Freshness: String, Codable, Sendable {
    case realTime = "tiempo_real"
    case delayed = "diferidas"
    case unavailable = "no_disponibles"
    case simulated = "simuladas"

    public var label: String {
        switch self {
        case .realTime: return "Tiempo real"
        case .delayed: return "Diferidas"
        case .unavailable: return "No disponibles"
        case .simulated: return "Simuladas"
        }
    }
}

public struct FxRate: Codable, Hashable, Sendable {
    public let pair: String
    public let rate: Dec
    public let source: String
    public let asOf: Date
}

public struct AccountSnapshot: Codable, Hashable, Sendable {
    public let accountIdMasked: String
    public let mode: String
    public let timestamp: Date
    public let baseCurrency: String
    public let netLiquidation: Money
    public let netLiquidationEur: Money
    public let fx: FxRate?
    public let settledCash: [Money]
    public let availableFunds: Money
    public let buyingPower: Money
    public let unrealizedPnl: Money
    public let realizedPnl: Money
    public let realizedFees: Money
    public let dailyPnl: Money
    public let dailyPnlPct: Dec
    public let dailyPnlBasis: String
    public let investedCapital: Money
    public let exposure: Money
    public let riskBudget: Money
    public let freshness: Freshness
}

public struct Position: Codable, Hashable, Identifiable, Sendable {
    public var id: String { symbol }
    public let symbol: String
    public let name: String
    public let quantity: Dec
    public let currency: String
    public let avgPrice: Dec
    public let lastPrice: Dec
    public let unrealizedPnl: Money
    public let changePct: Dec?
    public let stop: Dec?
    public let target: Dec?
    public let estimatedFee: Money?
}

public enum Proposal: String, Codable, Sendable {
    case buy = "COMPRAR"
    case sellPosition = "VENDER_POSICION"
    case wait = "ESPERAR"
    case notEvaluable = "NO_EVALUABLE"

    public var label: String {
        switch self {
        case .buy: return "Comprar"
        case .sellPosition: return "Vender posición"
        case .wait: return "Esperar"
        case .notEvaluable: return "No evaluable"
        }
    }
}

public enum SignalStatus: String, Codable, Sendable {
    case study = "EN_ESTUDIO"
    case paperReady = "APTA_PAPER"
    case blocked = "BLOQUEADA"
    case expired = "CADUCADA"

    public var label: String {
        switch self {
        case .study: return "Señal en estudio"
        case .paperReady: return "Apta para paper"
        case .blocked: return "Bloqueada"
        case .expired: return "Caducada"
        }
    }
}

public struct Signal: Codable, Hashable, Identifiable, Sendable {
    public var id: String { symbol }
    public let symbol: String
    public let name: String
    public let price: Dec?
    public let changePct: Dec?
    public let freshness: Freshness
    public let trend: String
    public let proposal: Proposal
    public let horizonMinutes: Int
    public let netEvalAfterCosts: Money?
    public let status: SignalStatus
    public let reasons: [String]
    public let contradictions: [String]
    public let modelVersion: String?
}

public struct EquityPoint: Codable, Hashable, Sendable {
    public let timestamp: Date
    public let equityEur: Dec
    public let netFlowsEur: Dec
}

public struct EquityCurve: Codable, Hashable, Sendable {
    public let range: String
    public let sufficient: Bool
    public let message: String?
    public let points: [EquityPoint]
}

public enum OrderState: String, Codable, Sendable, CaseIterable {
    case draft = "DRAFT"
    case riskApproved = "RISK_APPROVED"
    case submitting = "SUBMITTING"
    case acknowledged = "ACKNOWLEDGED"
    case partiallyFilled = "PARTIALLY_FILLED"
    case filled = "FILLED"
    case cancelled = "CANCELLED"
    case rejected = "REJECTED"
    case unknown = "UNKNOWN"

    public var label: String {
        switch self {
        case .draft: return "Propuesta"
        case .riskApproved: return "Aprobada por riesgo"
        case .submitting: return "Enviando"
        case .acknowledged: return "Pendiente"
        case .partiallyFilled: return "Parcial"
        case .filled: return "Ejecutada"
        case .cancelled: return "Cancelada"
        case .rejected: return "Rechazada"
        case .unknown: return "Estado desconocido (en conciliación)"
        }
    }

    public var isTerminal: Bool { [.filled, .cancelled, .rejected].contains(self) }
}

public struct OrderRecord: Codable, Hashable, Identifiable, Sendable {
    public var id: String { clientOrderId }
    public let clientOrderId: String
    public let mode: String
    public let isMock: Bool
    public let symbol: String
    public let side: String
    public let quantity: Dec
    public let limitPrice: Dec
    public let currency: String
    public let state: OrderState
    public let createdAt: Date
    public let updatedAt: Date
    public let filledQuantity: Dec
    public let avgFillPrice: Dec?
    public let commission: Money?
    public let ibkrOrderId: Int?
    public let ibkrPermId: Int?
    public let note: String?
    /// "LMT" limitada o "STP" stop (en un stop, `limitPrice` es el precio de disparo).
    public let orderType: String?
    /// Ventas de protección que cuelgan de una compra (orden bracket).
    public let parentClientOrderId: String?

    public var isStop: Bool { orderType == "STP" }
    public var isTakeProfit: Bool { side == "SELL" && !isStop && clientOrderId.hasSuffix("-TP") }

    /// «Compra», «Venta», «Venta · ganancia» o «Venta · stop».
    public var sideLabel: String {
        if side == "BUY" { return "Compra" }
        if isStop { return "Venta · stop" }
        return isTakeProfit ? "Venta · ganancia" : "Venta"
    }
}

public struct OrderEvent: Codable, Hashable, Sendable {
    public let clientOrderId: String
    public let fromState: OrderState?
    public let toState: OrderState
    public let reason: String
    public let at: Date
}

public struct KillSwitchState: Codable, Hashable, Sendable {
    public let active: Bool
    public let reason: String?
    public let changedAt: Date?
    public let openOrders: [OrderRecord]
    public let openPositions: [Position]
    public let message: String
}

public struct SystemStatus: Codable, Hashable, Sendable {
    public let broker: String
    public let brokerDetail: String
    public let mode: String
    public let modeLabel: String
    public let market: String
    public let marketDetail: String
    public let quotes: Freshness
    public let lastUpdate: Date
    public let killSwitchActive: Bool
    public let tradingAllowed: Bool
    public let alerts: [String]
    public let serviceVersion: String
    /// Tope de importe por orden Paper; solo existe cuando el envío de órdenes está habilitado.
    public let maxOrderValue: Money?
}

/// Estado de Kronos-small en el servicio local (`GET /v1/model`).
public struct ModelStatus: Codable, Hashable, Sendable {
    public let installed: Bool
    public let loaded: Bool
    public let modelId: String
    public let version: String?
    public let device: String?
    public let detail: String
    public let lastLatencyMs: Int?
    public let lastRun: Date?
    public let watchlist: [String]

    /// Texto corto para la pantalla de Configuración.
    public var summary: String {
        if loaded { return "Funcionando" + (device.map { " (\($0))" } ?? "") }
        return installed ? "Instalado" : "No instalado"
    }
}

/// Orden limitada para la cuenta Paper de IBKR. Solo se envía tras la confirmación del usuario.
public struct PaperOrderRequest: Encodable, Sendable {
    public let clientOrderId: String
    public let symbol: String
    public let side: String
    public let quantity: String
    public let limitPrice: String
    public let currency: String
    public let confirmed: Bool
    /// Solo en compras: venta automática de ganancia máxima y stop de pérdida máxima (nil = sin ellas).
    public let takeProfitPrice: String?
    public let stopLossPrice: String?

    public init(symbol: String, side: String, quantity: Int, limitPrice: Decimal, currency: String = "USD",
                confirmed: Bool, clientOrderId: String = SimulatedOrderRequest.newClientOrderId(),
                protection: Protection? = nil) {
        self.clientOrderId = clientOrderId
        self.symbol = symbol.uppercased()
        self.side = side
        self.quantity = String(quantity)
        self.limitPrice = NSDecimalNumber(decimal: limitPrice).stringValue
        self.currency = currency
        self.confirmed = confirmed
        self.takeProfitPrice = protection.map { NSDecimalNumber(decimal: $0.takeProfit).stringValue }
        self.stopLossPrice = protection.map { NSDecimalNumber(decimal: $0.stopLoss).stringValue }
    }
}

/// Precios de la venta de ganancia máxima y del stop de pérdida máxima, a partir de un precio y dos porcentajes.
public struct Protection: Hashable, Sendable {
    public let takeProfit: Decimal
    public let stopLoss: Decimal

    public init(takeProfit: Decimal, stopLoss: Decimal) {
        self.takeProfit = takeProfit
        self.stopLoss = stopLoss
    }

    /// Redondea a céntimos: la ganancia hacia abajo y el stop hacia arriba, para no pasarse de los porcentajes.
    public init(entry: Decimal, gainPct: Decimal, lossPct: Decimal) {
        takeProfit = Self.round(entry * (1 + gainPct / 100), .down)
        stopLoss = Self.round(entry * (1 - lossPct / 100), .up)
    }

    /// Lo que se perdería, como mucho, si salta el stop a su precio (sin contar deslizamiento ni comisiones).
    public func maxLoss(entry: Decimal, quantity: Int) -> Decimal { (entry - stopLoss) * Decimal(quantity) }
    public func maxGain(entry: Decimal, quantity: Int) -> Decimal { (takeProfit - entry) * Decimal(quantity) }

    static func round(_ value: Decimal, _ mode: NSDecimalNumber.RoundingMode) -> Decimal {
        var input = value
        var output = Decimal()
        NSDecimalRound(&output, &input, 2, mode)
        return output
    }
}

/// Protege acciones que ya tienes (`/v1/orders/protect`).
public struct ProtectRequest: Encodable, Sendable {
    public let clientOrderId: String
    public let symbol: String
    public let quantity: String
    public let takeProfitPrice: String
    public let stopLossPrice: String
    public let currency: String
    public let confirmed: Bool

    public init(symbol: String, quantity: Int, protection: Protection, currency: String = "USD", confirmed: Bool,
                clientOrderId: String = SimulatedOrderRequest.newClientOrderId()) {
        self.clientOrderId = clientOrderId
        self.symbol = symbol.uppercased()
        self.quantity = String(quantity)
        self.takeProfitPrice = NSDecimalNumber(decimal: protection.takeProfit).stringValue
        self.stopLossPrice = NSDecimalNumber(decimal: protection.stopLoss).stringValue
        self.currency = currency
        self.confirmed = confirmed
    }
}

/// Tu lista de seguimiento (`/v1/watchlist`).
public struct Watchlist: Codable, Hashable, Sendable {
    public let symbols: [String]
    public let max: Int
}

/// Tope por orden real en euros (`/v1/order-limit`). Los importes llegan como texto decimal.
public struct OrderLimit: Codable, Hashable, Sendable {
    public let maxRealOrderEur: String
    public let ceilingEur: String
    public let realTrading: Bool
}

/// De dónde salen las velas que analiza Kronos: IBKR o Alpaca (tiempo real IEX gratuito).
public struct DataProvider: Codable, Hashable, Sendable {
    public let provider: String
    public let configured: Bool
    public let keyIdMasked: String?
    public let detail: String
}

public struct SimulatedOrderRequest: Encodable, Sendable {
    public let clientOrderId: String
    public let symbol: String
    public let side: String
    public let quantity: String
    public let limitPrice: String
    public let currency: String

    public init(symbol: String, quantity: Decimal, limitPrice: Decimal, currency: String = "USD",
                clientOrderId: String = SimulatedOrderRequest.newClientOrderId()) {
        self.clientOrderId = clientOrderId
        self.symbol = symbol
        self.side = "BUY"
        self.quantity = NSDecimalNumber(decimal: quantity).stringValue
        self.limitPrice = NSDecimalNumber(decimal: limitPrice).stringValue
        self.currency = currency
    }

    /// Identificador idempotente: reenviar la misma petición no duplica la orden.
    public static func newClientOrderId() -> String {
        let raw = UUID().uuidString.replacingOccurrences(of: "-", with: "")
        return "UI-" + String(raw.prefix(16))
    }
}
