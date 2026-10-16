import Foundation
import SwiftUI
import TradingDeskCore

enum AppSection: String, CaseIterable, Identifiable {
    case inicio = "Inicio"
    case oportunidades = "Oportunidades"
    case operaciones = "Operaciones"
    case rendimiento = "Rendimiento"
    case configuracion = "Configuración"

    var id: String { rawValue }

    var symbol: String {
        switch self {
        case .inicio: return "house"
        case .oportunidades: return "sparkles"
        case .operaciones: return "list.bullet.rectangle"
        case .rendimiento: return "chart.xyaxis.line"
        case .configuracion: return "gearshape"
        }
    }
}

@MainActor
final class AppModel: ObservableObject {
    @Published var section: AppSection? = .inicio
    @Published private(set) var status: SystemStatus?
    @Published private(set) var account: AccountSnapshot?
    @Published private(set) var positions: [Position] = []
    @Published private(set) var signals: [Signal] = []
    @Published private(set) var orders: [OrderRecord] = []
    @Published private(set) var curve: EquityCurve?
    @Published private(set) var killSwitch: KillSwitchState?
    @Published private(set) var modelStatus: ModelStatus?
    @Published var curveRange: String = "30d"
    /// Verdadero si alguna de las respuestas mostradas está marcada como simulada por el servicio.
    @Published private(set) var isMock = true
    /// Motivo por el que no hay saldo ni posiciones de IBKR (p. ej., IB Gateway cerrado).
    @Published private(set) var accountUnavailable: String?
    @Published private(set) var notice: String?
    @Published private(set) var serviceError: String?
    @Published var actionMessage: String?
    @Published private(set) var lastRefresh: Date?

    let client: APIClient
    let supervisor: ServiceSupervisor
    private var refreshTask: Task<Void, Never>?

    init(client: APIClient = APIClient()) {
        self.client = client
        self.supervisor = ServiceSupervisor(config: client.config)
    }

    var isConnectedToService: Bool { serviceError == nil && status != nil }

    func start() {
        guard refreshTask == nil else { return }
        refreshTask = Task { [weak self] in
            while !Task.isCancelled {
                // Arranca (o rearranca) el servicio local si no responde.
                if let self, self.serviceError != nil || self.status == nil {
                    await self.supervisor.ensureRunning()
                }
                await self?.refresh()
                try? await Task.sleep(nanoseconds: 15 * 1_000_000_000)
            }
        }
    }

    func refresh() async {
        do {
            async let statusEnv = client.status()
            async let signalsEnv = client.signals()
            async let ordersEnv = client.orders()
            async let killEnv = client.killSwitch()
            async let curveEnv = client.equityCurve(range: curveRange)
            let (s, sg, o, k, c) = try await (statusEnv, signalsEnv, ordersEnv, killEnv, curveEnv)
            var flags = [s.isMock, sg.isMock, o.isMock, k.isMock, c.isMock]
            // Con IBKR, el servicio puede estar activo sin IB Gateway: entonces no hay saldo que mostrar,
            // pero el resto de la app sigue funcionando.
            do {
                async let accountEnv = client.account()
                async let positionsEnv = client.positions()
                let (a, p) = try await (accountEnv, positionsEnv)
                account = a.data
                positions = p.data
                flags += [a.isMock, p.isMock]
                accountUnavailable = nil
                let cuenta = s.data.mode == "live" ? "tu cuenta REAL de IBKR (solo lectura)" : "cuenta Paper de IBKR"
                let senales = sg.isMock ? "SIMULADAS" : "Kronos-small"
                notice = a.isMock ? a.notice
                    : "Saldo y posiciones: \(cuenta). Señales: \(senales). Curva de patrimonio: DATOS SIMULADOS."
            } catch let error as APIError {
                guard case .http(503, let detail) = error else { throw error }
                account = nil
                positions = []
                accountUnavailable = detail
                notice = sg.notice
            }
            status = s.data
            signals = sg.data
            orders = o.data
            killSwitch = k.data
            curve = c.data
            // El estado de Kronos es informativo: si falla, no se tumba el resto de la pantalla.
            modelStatus = try? await client.modelStatus()
            isMock = flags.contains(true)
            serviceError = nil
            lastRefresh = Date()
        } catch {
            // INICIO-01: sin servicio no se muestran cifras antiguas como si fueran actuales.
            serviceError = error.localizedDescription
            status = nil
            account = nil
        }
    }

    func reloadCurve() async {
        do {
            curve = try await client.equityCurve(range: curveRange).data
        } catch {
            curve = nil
        }
    }

    func toggleKillSwitch() async {
        let activate = !(killSwitch?.active ?? false)
        do {
            killSwitch = try await client.setKillSwitch(
                active: activate,
                reason: activate ? "Detenida manualmente desde la aplicación" : nil
            ).data
            actionMessage = killSwitch?.message
            await refresh()
        } catch {
            actionMessage = error.localizedDescription
        }
    }

    func simulateBuy(_ signal: Signal) async {
        guard let price = signal.price?.value else {
            actionMessage = "No evaluable: no hay precio para simular la compra."
            return
        }
        do {
            let order = try await client.createSimulatedOrder(
                SimulatedOrderRequest(symbol: signal.symbol, quantity: 1, limitPrice: price)
            ).data
            actionMessage = "Borrador SIMULADO creado (\(order.clientOrderId)). No se ha enviado a ningún bróker."
            await refresh()
        } catch {
            actionMessage = error.localizedDescription
        }
    }

    /// Borrador para la hoja de nueva orden Paper (nil = cerrada).
    @Published var paperOrderDraft: PaperOrderDraft?

    var canSendPaperOrders: Bool { status?.tradingAllowed == true && status?.maxOrderValue != nil }

    func openPaperOrder(symbol: String = "", side: String = "BUY", price: Decimal? = nil) {
        paperOrderDraft = PaperOrderDraft(symbol: symbol, side: side, quantity: 1, limitPrice: price)
    }

    /// Envía la orden ya confirmada por el usuario. Devuelve nil si todo fue bien, o el motivo del rechazo.
    func sendPaperOrder(_ draft: PaperOrderDraft) async -> String? {
        guard let price = draft.limitPrice else { return "Indica un precio límite." }
        do {
            let order = try await client.createPaperOrder(PaperOrderRequest(
                symbol: draft.symbol, side: draft.side, quantity: draft.quantity, limitPrice: price,
                confirmed: true, clientOrderId: draft.clientOrderId
            )).data
            actionMessage = "Orden Paper \(order.clientOrderId) enviada a IBKR: \(order.state.label). Dinero ficticio."
            paperOrderDraft = nil
            await refresh()
            return nil
        } catch {
            return error.localizedDescription
        }
    }

    func cancel(_ order: OrderRecord) async {
        do {
            _ = try await client.cancelOrder(order.clientOrderId)
            actionMessage = order.isMock ? "Orden \(order.clientOrderId) cancelada."
                : "Cancelación de \(order.clientOrderId) pedida a IBKR. El estado se actualizará cuando IBKR la confirme."
            await refresh()
        } catch {
            actionMessage = error.localizedDescription
        }
    }
}

struct PaperOrderDraft: Identifiable, Equatable {
    let id = UUID()
    /// Fijo para toda la vida de la hoja: reintentar el envío no duplica la orden.
    let clientOrderId = SimulatedOrderRequest.newClientOrderId()
    var symbol: String
    var side: String
    var quantity: Int
    var limitPrice: Decimal?
}
