import SwiftUI
import TradingDeskCore

struct ConfiguracionView: View {
    @EnvironmentObject var model: AppModel
    @State private var capText = ""
    @State private var capMessage: String?

    var body: some View {
        Form {
            SwiftUI.Section("Servicio local") {
                LabeledContent("Dirección", value: model.client.config.baseURL.absoluteString)
                LabeledContent("Token", value: model.client.config.tokenPath.path)
                LabeledContent("Estado", value: model.isConnectedToService ? "Conectado" : (model.serviceError ?? "Conectando…"))
                LabeledContent("Arranque", value: model.supervisor.origin.label)
                if let version = model.status?.serviceVersion {
                    LabeledContent("Versión del servicio", value: version)
                }
                Button("Probar conexión") { Task { await model.refresh() } }
            }
            SwiftUI.Section("Bróker") {
                LabeledContent("Interactive Brokers", value: model.status?.broker.capitalized ?? "—")
                if let detail = model.status?.brokerDetail {
                    Text(detail).font(.caption).foregroundStyle(.secondary)
                }
                Text("La autenticación se hará siempre en IB Gateway oficial. La aplicación nunca pedirá ni guardará tu contraseña ni códigos 2FA.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            SwiftUI.Section("Modo de operativa") {
                LabeledContent("Modo", value: model.isRealTrading ? "Cuenta REAL con órdenes (dinero real)"
                               : model.status?.mode == "live" ? "Cuenta REAL en solo lectura (no se envían órdenes)"
                               : "Paper (cuenta de práctica)")
                if let cap = model.status?.maxOrderValue {
                    LabeledContent("Órdenes", value: "Habilitadas · tope \(Fmt.money(cap)) por orden")
                } else {
                    LabeledContent("Órdenes", value: "No habilitadas")
                }
                if let limit = model.orderLimit {
                    HStack {
                        TextField("Tope por orden real (€)", text: $capText)
                            .frame(width: 160)
                        Button("Guardar tope") {
                            let value = Decimal(string: capText.replacingOccurrences(of: ",", with: "."),
                                                locale: Locale(identifier: "en_US_POSIX"))
                            Task {
                                if let value {
                                    capMessage = await model.saveOrderLimit(value) ?? "Tope guardado: \(capText) € por orden."
                                } else {
                                    capMessage = "Escribe un importe, p. ej. 500."
                                }
                            }
                        }
                    }
                    .onAppear { capText = limit.maxRealOrderEur }
                    Text("Tope actual: \(limit.maxRealOrderEur) € por orden real (máximo permitido: \(limit.ceilingEur) €).")
                        .font(.caption).foregroundStyle(.secondary)
                    if let capMessage { Text(capMessage).font(.caption) }
                }
                Text("Las órdenes son siempre limitadas y solo salen tras tu confirmación.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            SwiftUI.Section("Modelo") {
                if let m = model.modelStatus {
                    LabeledContent("Kronos-small", value: m.summary)
                    if let version = m.version { LabeledContent("Versión", value: version) }
                    if let ms = m.lastLatencyMs { LabeledContent("Última previsión", value: "\(ms) ms") }
                    if let run = m.lastRun { LabeledContent("Último análisis", value: Fmt.dateTime(run)) }
                    LabeledContent("Acciones analizadas", value: m.watchlist.joined(separator: ", "))
                    Text(m.detail).font(.caption).foregroundStyle(.secondary)
                } else {
                    LabeledContent("Kronos-small", value: "Sin datos del servicio")
                }
            }
            SwiftUI.Section("Detener operativa") {
                LabeledContent("Estado", value: model.killSwitch?.active == true ? "Detenida" : "Habilitada (simulación)")
                if let message = model.killSwitch?.message {
                    Text(message).font(.caption).foregroundStyle(.secondary)
                }
            }
        }
        .formStyle(.grouped)
        .padding(8)
    }
}
