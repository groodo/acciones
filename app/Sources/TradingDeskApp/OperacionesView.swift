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

            OrdersTable(rows: rows, selection: $selection)
            .onChange(of: selection) { _, newValue in
                Task { await loadEvents(newValue) }
            }

            if let id = selection, let order = model.orders.first(where: { $0.id == id }) {
                OrderTimeline(order: order, events: events)
            }
        }
        .padding(16)
    }

    private func loadEvents(_ id: String?) async {
        guard let id else { events = []; return }
        events = (try? await model.client.orderEvents(id).data) ?? []
    }
}

/// SwiftUI admite como máximo 10 columnas por tabla.
struct OrdersTable: View {
    let rows: [OrderRecord]
    @Binding var selection: OrderRecord.ID?

    var body: some View {
        Table(rows, selection: $selection) {
            TableColumn("ID interno / IBKR") { (o: OrderRecord) in
                VStack(alignment: .leading) {
                    Text(o.clientOrderId).font(.system(.body, design: .monospaced))
                    Text("IBKR: " + (o.ibkrOrderId.map { String($0) } ?? "—")).font(.caption).foregroundStyle(.secondary)
                }
            }
            TableColumn("Origen") { (o: OrderRecord) in Text(o.isMock ? "SIMULADA" : o.mode.uppercased()) }
            TableColumn("Símbolo") { (o: OrderRecord) in Text(o.symbol) }
            TableColumn("Lado") { (o: OrderRecord) in Text(o.side == "BUY" ? "Compra" : "Venta") }
            TableColumn("Unidades") { (o: OrderRecord) in Text(Self.units(o)) }
            TableColumn("Límite") { (o: OrderRecord) in Text(Fmt.money(o.limitPrice.value, currency: o.currency)) }
            TableColumn("Precio medio") { (o: OrderRecord) in Text(Self.avgPrice(o)) }
            TableColumn("Comisión") { (o: OrderRecord) in Text(o.commission.map { Fmt.money($0) } ?? "Pendiente de confirmación") }
            TableColumn("Estado") { (o: OrderRecord) in Text(o.state.label) }
            TableColumn("Creada") { (o: OrderRecord) in Text(Fmt.dateTime(o.createdAt)) }
        }
    }

    static func units(_ o: OrderRecord) -> String {
        let filled = Fmt.number(o.filledQuantity.value, fractionDigits: 0)
        let total = Fmt.number(o.quantity.value, fractionDigits: 0)
        return filled + "/" + total
    }

    static func avgPrice(_ o: OrderRecord) -> String {
        guard let price = o.avgFillPrice else { return "—" }
        return Fmt.money(price.value, currency: o.currency)
    }
}

struct OrderTimeline: View {
    @EnvironmentObject var model: AppModel
    let order: OrderRecord
    let events: [OrderEvent]

    var body: some View {
        GroupBox("Cronología de \(order.clientOrderId)") {
            VStack(alignment: .leading, spacing: 4) {
                ForEach(Array(events.enumerated()), id: \.offset) { _, event in
                    HStack {
                        Text(Fmt.dateTime(event.at)).font(.caption).monospacedDigit()
                        Text((event.fromState?.label ?? "—") + " → " + event.toState.label).bold()
                        Text(event.reason).foregroundStyle(.secondary)
                    }
                }
                if order.state == .draft || order.state == .riskApproved {
                    Button("Cancelar borrador") { Task { await model.cancel(order) } }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}
