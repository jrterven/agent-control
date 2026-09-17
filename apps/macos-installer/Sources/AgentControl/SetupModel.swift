import AppKit
import Foundation
import ServiceManagement
import SwiftUI

enum SetupPage: Int, CaseIterable {
    case computer, provider, connection, ready
    var title: String { ["Tu equipo", "Tu modelo", "Conecta", "Listo"][rawValue] }
}

@MainActor
final class SetupModel: ObservableObject {
    static let website = URL(string: "https://agentcontrol.jemailabs.com")!
    static let serviceName = "com.jemailabs.agent-control.managed.plist"
    @Published var page: SetupPage = .computer
    @Published var mode = "managed"
    @Published var busy = false
    @Published var progress = ""
    @Published var error = ""
    @Published var notice = ""
    @Published var ready = false
    @Published var installed = false
    @Published var candidates: [[String: Any]] = []
    @Published var selectedSource = ""
    @Published var hermesToken = ""
    @Published var localReady = false
    @Published var paired = false
    @Published var profiles: [String] = []
    @Published var provider = "openrouter"
    @Published var model = ""
    @Published var availableModels: [String] = []
    @Published var providerConfigured = false
    @Published var needsProvider = true
    @Published var secret = ""
    @Published var authURL: URL?
    @Published var authFlow = ""
    @Published var authCode = ""
    @Published var pairingURL: URL?
    @Published var pairingCode = ""
    @Published var pairingFlow = ""
    @Published var backgroundStatus = ""
    @Published var diagnosticSummary = ""
    @Published var extras: [[String: Any]] = []
    @Published var restartRequired = false
    @Published var confirmUninstall = false
    @Published var needsAppInstall = !SetupModel.isInstalledApp
    private let engine = EngineClient()
    private var flowTask: Task<Void, Never>?
    private var updateProcess: Process?
    private var updateBuffer = Data()

    init() {
        engine.onProgress = { [weak self] event, data in
            guard event == "progress" || event == "stage" else { return }
            self?.progress = EngineClient.safeMessage(data["message"] as? String ?? data["stage"] as? String)
        }
        refreshService()
    }

    func perform(_ title: String, _ work: @escaping () async throws -> Void) {
        guard !busy else { return }
        busy = true; error = ""; notice = ""; progress = title
        Task {
            defer { busy = false; progress = "" }
            do { try await work() }
            catch { self.error = EngineClient.safeMessage((error as? SetupFailure)?.message) }
        }
    }

    func load() {
        guard !needsAppInstall else { return }
        perform("Comprobando tu equipo…") {
            self.applyStatus(try await self.engine.request("status"))
            let found = try await self.engine.request("detect")
            self.candidates = found["existing"] as? [[String: Any]] ?? []
            if let first = self.candidates.first {
                self.selectedSource = first["hermesSource"] as? String ?? ""
            }
            if self.ready || self.paired { self.page = .ready }
            else if self.installed && self.localReady {
                self.page = self.needsProvider ? .provider : .connection
            }
            self.refreshService()
        }
    }

    func install() {
        let token = hermesToken
        hermesToken = ""
        perform(mode == "managed" ? "Preparando Hermes y Agent Control…" : "Comprobando tu instalación de Hermes…") {
            var params: [String: Any] = ["mode": self.mode]
            if self.mode == "existing" {
                params["hermesSource"] = self.selectedSource
                if let item = self.candidates.first(where: { $0["hermesSource"] as? String == self.selectedSource }) {
                    params["hermesHome"] = item["hermesHome"]
                }
                if !token.isEmpty { params["token"] = token }
            }
            let result = try await self.engine.request("install", params)
            self.applyStatus(result)
            self.installed = true
            if self.paired { self.page = .ready; return }
            if try await self.registerManagedService() {
                self.applyStatus(try await self.engine.request("install-service", ["manager": "smappservice"]))
                self.page = self.mode == "managed" ? .provider : .connection
            }
        }
    }

    func saveProvider() {
        let credential = secret
        secret = ""
        perform("Comprobando el proveedor…") {
            var params: [String: Any] = ["provider": self.provider]
            if !credential.isEmpty { params["apiKey"] = credential }
            if self.providerConfigured && !self.model.isEmpty { params["model"] = self.model }
            let result = try await self.engine.request("configure-provider", params)
            if params["model"] != nil { self.page = .connection }
            else { self.applyModels(result) }
        }
    }

    func startOAuth() {
        perform("Abriendo la autorización del proveedor…") {
            let result = try await self.engine.request("oauth-start", ["provider": self.provider])
            guard let url = Self.httpsURL(result["authorizationUrl"] ?? result["verificationUrl"]),
                  let flow = result["flowId"] as? String else {
                throw SetupFailure(message: "El proveedor no pudo iniciar la autorización. Usa una clave API o vuelve a intentarlo.")
            }
            self.authURL = url; self.authFlow = flow
            self.authCode = result["userCode"] as? String ?? ""
            NSWorkspace.shared.open(url)
            self.pollOAuth()
        }
    }

    private func pollOAuth() {
        flowTask?.cancel()
        let flow = authFlow
        flowTask = Task {
            for _ in 0..<180 {
                do {
                    try await Task.sleep(nanoseconds: 5_000_000_000)
                    guard !Task.isCancelled, authFlow == flow else { return }
                    let result = try await engine.request("oauth-poll", ["flowId": flow], timeout: 30)
                    if ["complete", "authorized", "connected"].contains(result["status"] as? String ?? "") {
                        authURL = nil; authFlow = ""; applyModels(result); return
                    }
                    if ["expired", "denied", "cancelled", "error"].contains(result["status"] as? String ?? "") {
                        throw SetupFailure(message: "La autorización terminó sin conectar el proveedor. Vuelve a intentarlo.")
                    }
                } catch is CancellationError { return }
                catch { self.error = EngineClient.safeMessage((error as? SetupFailure)?.message); return }
            }
            error = "La autorización caducó. Iníciala de nuevo."
        }
    }

    func cancelOAuth() {
        flowTask?.cancel()
        let flow = authFlow
        authFlow = ""; authURL = nil
        perform("Cancelando autorización…") { _ = try await self.engine.request("oauth-cancel", ["flowId": flow]) }
    }

    func pair() {
        perform("Preparando la vinculación…") {
            let result = try await self.engine.request("pair-start", ["server": Self.website.absoluteString])
            guard let url = Self.httpsURL(result["verificationUrl"] ?? result["authorizationUrl"]),
                  url.host == Self.website.host,
                  let code = result["userCode"] as? String,
                  let flow = result["flowId"] as? String else {
                throw SetupFailure(message: "No se pudo crear el código de vinculación. Comprueba tu conexión y reintenta.")
            }
            self.pairingURL = url; self.pairingCode = code; self.pairingFlow = flow
            NSWorkspace.shared.open(url)
            self.pollPairing()
        }
    }

    private func pollPairing() {
        flowTask?.cancel()
        let flow = pairingFlow
        flowTask = Task {
            for _ in 0..<180 {
                do {
                    try await Task.sleep(nanoseconds: 5_000_000_000)
                    guard !Task.isCancelled, pairingFlow == flow else { return }
                    let result = try await engine.request("pair-poll", ["flowId": flow], timeout: 30)
                    if ["complete", "paired", "connected"].contains(result["status"] as? String ?? "") {
                        pairingFlow = ""; pairingURL = nil
                        enableService()
                        return
                    }
                    if ["expired", "denied", "cancelled", "error"].contains(result["status"] as? String ?? "") {
                        throw SetupFailure(message: "El código caducó o no fue aprobado. Genera uno nuevo y confirma tus agentes en la web.")
                    }
                } catch is CancellationError { return }
                catch { self.error = EngineClient.safeMessage((error as? SetupFailure)?.message); pairingFlow = ""; return }
            }
            pairingFlow = ""; error = "El código caducó. Genera uno nuevo para vincular este equipo."
        }
    }

    func enableService() {
        perform("Activando la conexión en segundo plano…") {
            if try await !self.registerManagedService() { return }
            // SMAppService owns registration; the engine checks the running
            // supervisor and waits for actual Hermes and cloud readiness.
            let params: [String: Any] = ["manager": "smappservice"]
            self.applyStatus(try await self.engine.request("install-service", params))
            self.applyStatus(try await self.engine.request("status"))
            self.page = self.paired ? .ready : (self.needsProvider ? .provider : .connection)
        }
    }

    private func registerManagedService() async throws -> Bool {
        guard Self.isInstalledApp else { throw SetupFailure(message: "Instala primero Agent Control en tu carpeta Aplicaciones.") }
        let service = SMAppService.agent(plistName: Self.serviceName)
        if service.status != .enabled { try service.register() }
        refreshService()
        if service.status == .requiresApproval {
            notice = "Permite Agent Control en Ítems de inicio y pulsa Ya concedí el permiso para continuar."
            SMAppService.openSystemSettingsLoginItems()
            return false
        }
        return true
    }

    func refreshService() {
        switch SMAppService.agent(plistName: Self.serviceName).status {
        case .enabled: backgroundStatus = "Inicio automático activado"
        case .requiresApproval: backgroundStatus = "Pendiente de permiso en Ítems de inicio"
        case .notRegistered: backgroundStatus = "Inicio automático desactivado"
        default: backgroundStatus = "Abre la aplicación desde Aplicaciones para activar el inicio automático"
        }
    }

    func diagnose() {
        perform("Comprobando la instalación…") {
            let result = try await self.engine.request("diagnose")
            self.applyStatus(result)
            self.diagnosticSummary = EngineClient.safeMessage(result["summary"] as? String ?? result["message"] as? String)
        }
    }

    func maintenance(_ method: String) {
        if method == "restart" {
            perform("Comprobando que tus agentes hayan terminado antes de reiniciar…") {
                self.applyStatus(try await self.engine.request("restart"))
                self.restartRequired = false
                self.notice = "Servicio reiniciado y conexión verificada."
            }
            return
        }
        guard !busy else { return }
        busy = true; error = ""; notice = ""; progress = "Preparando una actualización segura…"
        let process = Process(), input = Pipe(), output = Pipe()
        process.executableURL = Bundle.main.bundleURL.appendingPathComponent("Contents/MacOS/AgentControlUpdater")
        process.standardInput = input; process.standardOutput = output; process.standardError = FileHandle.nullDevice
        process.environment = ["HOME": FileManager.default.homeDirectoryForCurrentUser.path, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8"]
        updateBuffer.removeAll()
        process.terminationHandler = { [weak self] stopped in
            Task { @MainActor in
                guard let self else { return }
                self.busy = false; self.progress = ""
                if stopped.terminationStatus != 0 && self.error.isEmpty {
                    self.error = "La actualización se detuvo. Comprueba el estado antes de volver a intentarlo."
                }
            }
        }
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { handle.readabilityHandler = nil; return }
            Task { @MainActor in self?.receiveUpdate(data) }
        }
        do {
            try process.run(); updateProcess = process
            try input.fileHandleForWriting.write(contentsOf: JSONSerialization.data(withJSONObject: ["method": method]))
            try input.fileHandleForWriting.close()
        } catch {
            output.fileHandleForReading.readabilityHandler = nil
            busy = false; progress = ""
            self.error = "No se pudo iniciar el actualizador. Descarga de nuevo la aplicación completa."
        }
    }

    private func receiveUpdate(_ data: Data) {
        updateBuffer.append(data)
        guard updateBuffer.count < 2_000_000 else { return }
        while let end = updateBuffer.firstIndex(of: 10) {
            let line = Data(updateBuffer[..<end]); updateBuffer.removeSubrange(...end)
            guard let item = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
                  let event = item["event"] as? String, let value = item["data"] as? [String: Any] else { continue }
            if event == "progress" { progress = EngineClient.safeMessage(value["message"] as? String) }
            if event == "error" { error = EngineClient.safeMessage(value["message"] as? String) }
            if event == "complete" {
                notice = value["status"] as? String == "current" ? "Ya tienes la versión actual." : "Versión verificada y conexión recuperada."
                if value["restartApp"] as? Bool == true { NSApplication.shared.terminate(nil) }
            }
        }
    }

    func uninstall() {
        perform("Desinstalando los componentes administrados…") {
            // Engine must refuse active work before stopping any component.
            _ = try await self.engine.request("uninstall", ["keepData": true, "manager": "smappservice"])
            let service = SMAppService.agent(plistName: Self.serviceName)
            if service.status == .enabled || service.status == .requiresApproval { try await service.unregister() }
            self.installed = false; self.ready = false; self.page = .computer
            self.notice = "Componentes administrados desinstalados. Se conservaron tus datos y las instalaciones externas de Hermes."
            self.refreshService()
        }
    }

    func listExtras() {
        perform("Consultando funciones opcionales…") {
            let result = try await self.engine.request("extras-list")
            self.extras = result["items"] as? [[String: Any]] ?? []
        }
    }

    func installExtra(_ id: String) {
        perform("Instalando la función opcional…") {
            let installed = try await self.engine.request("extras-install", ["id": id])
            self.restartRequired = installed["restartRequired"] as? Bool ?? false
            if self.restartRequired { self.notice = "Función instalada. Pulsa Reiniciar para activarla cuando tus agentes hayan terminado." }
            let result = try await self.engine.request("extras-list")
            self.extras = result["items"] as? [[String: Any]] ?? []
        }
    }

    private func applyStatus(_ result: [String: Any]) {
        if let value = result["ready"] as? Bool { ready = value }
        if let value = result["installed"] as? Bool { installed = value }
        if let value = result["mode"] as? String { mode = value }
        if let value = result["localReady"] as? Bool { localReady = value }
        if let value = result["paired"] as? Bool { paired = value }
        if let value = result["needsProvider"] as? Bool { needsProvider = value }
        if let value = result["restartRequired"] as? Bool { restartRequired = value }
        if let values = result["profiles"] as? [String] { profiles = values }
        if let values = result["profiles"] as? [[String: Any]] {
            profiles = values.compactMap { $0["displayName"] as? String ?? $0["name"] as? String }
        }
    }

    private func applyModels(_ result: [String: Any]) {
        availableModels = result["models"] as? [String] ?? []
        model = result["recommendedModel"] as? String ?? availableModels.first ?? ""
        providerConfigured = true
        if availableModels.isEmpty {
            error = "El proveedor se guardó, pero no devolvió modelos disponibles. Revisa tu cuenta o indica el identificador de un modelo compatible."
        }
    }

    static func httpsURL(_ raw: Any?) -> URL? {
        guard let text = raw as? String, let url = URL(string: text),
              url.scheme == "https", url.host != nil, url.user == nil, url.password == nil else { return nil }
        return url
    }

    static var installURL: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Applications/Agent Control.app")
    }

    static var isInstalledApp: Bool {
        Bundle.main.bundleURL.standardizedFileURL == installURL.standardizedFileURL
    }

    func installApplication() {
        perform("Copiando Agent Control a tus aplicaciones…") {
            let target = Self.installURL
            let parent = target.deletingLastPathComponent()
            try FileManager.default.createDirectory(at: parent, withIntermediateDirectories: true)
            let parentInfo = try parent.resourceValues(forKeys: [.isSymbolicLinkKey])
            guard parentInfo.isSymbolicLink != true else {
                throw SetupFailure(message: "Tu carpeta Aplicaciones es un enlace. Elige una instalación normal en tu carpeta de usuario antes de continuar.")
            }
            if FileManager.default.fileExists(atPath: target.path) {
                // Re-running a downloaded installer never overwrites an app
                // whose service may be running active agent work.
                throw SetupFailure(message: "Ya hay una aplicación en tu carpeta Aplicaciones. Ábrela y usa Actualizar; no reemplazaremos una versión que puede estar trabajando.")
            }
            try FileManager.default.copyItem(at: Bundle.main.bundleURL, to: target)
            let configuration = NSWorkspace.OpenConfiguration()
            configuration.activates = true
            NSWorkspace.shared.openApplication(at: target, configuration: configuration) { _, error in
                Task { @MainActor in
                    if error == nil { NSApplication.shared.terminate(nil) }
                    else { self.error = "La aplicación se copió. Ábrela desde tu carpeta Aplicaciones para continuar." }
                }
            }
        }
    }
}
