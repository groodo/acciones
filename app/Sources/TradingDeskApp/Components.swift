import SwiftUI
import TradingDeskCore

/// Cabecera persistente (PRD §4.1): conexión, modo, mercado, cotizaciones, última actualización y botón de parada.
struct HeaderBar: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        HStack(spacing: 10) {
            if let status = model.status {
                StatusPill(title: "IBKR", value: status.broker.capitalized,
                           systemImage: status.broker == "conectado" ? "link" : "link.badge.plus",
                           tone: status.broker == "conectado" ? .ok : .warning, help: status.brokerDetail)
                StatusPill(title: "Modo", value: status.modeLabel, systemImage: "testtube.2", tone: .info,
                           help: "Paper Trading: no se usa dinero real")
                StatusPill(title: "Mercado", value: status.market.capitalized,
                           systemImage: status.market == "abierto" ? "bell" : "bell.slash",
                           tone: .neutral, help: status.marketDetail)
                StatusPill(title: "Cotizaciones", value: status.quotes.label, systemImage: "waveform.path.ecg",
                           tone: status.quotes == .realTime ? .ok : .warning, help: nil)
                Text("Actualizado: \(Fmt.dateTime(status.lastUpdate))")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                StatusPill(title: "Servicio local", value: "Sin conexión", systemImage: "bolt.horizontal.circle",
                           tone: .warning, help: model.serviceError)
            }
            Spacer()
            Button {
                Task { await model.toggleKillSwitch() }
            } label: {
                if model.killSwitch?.active == true {
                    Label("Reanudar operativa", systemImage: "play.circle")
                } else {
                    Label("Detener operativa", systemImage: "stop.circle.fill")
                }
            }
            .buttonStyle(.borderedProminent)
            .tint(model.killSwitch?.active == true ? .gray : .red)
            .disabled(!model.isConnectedToService)
            .help("Bloquea nuevas entradas de inmediato. No liquida posiciones por sí mismo.")
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
    }
}

enum Tone {
    case ok, warning, info, neutral, danger

    var color: Color {
        switch self {
        case .ok: return .green
        case .warning: return .orange
        case .info: return .blue
        case .neutral: return .secondary
        case .danger: return .red
        }
    }
}

struct StatusPill: View {
    let title: String
    let value: String
    let systemImage: String
    let tone: Tone
    let help: String?

    var body: some View {
        Label {
            Text("\(title): ").foregroundColor(.secondary) + Text(value).bold()
        } icon: {
            Image(systemName: systemImage).foregroundStyle(tone.color)
        }
        .font(.callout)
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(tone.color.opacity(0.12), in: Capsule())
        .help(help ?? "")
    }
}

/// Distintivo persistente: todo lo que se ve son datos simulados.
struct MockBanner: View {
    let notice: String?

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "exclamationmark.triangle.fill")
            Text(notice ?? "DATOS SIMULADOS")
                .bold()
            Spacer()
        }
        .font(.callout)
        .padding(.horizontal, 16)
        .padding(.vertical, 6)
        .foregroundStyle(.black)
        .background(Color.yellow)
        .accessibilityLabel("Aviso: datos simulados")
    }
}

struct ActionMessageBar: View {
    let message: String
    let dismiss: () -> Void

    var body: some View {
        HStack {
            Image(systemName: "info.circle")
            Text(message)
            Spacer()
            Button("Cerrar", action: dismiss).buttonStyle(.borderless)
        }
        .font(.callout)
        .padding(.horizontal, 16)
        .padding(.vertical, 6)
        .background(Color.blue.opacity(0.12))
    }
}

struct ServiceUnavailableView: View {
    @EnvironmentObject var model: AppModel
    let message: String

    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: "bolt.horizontal.circle")
                .font(.system(size: 44))
                .foregroundStyle(.orange)
            Text("Servicio local no disponible").font(.title2).bold()
            Text(message)
                .multilineTextAlignment(.center)
                .foregroundStyle(.secondary)
                .frame(maxWidth: 520)
            Text("No se muestran saldos: sin servicio no hay datos actuales.")
                .font(.callout)
            Button("Reintentar") {
                Task {
                    await model.supervisor.ensureRunning()
                    await model.refresh()
                }
            }
        }
        .padding()
    }
}

/// Importe con signo, texto y color redundantes (nunca solo color).
struct SignedAmount: View {
    let money: Money
    var showDirection = true

    var body: some View {
        let value = money.amount.value
        HStack(spacing: 4) {
            Image(systemName: value > 0 ? "arrow.up.right" : (value < 0 ? "arrow.down.right" : "equal"))
            Text(Fmt.signedMoney(money)).monospacedDigit()
            if showDirection {
                Text("(\(Fmt.direction(value)))").font(.caption).foregroundStyle(.secondary)
            }
        }
        .foregroundStyle(value > 0 ? Color.green : (value < 0 ? Color.red : Color.primary))
        .accessibilityElement(children: .combine)
    }
}

struct MetricCard<Content: View>: View {
    let title: String
    let systemImage: String
    var footnote: String?
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(title, systemImage: systemImage)
                .font(.headline)
                .foregroundStyle(.secondary)
            content
            if let footnote {
                Text(footnote)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .background(.background.secondary, in: RoundedRectangle(cornerRadius: 10))
    }
}
