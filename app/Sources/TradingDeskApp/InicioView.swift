import Charts
import SwiftUI
import TradingDeskCore

struct InicioView: View {
    @EnvironmentObject var model: AppModel

    private let columns = [GridItem(.adaptive(minimum: 260), spacing: 12)]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                if let account = model.account {
                    LazyVGrid(columns: columns, spacing: 12) {
                        cards(account)
                    }
                } else if let reason = model.accountUnavailable {
                    Label("Saldo no disponible. \(reason)", systemImage: "exclamationmark.triangle")
                        .padding(12)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
                }
                EquityChartCard()
                PositionsCard()
                RecentActivityCard()
            }
            .padding(16)
        }
    }

    @ViewBuilder
    private func cards(_ a: AccountSnapshot) -> some View {
        MetricCard(title: "Valor total de la cuenta", systemImage: "eurosign.circle",
                   footnote: "Net Liquidation · \(a.baseCurrency) · cuenta \(a.accountIdMasked) · \(Fmt.dateTime(a.timestamp))") {
            Text(Fmt.money(a.netLiquidationEur)).font(.system(size: 28, weight: .semibold)).monospacedDigit()
            if let fx = a.fx {
                Text("Tipo \(fx.pair) \(Fmt.number(fx.rate.value, fractionDigits: 4)) · fuente: \(fx.source)")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        MetricCard(title: "Ganancia/pérdida del día", systemImage: "calendar", footnote: a.dailyPnlBasis) {
            SignedAmount(money: a.dailyPnl).font(.title2)
            Text(Fmt.signedPercent(a.dailyPnlPct.value)).monospacedDigit()
        }
        MetricCard(title: "Resultado realizado", systemImage: "checkmark.seal",
                   footnote: "Posiciones cerradas del periodo. Comisiones aparte: \(Fmt.money(a.realizedFees))") {
            SignedAmount(money: a.realizedPnl).font(.title2)
        }
        MetricCard(title: "Resultado no realizado", systemImage: "hourglass",
                   footnote: "Posiciones abiertas valoradas al último precio disponible") {
            SignedAmount(money: a.unrealizedPnl).font(.title2)
        }
        MetricCard(title: "Efectivo", systemImage: "banknote",
                   footnote: "El poder de compra lo comunica el bróker y no equivale siempre al efectivo liquidado.") {
            ForEach(a.settledCash, id: \.currency) { cash in
                Text("Liquidado \(cash.currency): \(Fmt.money(cash))").monospacedDigit()
            }
            Text("Disponible: \(Fmt.money(a.availableFunds))").monospacedDigit()
            Text("Poder de compra: \(Fmt.money(a.buyingPower))").monospacedDigit()
        }
        MetricCard(title: "Capital y riesgo", systemImage: "shield.lefthalf.filled",
                   footnote: "El riesgo presupuestado no equivale a la pérdida máxima posible.") {
            Text("Invertido: \(Fmt.money(a.investedCapital))").monospacedDigit()
            Text("Exposición: \(Fmt.money(a.exposure))").monospacedDigit()
            Text("Riesgo presupuestado: \(Fmt.money(a.riskBudget))").monospacedDigit()
        }
    }
}

struct EquityChartCard: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        MetricCard(title: "Patrimonio (EUR)", systemImage: "chart.line.uptrend.xyaxis",
                   footnote: "Las aportaciones y retiradas se registran aparte y no cuentan como rentabilidad.") {
            Picker("Periodo", selection: $model.curveRange) {
                Text("Hoy").tag("hoy")
                Text("7 días").tag("7d")
                Text("30 días").tag("30d")
                Text("Todo").tag("todo")
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .frame(maxWidth: 360)
            .onChange(of: model.curveRange) { _, _ in
                Task { await model.reloadCurve() }
            }

            if let curve = model.curve, curve.sufficient, !curve.points.isEmpty {
                Chart(curve.points, id: \.timestamp) { point in
                    LineMark(x: .value("Fecha", point.timestamp),
                             y: .value("EUR", NSDecimalNumber(decimal: point.equityEur.value).doubleValue))
                    if point.netFlowsEur.value != 0 {
                        PointMark(x: .value("Fecha", point.timestamp),
                                  y: .value("EUR", NSDecimalNumber(decimal: point.equityEur.value).doubleValue))
                            .symbol(.diamond)
                            .annotation(position: .top) { Text("Aportación").font(.caption2) }
                    }
                }
                .chartYScale(domain: .automatic(includesZero: false))
                .frame(height: 220)
            } else {
                // Nunca se dibuja una curva artificial.
                Text(model.curve?.message ?? "Datos insuficientes")
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, minHeight: 120)
            }
        }
    }
}

struct PositionsCard: View {
    @EnvironmentObject var model: AppModel
    @State private var sortOrder = [KeyPathComparator(\Position.symbol)]

    var body: some View {
        MetricCard(title: "Posiciones abiertas", systemImage: "briefcase") {
            if model.positions.isEmpty {
                Text("No hay posiciones abiertas.").foregroundStyle(.secondary)
            } else {
                Table(model.positions.sorted(using: sortOrder), sortOrder: $sortOrder) {
                    TableColumn("Símbolo", value: \.symbol) { p in
                        VStack(alignment: .leading) {
                            Text(p.symbol).bold()
                            Text(p.name).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    TableColumn("Cantidad") { p in Text(Fmt.number(p.quantity.value, fractionDigits: 0)) }
                    TableColumn("Precio medio") { p in Text(Fmt.money(p.avgPrice.value, currency: p.currency)) }
                    TableColumn("Precio actual") { p in Text(Fmt.money(p.lastPrice.value, currency: p.currency)) }
                    TableColumn("No realizado") { p in SignedAmount(money: p.unrealizedPnl, showDirection: false) }
                    TableColumn("Variación") { p in Text(p.changePct.map { Fmt.signedPercent($0.value) } ?? "—") }
                    TableColumn("Stop / objetivo") { p in
                        Text("\(p.stop.map { Fmt.number($0.value) } ?? "—") / \(p.target.map { Fmt.number($0.value) } ?? "—")")
                    }
                    TableColumn("Comisión est.") { p in Text(p.estimatedFee.map { Fmt.money($0) } ?? "Pendiente de confirmación") }
                    TableColumn("") { p in
                        Button("Proteger…") { model.openProtect(p) }
                            .disabled(!model.canSendPaperOrders || p.stop != nil)
                            .help(p.stop != nil ? "Ya tiene venta de protección en IBKR."
                                  : "Venta automática al llegar a tu ganancia máxima o a tu pérdida máxima.")
                    }
                }
                .frame(minHeight: 140)
            }
        }
    }
}

struct RecentActivityCard: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        MetricCard(title: "Actividad reciente", systemImage: "clock.arrow.circlepath") {
            ForEach(model.orders.prefix(5)) { order in
                HStack {
                    if order.isMock {
                        Text("SIMULADA").font(.caption2).bold().padding(3).background(Color.yellow.opacity(0.4))
                    }
                    Text("\(order.side == "BUY" ? "Compra" : "Venta") \(Fmt.number(order.quantity.value, fractionDigits: 0)) \(order.symbol)")
                    Spacer()
                    Text(order.state.label).foregroundStyle(.secondary)
                    Text(Fmt.dateTime(order.updatedAt)).font(.caption).foregroundStyle(.secondary)
                }
            }
            if model.orders.isEmpty {
                Text("Sin actividad.").foregroundStyle(.secondary)
            }
        }
    }
}
