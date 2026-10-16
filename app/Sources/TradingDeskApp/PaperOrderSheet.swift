import SwiftUI
import TradingDeskCore

/// Hoja para preparar una orden limitada en IBKR (cuenta Paper o, si está habilitada, la cuenta real).
/// Nada se envía hasta que el usuario revisa el resumen y lo confirma de forma explícita.
struct PaperOrderSheet: View {
    @EnvironmentObject var model: AppModel
    @State var draft: PaperOrderDraft
    @State private var priceText: String = ""
    @State private var confirming = false
    @State private var sending = false
    @State private var error: String?

    private var price: Decimal? {
        Decimal(string: priceText.replacingOccurrences(of: ",", with: "."), locale: Locale(identifier: "en_US_POSIX"))
    }

    private var total: Decimal? { price.map { $0 * Decimal(draft.quantity) } }

    private var cap: Money? { model.status?.maxOrderValue }

    private var problem: String? {
        if draft.symbol.trimmingCharacters(in: .whitespaces).isEmpty { return "Escribe el símbolo de la acción." }
        guard let price, price > 0 else { return "Indica un precio límite mayor que cero." }
        if let cap, cap.currency == "USD", let total, total > cap.amount.value {
            return "El importe supera el tope de \(Fmt.money(cap)) por orden."
        }
        if draft.side == "SELL" {
            let held = model.positions.first { $0.symbol == draft.symbol.uppercased() }?.quantity.value ?? 0
            if Decimal(draft.quantity) > held { return "Solo puedes vender acciones que tengas (tienes \(held))." }
        }
        return nil
    }

    private var protection: Protection? { price.flatMap { draft.protection(entry: $0) } }

    private var protectionProblem: String? {
        guard draft.side == "BUY", draft.protect else { return nil }
        if draft.gainPct <= 0 || draft.gainPct > 50 { return "La ganancia máxima debe estar entre 0 y 50 %." }
        if draft.lossPct <= 0 || draft.lossPct > 50 { return "La pérdida máxima debe estar entre 0 y 50 %." }
        return nil
    }

    private var protectionText: String {
        guard let protection, let price else { return "" }
        return " Al ejecutarse, venta automática a \(Fmt.money(protection.takeProfit, currency: "USD")) "
            + "(+\(Fmt.money(protection.maxGain(entry: price, quantity: draft.quantity), currency: "USD"))) o stop a "
            + "\(Fmt.money(protection.stopLoss, currency: "USD")) (−\(Fmt.money(protection.maxLoss(entry: price, quantity: draft.quantity), currency: "USD")))."
    }

    private var sideLabel: String { draft.side == "BUY" ? "COMPRAR" : "VENDER" }
    private var real: Bool { model.isRealTrading }
    private var accountText: String { real ? "CUENTA REAL: dinero real." : "Cuenta Paper: dinero ficticio." }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            if real {
                Label("Nueva orden REAL", systemImage: "exclamationmark.octagon.fill").font(.title2).bold()
                    .foregroundStyle(.red)
                Text("Tu cuenta real de IBKR: esta orden usa dinero real y no se puede deshacer una vez ejecutada. "
                     + "Orden limitada válida durante la sesión de hoy.")
                    .font(.callout).foregroundStyle(.red)
            } else {
                Label("Nueva orden en la cuenta Paper", systemImage: "testtube.2").font(.title2).bold()
                Text("Cuenta de práctica de IBKR: dinero ficticio. Orden limitada válida durante la sesión de hoy.")
                    .font(.callout).foregroundStyle(.secondary)
            }

            Form {
                Picker("Operación", selection: $draft.side) {
                    Text("Comprar").tag("BUY")
                    Text("Vender").tag("SELL")
                }
                .pickerStyle(.segmented)
                TextField("Símbolo (p. ej. AAPL)", text: $draft.symbol)
                    .onChange(of: draft.symbol) { _, v in draft.symbol = v.uppercased() }
                Stepper("Acciones: \(draft.quantity)", value: $draft.quantity, in: 1...10_000)
                TextField("Precio límite (USD)", text: $priceText)
                LabeledContent("Importe máximo", value: total.map { Fmt.money($0, currency: "USD") } ?? "—")
                if let cap {
                    LabeledContent("Tope por orden", value: Fmt.money(cap))
                }
                if draft.side == "BUY" {
                    Toggle("Venta automática con ganancia y pérdida máximas", isOn: $draft.protect)
                    if draft.protect {
                        TextField("Ganancia máxima (%)", value: $draft.gainPct, format: .number)
                        TextField("Pérdida máxima (%)", value: $draft.lossPct, format: .number)
                        if let protection, let price {
                            LabeledContent("Vende con ganancia a",
                                           value: "\(Fmt.money(protection.takeProfit, currency: "USD")) · +\(Fmt.money(protection.maxGain(entry: price, quantity: draft.quantity), currency: "USD"))")
                            LabeledContent("Stop de pérdida a",
                                           value: "\(Fmt.money(protection.stopLoss, currency: "USD")) · −\(Fmt.money(protection.maxLoss(entry: price, quantity: draft.quantity), currency: "USD"))")
                        }
                    }
                }
            }
            .formStyle(.grouped)

            Text("El precio límite debe estar a menos de un 5 % del precio actual según IBKR. Si el mercado está "
                 + "cerrado, la orden espera a la apertura.")
                .font(.caption).foregroundStyle(.secondary)

            if draft.side == "BUY" && draft.protect {
                Text("Las dos ventas quedan en IBKR (aunque cierres la app) hasta que se ejecute una; entonces IBKR cancela "
                     + "la otra. El stop vende a mercado: en una caída brusca puede vender algo por debajo. Solo actúan en "
                     + "horario normal.")
                    .font(.caption).foregroundStyle(.secondary)
            }

            if let message = error ?? problem ?? protectionProblem {
                Label(message, systemImage: "exclamationmark.triangle").foregroundStyle(.orange)
            }

            HStack {
                Button("Cancelar", role: .cancel) { model.paperOrderDraft = nil }
                    .keyboardShortcut(.cancelAction)
                Spacer()
                if sending { ProgressView().controlSize(.small) }
                Button("Revisar y enviar…") { error = nil; confirming = true }
                    .buttonStyle(.borderedProminent)
                    .disabled(problem != nil || protectionProblem != nil || sending || !model.canSendPaperOrders)
            }
        }
        .padding(20)
        .frame(width: 460)
        .onAppear {
            if let p = draft.limitPrice { priceText = NSDecimalNumber(decimal: p).stringValue }
        }
        .confirmationDialog(real ? "¿Enviar esta orden REAL a IBKR?" : "¿Enviar esta orden a IBKR Paper?",
                            isPresented: $confirming, titleVisibility: .visible) {
            Button(real ? "Enviar orden REAL" : "Enviar orden Paper", role: real ? .destructive : nil) {
                Task {
                    sending = true
                    var final = draft
                    final.limitPrice = price
                    error = await model.sendPaperOrder(final)
                    sending = false
                }
            }
            Button("Volver", role: .cancel) {}
        } message: {
            Text("\(sideLabel) \(draft.quantity) \(draft.symbol) con límite \(price.map { Fmt.money($0, currency: "USD") } ?? "—"). "
                 + "Importe máximo \(total.map { Fmt.money($0, currency: "USD") } ?? "—").\(protectionText) \(accountText)")
        }
    }
}
