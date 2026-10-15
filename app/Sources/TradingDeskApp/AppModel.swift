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
    @Published var curveRange: String = "30d"
    /// Todas las respuestas actuales vienen marcadas como simuladas por el servicio.
    @Published private(set) var isMock = true
    @Published private(set) var notice: String?
    @Published private(set) var serviceError: String?
    @Published var actionMessage: String?
    @Published private(set) var lastRefresh: Date?

    let client: APIClient
    private var refreshTask: Task<Void, Never>?

    init(client: APIClient = APIClient()) {
        self.client = client
    }

    var isConnectedToService: Bool { serviceError == nil && status != nil }

    func start() {
        guard refreshTask == nil else { return }
        refreshTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                try? await Task.sleep(nanoseconds: 15 * 1_000_000_000)
            }
        }
    }

    func refresh() async {
        do {
            async let statusEnv = client.status()
            async let accountEnv = client.account()
            async let positionsEnv = client.positions()
            async let signalsEnv = client.signals()
            async let ordersEnv = client.orders()
            async let killEnv = client.killSwitch()
            async let curveEnv = client.equityCurve(range: curveRange)
            let (s, a, p, sg, o, k, c) = try await (statusEnv, accountEnv, positionsEnv, signalsEnv, ordersEnv, killEnv, curveEnv)
            status = s.data
            account = a.data
            positions = p.data
            signals = sg.data
            orders = o.data
            killSwitch = k.data
            curve = c.data
            isMock = [s.isMock, a.isMock, p.isMock, sg.isMock, o.isMock, k.isMock, c.isMock].contains(true)
            notice = a.notice
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

    func cancel(_ order: OrderRecord) async {
        do {
            _ = try await client.cancelOrder(order.clientOrderId)
            actionMessage = "Orden \(order.clientOrderId) cancelada."
            await refresh()
        } catch {
            actionMessage = error.localizedDescription
        }
    }
}
