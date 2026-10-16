import SwiftUI
import TradingDeskCore

struct RendimientoView: View {
    @EnvironmentObject var model: AppModel

    private let metrics = [
        "P&L neto y bruto", "Comisiones", "Diferencial estimado", "Deslizamiento estimado",
        "Rentabilidad sobre capital expuesto", "Profit factor", "Win rate", "Beneficio/pérdida media",
        "Máximo drawdown", "Esperanza por operación", "Comparación con estrategia base sin IA",
    ]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                Text("Rendimiento").font(.title2).bold()
                Text(model.isMock
                     ? "Con tres operaciones simuladas la muestra es demasiado pequeña: ninguna métrica es concluyente."
                     : "Cifras de tu cuenta según IBKR. Las métricas por operación necesitan un historial de operaciones "
                       + "que la app aún no registra (ledger y backtesting, Hitos D y E).")
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                if let a = model.account {
                    HStack(spacing: 12) {
                        MetricCard(title: model.isMock ? "Realizado (simulado)" : "Realizado", systemImage: "checkmark.seal") {
                            SignedAmount(money: a.realizedPnl)
                        }
                        MetricCard(title: model.isMock ? "No realizado (simulado)" : "No realizado", systemImage: "hourglass") {
                            SignedAmount(money: a.unrealizedPnl)
                        }
                        MetricCard(title: model.isMock ? "Comisiones (simulado)" : "Comisiones", systemImage: "creditcard") {
                            Text(Fmt.money(a.realizedFees))
                        }
                    }
                }
                GroupBox("Métricas previstas") {
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(metrics, id: \.self) { metric in
                            HStack {
                                Text(metric)
                                Spacer()
                                Text("No concluyente").foregroundStyle(.secondary)
                            }
                        }
                    }
                }
            }
            .padding(16)
        }
    }
}
