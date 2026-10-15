import AppKit
import SwiftUI
import TradingDeskCore

@main
struct TradingDeskApp: App {
    @StateObject private var model = AppModel()

    init() {
        // Necesario al lanzar con `swift run` (sin bundle .app) para que la ventana tome el foco.
        NSApplication.shared.setActivationPolicy(.regular)
        NSApplication.shared.activate(ignoringOtherApps: true)
    }

    var body: some Scene {
        WindowGroup("AI Trading Desk") {
            RootView()
                .environmentObject(model)
                .frame(minWidth: 1000, minHeight: 680)
                .task { model.start() }
        }
        .commands {
            CommandGroup(after: .toolbar) {
                Button("Actualizar") { Task { await model.refresh() } }
                    .keyboardShortcut("r")
            }
        }
    }
}

struct RootView: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        NavigationSplitView {
            List(selection: $model.section) {
                ForEach(AppSection.allCases) { section in
                    NavigationLink(value: section) {
                        Label(section.rawValue, systemImage: section.symbol)
                    }
                }
            }
            .navigationSplitViewColumnWidth(min: 180, ideal: 200)
        } detail: {
            VStack(spacing: 0) {
                HeaderBar()
                if model.isMock { MockBanner(notice: model.notice) }
                if let message = model.actionMessage {
                    ActionMessageBar(message: message) { model.actionMessage = nil }
                }
                Divider()
                Group {
                    if model.section == .configuracion {
                        ConfiguracionView()
                    } else if let error = model.serviceError {
                        ServiceUnavailableView(message: error)
                    } else if !model.isConnectedToService {
                        ProgressView("Conectando con el servicio local…")
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                    } else {
                        switch model.section ?? .inicio {
                        case .inicio: InicioView()
                        case .oportunidades: OportunidadesView()
                        case .operaciones: OperacionesView()
                        case .rendimiento: RendimientoView()
                        case .configuracion: ConfiguracionView()
                        }
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .sheet(item: $model.paperOrderDraft) { draft in
            PaperOrderSheet(draft: draft).environmentObject(model)
        }
    }
}
