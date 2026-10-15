import SwiftUI
import TradingDeskCore

struct OperacionesView: View {
    enum Tab: String, CaseIterable, Identifiable {
        case abiertas = "Abiertas"
        case ordenes = "Órdenes"
        case historial = "Historial"
        var id: String { rawValue }
    }

    @EnvironmentObject var model: AppModel
    @State private var tab: Tab = .ordenes
    @State private var selection: OrderRecord.ID?
    @State private var events: [OrderEvent] = []

    private var rows: [OrderRecord] {
        switch tab {
        case .abiertas: return model.orders.filter { !$0.state.isTerminal }
        case .ordenes: return model.orders
        case .historial: return model.orders.filter { $0.state.isTerminal }
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Picker("Vista", selection: $tab) {
                ForEach(Tab.allCases) { Text($0.rawValue).tag($0) }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .frame(maxWidth: 360)

            Table(rows, selection: $selection) {
                TableColumn("ID interno") { o in Text(o.clientOrderId).font(.system(.body, design: .monospaced)) }
                TableColumn("Origen") { o in Text(o.isMock ? "SIMULADA" : o.mode.uppercased()) }
                TableColumn("Símbolo") { o in Text(o.symbol) }
                TableColumn("Lado") { o in Text(o.side == "BUY" ? "Compra" : "Venta") }
                TableColumn("Unidades") { o in Text("\(Fmt.number(o.filledQuantity.value, fractionDigits: 0))/\(Fmt.number(o.quantity.value, fractionDigits: 0))") }
                TableColumn("Límite") { o in Text(Fmt.money(o.limitPrice.value, currency: o.currency)) }
                TableColumn("Precio medio") { o in Text(o.avgFillPrice.map { Fmt.money($0.value, currency: o.currency) } ?? "—") }
                TableColumn("Comisión") { o in Text(o.commission.map { Fmt.money($0) } ?? "Pendiente de confirmación") }
                TableColumn("Estado") { o in Text(o.state.label) }
                TableColumn("IBKR") { o in Text(o.ibkrOrderId.map { String($0) } ?? "—") }
                TableColumn("Creada") { o in Text(Fmt.dateTime(o.createdAt)) }
            }
            .onChange(of: selection) { _, newValue in
                Task { await loadEvents(newValue) }
            }

            if let id = selection, let order = model.orders.first(where: { $0.id == id }) {
                GroupBox("Cronología de \(order.clientOrderId)") {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(Array(events.enumerated()), id: \.offset) { _, event in
                            HStack {
                                Text(Fmt.dateTime(event.at)).font(.caption).monospacedDigit()
                                Text("\(event.fromState?.label ?? "—") → \(event.toState.label)").bold()
                                Text(event.reason).foregroundStyle(.secondary)
                            }
                        }
                        if [.draft, .riskApproved].contains(order.state) {
                            Button("Cancelar borrador") { Task { await model.cancel(order) } }
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
        .padding(16)
    }

    private func loadEvents(_ id: String?) async {
        guard let id else { events = []; return }
        events = (try? await model.client.orderEvents(id).data) ?? []
    }
}
