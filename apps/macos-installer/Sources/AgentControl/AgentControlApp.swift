import AppKit
import ServiceManagement
import SwiftUI

@main
struct AgentControlApp: App {
    @StateObject private var setup = SetupModel()
    var body: some Scene {
        WindowGroup("Agent Control", id: "setup") {
            SetupView().environmentObject(setup)
                .frame(minWidth: 620, idealWidth: 720, minHeight: 540, idealHeight: 620)
        }
        .windowStyle(.titleBar)
        .defaultSize(width: 720, height: 620)
        .commands { CommandGroup(replacing: .newItem) {} }
        MenuBarExtra("Agent Control", systemImage: setup.ready ? "desktopcomputer" : "desktopcomputer.trianglebadge.exclamationmark") {
            StatusMenu().environmentObject(setup)
        }
    }
}

struct StatusMenu: View {
    @EnvironmentObject var setup: SetupModel
    @Environment(\.openWindow) private var openWindow
    var body: some View {
        Text(setup.ready ? "Equipo conectado" : "Revisar conexión")
        Button("Abrir Agent Control") { NSWorkspace.shared.open(SetupModel.website) }
        Button("Configuración…") { openWindow(id: "setup"); NSApplication.shared.activate(ignoringOtherApps: true) }
        Divider()
        Button("Comprobar estado") { setup.load() }.disabled(setup.busy || setup.needsAppInstall)
        Button("Diagnóstico…") { openWindow(id: "setup"); setup.diagnose() }.disabled(setup.busy || setup.needsAppInstall)
        Button("Reiniciar") { openWindow(id: "setup"); setup.maintenance("restart") }.disabled(setup.busy || !setup.installed)
        Button("Actualizar…") { openWindow(id: "setup"); setup.maintenance("update") }.disabled(setup.busy || !setup.installed || setup.mode != "managed")
        Divider()
        Text("Cerrar esta ventana no detiene tus agentes.").font(.caption)
        Button("Salir de la aplicación") { NSApplication.shared.terminate(nil) }.disabled(setup.busy)
    }
}

struct SetupView: View {
    @EnvironmentObject var setup: SetupModel
    @State private var settingsOpen = false
    @Environment(\.scenePhase) private var scenePhase
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 12) {
                Image(systemName: "desktopcomputer").font(.system(size: 28)).foregroundStyle(.tint)
                VStack(alignment: .leading) {
                    Text("Agent Control").font(.title2.bold())
                    Text("Tus agentes, en tu equipo. Contigo desde cualquier lugar.").font(.subheadline).foregroundStyle(.secondary)
                }
                Spacer()
                Button { settingsOpen = true } label: { Image(systemName: "gearshape") }
                    .help("Configuración y diagnóstico").disabled(setup.needsAppInstall)
            }.padding(24)
            Divider()
            if setup.needsAppInstall {
                installAppPage.padding(28)
            } else {
                HStack {
                    ForEach(SetupPage.allCases, id: \.rawValue) { page in
                        Label(page.title, systemImage: setup.page.rawValue > page.rawValue ? "checkmark.circle.fill" : "\(page.rawValue + 1).circle")
                            .font(.subheadline.weight(setup.page == page ? .bold : .regular))
                            .foregroundStyle(setup.page == page ? Color.accentColor : Color.secondary)
                        if page != .ready { Spacer() }
                    }
                }.padding(.horizontal, 28).padding(.top, 20)
                ScrollView {
                    VStack(alignment: .leading, spacing: 20) {
                        switch setup.page {
                        case .computer: computerPage
                        case .provider: providerPage
                        case .connection: connectionPage
                        case .ready: readyPage
                        }
                    }.frame(maxWidth: .infinity, alignment: .leading).padding(28)
                }
            }
            Spacer(minLength: 0)
            if !setup.error.isEmpty { message(setup.error, icon: "exclamationmark.circle", color: .red) }
            if !setup.notice.isEmpty { message(setup.notice, icon: "info.circle", color: .secondary) }
            if setup.busy {
                HStack { ProgressView().controlSize(.small); Text(setup.progress).font(.callout) }.padding(20)
            }
            Divider()
            HStack {
                Text("Las claves del modelo permanecen en este equipo.").font(.caption).foregroundStyle(.secondary)
                Spacer()
                Link("Ayuda", destination: SetupModel.website.appendingPathComponent("connect"))
            }.padding(16)
        }
        .background(Color(nsColor: .windowBackgroundColor))
        .onAppear { setup.load() }
        .onChange(of: scenePhase) { phase in if phase == .active { setup.refreshService() } }
        .sheet(isPresented: $settingsOpen) { SettingsView().environmentObject(setup) }
    }

    private var installAppPage: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("Instala Agent Control").font(.title.bold())
            Text("Copiaremos la aplicación a tu carpeta Aplicaciones. Después podrás conectar Hermes o preparar una instalación nueva con unos pocos pasos.")
            Text("No necesitas Terminal, Homebrew ni instalar Python.").foregroundStyle(.secondary)
            Button("Instalar y abrir") { setup.installApplication() }.buttonStyle(.borderedProminent).disabled(setup.busy)
            Button("Abrir mi carpeta Aplicaciones") {
                NSWorkspace.shared.open(SetupModel.installURL.deletingLastPathComponent())
            }
        }
    }

    private var computerPage: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("Conecta tu primer equipo").font(.title.bold())
            Text("Hermes ejecuta tus agentes aquí. Agent Control te permite conversar con ellos desde la web o el teléfono.").foregroundStyle(.secondary)
            Picker("Cómo quieres empezar", selection: $setup.mode) {
                Text("Preparar Hermes para mí").tag("managed")
                Text("Conectar mi Hermes existente").tag("existing")
            }.pickerStyle(.radioGroup).disabled(setup.busy)
            if setup.mode == "existing" {
                if setup.candidates.isEmpty {
                    Text("No detectamos una instalación compatible. Comprueba que Hermes esté instalado con tu usuario o elige preparar una instalación nueva.").foregroundStyle(.secondary)
                    Button("Buscar de nuevo") { setup.load() }.disabled(setup.busy)
                } else {
                    Picker("Instalación", selection: $setup.selectedSource) {
                        ForEach(Array(setup.candidates.enumerated()), id: \.offset) { _, candidate in
                            let path = candidate["hermesSource"] as? String ?? ""
                            Text(candidate["name"] as? String ?? path).tag(path)
                        }
                    }
                    Text("Se conservan tus perfiles, historial y servicios actuales.").font(.callout).foregroundStyle(.secondary)
                    DisclosureGroup("Token de Hermes (solo si no se detecta)") {
                        SecureField("Token del dashboard local", text: $setup.hermesToken).textFieldStyle(.roundedBorder)
                    }
                }
            } else {
                Text("Incluye Hermes y la conexión con Agent Control. Los modelos se usan con la cuenta o clave del proveedor que elijas.").font(.callout)
            }
            Button(setup.mode == "managed" ? "Preparar este equipo" : "Conectar esta instalación") { setup.install() }
                .buttonStyle(.borderedProminent)
                .disabled(setup.busy || (setup.mode == "existing" && setup.selectedSource.isEmpty))
            if setup.backgroundStatus.contains("Pendiente") {
                Button("Ya concedí el permiso") { setup.enableService() }.disabled(setup.busy)
            }
        }
    }

    private var providerPage: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("Elige el modelo de tus agentes").font(.title.bold())
            Text("Usa tu propia cuenta del proveedor. Agent Control no incluye consumo de modelos.").foregroundStyle(.secondary)
            Picker("Proveedor", selection: $setup.provider) {
                Text("OpenRouter").tag("openrouter")
                Text("OpenAI").tag("openai")
                Text("ChatGPT (suscripción)").tag("chatgpt")
                Text("Anthropic").tag("anthropic")
                Text("Google Gemini").tag("gemini")
            }.disabled(setup.busy || !setup.authFlow.isEmpty)
                .onChange(of: setup.provider) { _ in setup.providerConfigured = false; setup.availableModels = []; setup.model = ""; setup.secret = "" }
            if setup.providerConfigured {
                if !setup.availableModels.isEmpty {
                    Picker("Modelo", selection: $setup.model) {
                        ForEach(setup.availableModels, id: \.self) { Text($0).tag($0) }
                    }
                } else { TextField("Identificador del modelo", text: $setup.model) }
                Button("Usar este modelo") { setup.saveProvider() }
                    .buttonStyle(.borderedProminent).disabled(setup.busy || setup.model.isEmpty)
            } else if setup.provider != "chatgpt" {
                SecureField("Clave API", text: $setup.secret).textContentType(.password)
                Button("Conectar proveedor") { setup.saveProvider() }
                    .buttonStyle(.borderedProminent).disabled(setup.busy || !setup.authFlow.isEmpty || setup.secret.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            if ["openrouter", "chatgpt"].contains(setup.provider) && !setup.providerConfigured {
                Divider()
                if let url = setup.authURL {
                    Link("Volver a la autorización del proveedor", destination: url)
                    if !setup.authCode.isEmpty { Text(setup.authCode).font(.system(.title2, design: .monospaced)).textSelection(.enabled) }
                    Text("Esperando a que completes la autorización en tu navegador…").foregroundStyle(.secondary)
                    Button("Cancelar autorización") { setup.cancelOAuth() }.disabled(setup.busy)
                } else {
                    Button("Conectar mi cuenta en el navegador") { setup.startOAuth() }.disabled(setup.busy)
                }
            }
        }.textFieldStyle(.roundedBorder)
    }

    private var connectionPage: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("Vincula este equipo con tu cuenta").font(.title.bold())
            Text("Se abrirá Agent Control en tu navegador. Inicia sesión y confirma el equipo y los agentes que quieres conectar.").foregroundStyle(.secondary)
            if let url = setup.pairingURL {
                Text(setup.pairingCode).font(.system(.largeTitle, design: .monospaced).bold()).textSelection(.enabled)
                Link("Abrir la vinculación", destination: url)
                Text("Esperando tu confirmación en la web…").foregroundStyle(.secondary)
            }
            Button(setup.pairingFlow.isEmpty ? "Vincular equipo" : "Generar un código nuevo") { setup.pair() }
                .buttonStyle(.borderedProminent).disabled(setup.busy)
            if setup.backgroundStatus.contains("Pendiente") {
                Button("Permitir inicio automático") { SMAppService.openSystemSettingsLoginItems() }
                Button("Ya concedí el permiso") { setup.enableService() }.disabled(setup.busy)
            }
        }
    }

    private var readyPage: some View {
        VStack(alignment: .leading, spacing: 18) {
            Label(setup.ready ? "Tu equipo está conectado" : "Comprobemos la conexión", systemImage: setup.ready ? "checkmark.circle.fill" : "clock")
                .font(.title.bold()).foregroundStyle(setup.ready ? Color.green : Color.primary)
            Text(setup.ready ? "Ya puedes conversar con tus agentes. Este equipo debe permanecer encendido y conectado a Internet." : "Agent Control se ha configurado. Confirma que Hermes y el conector estén disponibles antes de empezar.")
                .foregroundStyle(.secondary)
            if !setup.profiles.isEmpty { Text("Agentes: " + setup.profiles.joined(separator: ", ")) }
            Label(setup.backgroundStatus, systemImage: "power")
            Button("Abrir Agent Control") { NSWorkspace.shared.open(SetupModel.website) }.buttonStyle(.borderedProminent).disabled(!setup.ready)
            HStack {
                Button("Comprobar conexión") { setup.load() }.disabled(setup.busy)
                Button("Diagnóstico") { setup.diagnose() }.disabled(setup.busy)
            }
            if !setup.diagnosticSummary.isEmpty { Text(setup.diagnosticSummary).textSelection(.enabled) }
        }
    }

    private func message(_ text: String, icon: String, color: Color) -> some View {
        Label(text, systemImage: icon).font(.callout).foregroundStyle(color)
            .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 24).padding(.vertical, 10)
            .accessibilityAddTraits(.updatesFrequently)
    }
}

struct SettingsView: View {
    @EnvironmentObject var setup: SetupModel
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack { Text("Configuración").font(.title2.bold()); Spacer(); Button("Cerrar") { dismiss() } }
            Text(setup.backgroundStatus).foregroundStyle(.secondary)
            Button("Administrar inicio automático") { SMAppService.openSystemSettingsLoginItems() }
            HStack {
                Button("Comprobar estado") { setup.load() }
                Button("Diagnóstico") { setup.diagnose() }
                Button("Reiniciar") { setup.maintenance("restart") }.disabled(!setup.installed)
            }.disabled(setup.busy)
            if !setup.diagnosticSummary.isEmpty { Text(setup.diagnosticSummary).textSelection(.enabled) }
            Divider()
            Text("Actualizaciones").font(.headline)
            Text("Se aplican cuando tus agentes no están trabajando. Tus datos y conversaciones se conservan.").font(.callout).foregroundStyle(.secondary)
            HStack {
                Button("Actualizar") { setup.maintenance("update") }
                Button("Volver a la versión anterior") { setup.maintenance("rollback") }
            }.disabled(setup.busy || !setup.installed || setup.mode != "managed")
            Divider()
            Button("Consultar funciones opcionales") { setup.listExtras() }.disabled(setup.busy)
            ForEach(Array(setup.extras.enumerated()), id: \.offset) { _, extra in
                HStack {
                    Text(extra["name"] as? String ?? extra["id"] as? String ?? "Función")
                    Spacer()
                    if extra["installed"] as? Bool == true { Text("Instalada").foregroundStyle(.secondary) }
                    else if extra["available"] as? Bool == true { Button("Instalar") { setup.installExtra(extra["id"] as? String ?? "") }.disabled(setup.busy) }
                    else { Text("Próximamente").foregroundStyle(.secondary) }
                }
            }
            if setup.restartRequired {
                Text("La función está instalada y se activará al reiniciar. Primero comprobaremos que tus agentes no estén trabajando.").font(.callout)
                Button("Reiniciar para activar") { setup.maintenance("restart") }.disabled(setup.busy)
            }
            Divider()
            Button("Desinstalar componentes administrados…", role: .destructive) { setup.confirmUninstall = true }.disabled(setup.busy || !setup.installed)
            if !setup.error.isEmpty { Text(setup.error).foregroundStyle(.red) }
            if setup.busy { ProgressView(setup.progress) }
        }.padding(24).frame(width: 580)
        .confirmationDialog("¿Desinstalar los componentes administrados?", isPresented: $setup.confirmUninstall, titleVisibility: .visible) {
            Button("Desinstalar conservando mis datos", role: .destructive) { setup.uninstall() }
        } message: { Text("Tus conversaciones y configuraciones se conservarán. Las instalaciones de Hermes que ya tenías no se eliminan.") }
    }
}
