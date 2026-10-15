import SwiftUI
import TradingDeskCore

struct ConfiguracionView: View {
    @EnvironmentObject var model: AppModel

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
                LabeledContent("Modo", value: "Paper (cuenta de práctica)")
                if let cap = model.status?.maxOrderValue {
                    LabeledContent("Órdenes Paper", value: "Habilitadas · tope \(Fmt.money(cap)) por orden")
                } else {
                    LabeledContent("Órdenes Paper", value: "No habilitadas")
                }
                Text("El modo real está bloqueado en el servicio: no existe proceso de habilitación en esta versión, y cambiar la interfaz o un archivo local no lo activa.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            SwiftUI.Section("Modelo") {
                LabeledContent("Kronos-small", value: "No instalado (Hito C)")
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
