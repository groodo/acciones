import SwiftUI
import TradingDeskCore

/// Venta automática de ganancia máxima y stop de pérdida máxima para acciones que ya tienes.
/// Las dos órdenes quedan enlazadas en IBKR: si se ejecuta una, IBKR cancela la otra.
struct ProtectSheet: View {
    @EnvironmentObject var model: AppModel
    @State var draft: ProtectDraft
    @State private var confirming = false
    @State private var sending = false
    @State private var error: String?

    private var real: Bool { model.isRealTrading }
    private var protection: Protection { draft.protection }
    private func usd(_ value: Decimal) -> String { Fmt.money(value, currency: draft.currency) }

    private var problem: String? {
        if draft.gainPct <= 0 || draft.gainPct > 50 { return "La ganancia máxima debe estar entre 0 y 50 %." }
        if draft.lossPct <= 0 || draft.lossPct > 50 { return "La pérdida máxima debe estar entre 0 y 50 %." }
        return nil
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Label("Proteger \(draft.symbol)", systemImage: "shield.lefthalf.filled").font(.title2).bold()
                .foregroundStyle(real ? .red : .primary)
            Text(real ? "Cuenta REAL: estas ventas usan tus acciones reales." : "Cuenta Paper: dinero ficticio.")
                .font(.callout).foregroundStyle(real ? .red : .secondary)

            Form {
                Stepper("Acciones: \(draft.quantity) de \(draft.maxQuantity)", value: $draft.quantity,
                        in: 1...max(draft.maxQuantity, 1))
                LabeledContent("Precio actual", value: usd(draft.price))
                TextField("Ganancia máxima (%)", value: $draft.gainPct, format: .number)
                TextField("Pérdida máxima (%)", value: $draft.lossPct, format: .number)
                LabeledContent("Vende con ganancia a",
                               value: "\(usd(protection.takeProfit)) · +\(usd(protection.maxGain(entry: draft.price, quantity: draft.quantity)))")
                LabeledContent("Stop de pérdida a",
                               value: "\(usd(protection.stopLoss)) · −\(usd(protection.maxLoss(entry: draft.price, quantity: draft.quantity)))")
            }
            .formStyle(.grouped)

            Text("Los importes se calculan sobre el precio actual. Las dos ventas quedan en IBKR (aunque cierres la app) "
                 + "hasta que se ejecute una; entonces IBKR cancela la otra. El stop vende a mercado: en una caída brusca "
                 + "puede vender algo por debajo. Solo actúan en horario normal.")
                .font(.caption).foregroundStyle(.secondary)

            if let message = error ?? problem {
                Label(message, systemImage: "exclamationmark.triangle").foregroundStyle(.orange)
            }

            HStack {
                Button("Cancelar", role: .cancel) { model.protectDraft = nil }
                    .keyboardShortcut(.cancelAction)
                Spacer()
                if sending { ProgressView().controlSize(.small) }
                Button("Revisar y enviar…") { error = nil; confirming = true }
                    .buttonStyle(.borderedProminent)
                    .disabled(problem != nil || sending || !model.canSendPaperOrders)
            }
        }
        .padding(20)
        .frame(width: 460)
        .confirmationDialog(real ? "¿Enviar estas ventas REALES a IBKR?" : "¿Enviar estas ventas a IBKR Paper?",
                            isPresented: $confirming, titleVisibility: .visible) {
            Button(real ? "Enviar ventas REALES" : "Enviar ventas Paper", role: real ? .destructive : nil) {
                Task {
                    sending = true
                    error = await model.sendProtect(draft)
                    sending = false
                }
            }
            Button("Volver", role: .cancel) {}
        } message: {
            Text("VENDER \(draft.quantity) \(draft.symbol) a \(usd(protection.takeProfit)) (limitada) o con stop a "
                 + "\(usd(protection.stopLoss)); si se ejecuta una, IBKR cancela la otra.")
        }
    }
}
