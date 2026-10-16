import SwiftUI
import TradingDeskCore

struct OportunidadesView: View {
    enum Tab: String, CaseIterable, Identifiable {
        case mine = "Mi lista"
        case system = "Recomendadas por el sistema"
        var id: String { rawValue }
    }

    @EnvironmentObject var model: AppModel
    @State private var tab: Tab = .mine
    @State private var selection: Signal.ID?

    private var rows: [Signal] { tab == .mine ? model.signals : model.opportunities }

    var body: some View {
        HSplitView {
            VStack(alignment: .leading, spacing: 8) {
                Picker("Lista", selection: $tab) {
                    ForEach(Tab.allCases) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .onChange(of: tab) { _, _ in selection = nil }

                if tab == .mine {
                    WatchlistEditor()
                    Text(model.signals.contains { $0.modelVersion != nil } || model.modelStatus?.installed == true
                         ? "Opinión de Kronos-small sobre tus valores. No son probabilidades calibradas ni una recomendación."
                         : "Señales de ejemplo: no proceden de ningún modelo.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                } else {
                    Text(model.opportunitiesNotice ?? "El sistema explora el Nasdaq cada 15 minutos.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }

                Table(rows, selection: $selection) {
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
                .overlay {
                    if rows.isEmpty {
                        Text(tab == .mine ? "Añade valores a tu lista para que Kronos los analice."
                             : "Aún no hay oportunidades: la primera exploración tarda unos minutos.")
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .padding(16)
            .frame(minWidth: 560)

            Group {
                if let id = selection, let signal = rows.first(where: { $0.id == id }) {
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

/// Añadir y quitar valores de tu lista de seguimiento (máximo 10).
struct WatchlistEditor: View {
    @EnvironmentObject var model: AppModel
    @State private var newSymbol = ""
    @State private var error: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                ForEach(model.watchlist, id: \.self) { symbol in
                    HStack(spacing: 2) {
                        Text(symbol).bold()
                        Button {
                            save(model.watchlist.filter { $0 != symbol })
                        } label: {
                            Image(systemName: "xmark.circle.fill")
                        }
                        .buttonStyle(.borderless)
                        .help("Quitar \(symbol) de tu lista")
                        .disabled(model.watchlist.count <= 1)
                    }
                    .padding(.horizontal, 6).padding(.vertical, 3)
                    .background(Color.secondary.opacity(0.15), in: Capsule())
                }
                TextField("Añadir (p. ej. TSLA)", text: $newSymbol)
                    .frame(width: 140)
                    .onSubmit(add)
                Button("Añadir", action: add)
                    .disabled(newSymbol.trimmingCharacters(in: .whitespaces).isEmpty || model.watchlist.count >= 10)
            }
            if let error {
                Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(.orange).font(.caption)
            }
        }
    }

    private func add() {
        let symbol = newSymbol.trimmingCharacters(in: .whitespaces).uppercased()
        guard !symbol.isEmpty else { return }
        save(model.watchlist + [symbol])
        newSymbol = ""
    }

    private func save(_ symbols: [String]) {
        Task { error = await model.saveWatchlist(symbols) }
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
                    if model.isMock {
                        Button("Simular compra") { Task { await model.simulateBuy(signal) } }
                            .disabled(signal.status != .paperReady || model.killSwitch?.active == true)
                            .help(signal.status == .paperReady ? "Crea un borrador SIMULADO; no se envía a ningún bróker"
                                                               : "Solo disponible para señales aptas para paper")
                    }
                    Button(model.isRealTrading ? "Preparar orden REAL…" : "Preparar orden Paper…") {
                        model.openPaperOrder(symbol: signal.symbol,
                                             side: signal.proposal == .sellPosition ? "SELL" : "BUY",
                                             price: signal.price?.value)
                    }
                    .disabled(!model.canSendPaperOrders)
                    .help(model.isRealTrading ? "Abre una orden limitada en tu cuenta REAL. Revisa el precio antes de confirmar."
                          : "Abre una orden limitada en la cuenta Paper. Revisa el precio antes de confirmar.")
                    Button("Descartar") { model.actionMessage = "Señal \(signal.symbol) descartada (solo en esta sesión)." }
                }
                // Las órdenes reales solo existen con la cuenta «real-trading» y siempre con confirmación manual.
            }
            .padding(16)
        }
    }
}
