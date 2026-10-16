import SwiftUI
import TradingDeskCore

struct OportunidadesView: View {
    @EnvironmentObject var model: AppModel
    @State private var selection: Signal.ID?

    var body: some View {
        HSplitView {
            VStack(alignment: .leading, spacing: 8) {
                Text(model.signals.contains { $0.modelVersion != nil } || model.modelStatus?.installed == true
                     ? "Previsiones de Kronos-small en tu Mac. No son probabilidades calibradas ni una recomendación."
                     : "Señales de ejemplo: no proceden de ningún modelo.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                Table(model.signals, selection: $selection) {
                    TableColumn("Símbolo") { s in Text(s.symbol).bold() }
                    TableColumn("Precio") { s in Text(s.price.map { Fmt.number($0.value) } ?? "No disponible") }
                    TableColumn("Variación") { s in Text(s.changePct.map { Fmt.signedPercent($0.value) } ?? "—") }
                    TableColumn("Datos") { s in Text(s.freshness.label) }
                    TableColumn("Tendencia") { s in Text(s.trend) }
                    TableColumn("Propuesta") { s in ProposalBadge(proposal: s.proposal) }
                    TableColumn("Horizonte") { s in Text("\(s.horizonMinutes) min") }
                    TableColumn("Neto tras costes") { s in
                        if let net = s.netEvalAfterCosts {
                            SignedAmount(money: net, showDirection: false)
                        } else {
                            Text("No evaluable").foregroundStyle(.secondary)
                        }
                    }
                    TableColumn("Estado") { s in Text(s.status.label) }
                }
            }
            .padding(16)
            .frame(minWidth: 560)

            Group {
                if let id = selection, let signal = model.signals.first(where: { $0.id == id }) {
                    SignalDetail(signal: signal)
                } else {
                    Text("Selecciona una señal para ver su ficha.")
                        .foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            }
            .frame(minWidth: 320, idealWidth: 380)
        }
    }
}

struct ProposalBadge: View {
    let proposal: Proposal

    var body: some View {
        let (symbol, tone): (String, Tone) = {
            switch proposal {
            case .buy: return ("cart.badge.plus", .ok)
            case .sellPosition: return ("arrow.uturn.left.circle", .warning)
            case .wait: return ("pause.circle", .neutral)
            case .notEvaluable: return ("questionmark.circle", .danger)
            }
        }()
        Label(proposal.label, systemImage: symbol)
            .foregroundStyle(tone.color)
    }
}

struct SignalDetail: View {
    @EnvironmentObject var model: AppModel
    let signal: Signal

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    Text(signal.symbol).font(.largeTitle).bold()
                    Text(signal.name).foregroundStyle(.secondary)
                }
                ProposalBadge(proposal: signal.proposal).font(.title3)
                Text("Estado: \(signal.status.label) · horizonte \(signal.horizonMinutes) min · datos \(signal.freshness.label.lowercased())")
                    .font(.callout)
                Text("Modelo: \(signal.modelVersion ?? "ninguno")")
                    .font(.caption).foregroundStyle(.secondary)

                GroupBox("Motivos") {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(signal.reasons, id: \.self) { Label($0, systemImage: "checkmark.circle") }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                if !signal.contradictions.isEmpty {
                    GroupBox("Señales contradictorias") {
                        VStack(alignment: .leading, spacing: 4) {
                            ForEach(signal.contradictions, id: \.self) { Label($0, systemImage: "exclamationmark.triangle") }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                Text("Sin porcentaje de «confianza»: las previsiones no están calibradas y no son una recomendación.")
                    .font(.caption)
                    .foregroundStyle(.secondary)

                HStack {
                    Button("Simular compra") { Task { await model.simulateBuy(signal) } }
                        .disabled(signal.status != .paperReady || model.killSwitch?.active == true)
                        .help(signal.status == .paperReady ? "Crea un borrador SIMULADO; no se envía a ningún bróker"
                                                           : "Solo disponible para señales aptas para paper")
                    Button("Preparar orden Paper…") {
                        model.openPaperOrder(symbol: signal.symbol,
                                             side: signal.proposal == .sellPosition ? "SELL" : "BUY",
                                             price: signal.price?.value)
                    }
                    .disabled(!model.canSendPaperOrders)
                    .help("Abre una orden limitada en la cuenta Paper. La señal es de ejemplo: revisa el precio.")
                    Button("Descartar") { model.actionMessage = "Señal \(signal.symbol) descartada (solo en esta sesión)." }
                }
                // No existe botón de operación REAL: no hay habilitación live en el backend.
            }
            .padding(16)
        }
    }
}
