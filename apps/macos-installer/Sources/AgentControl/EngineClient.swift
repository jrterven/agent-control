import Foundation

struct SetupFailure: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

/// Only a bundled, locally launched engine receives these requests. Never use a
/// shell, a network listener, user-supplied executable paths, or stderr as UI.
@MainActor
final class EngineClient {
    private var process: Process?
    private var input: FileHandle?
    private var output: FileHandle?
    private var buffer = Data()
    private var pending: [String: CheckedContinuation<[String: Any], Error>] = [:]
    private var deadlines: [String: Task<Void, Never>] = [:]
    var onProgress: ((String, [String: Any]) -> Void)?

    private func start() throws {
        if process?.isRunning == true { return }
        guard let resources = Bundle.main.resourceURL else {
            throw SetupFailure(message: "La aplicación está incompleta. Descarga de nuevo el instalador de Agent Control.")
        }
        let runtime = resources.appendingPathComponent("runtime")
        let executable = runtime.appendingPathComponent("python/bin/python3")
        guard FileManager.default.isExecutableFile(atPath: executable.path) else {
            throw SetupFailure(message: "Falta el motor de instalación. Descarga de nuevo la aplicación completa; no necesitas instalar Python.")
        }
        let engine = Process()
        engine.executableURL = executable
        let managed = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/Agent Control/managed")
        engine.arguments = ["-s", "-B", "-m", "agent_control_connector.setup_engine", "--rpc",
                            "--release-root", runtime.path, "--data-dir", managed.path]
        engine.currentDirectoryURL = resources
        engine.environment = [
            "HOME": FileManager.default.homeDirectoryForCurrentUser.path,
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8", "PYTHONUNBUFFERED": "1", "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "SSL_CERT_FILE": runtime.appendingPathComponent("python/lib/python3.12/site-packages/certifi/cacert.pem").path,
            "PYTHONPATH": runtime.appendingPathComponent("connector").path + ":" + runtime.appendingPathComponent("hermes").path,
            "AGENT_CONTROL_APP_BUNDLE": Bundle.main.bundleURL.path,
        ]
        let incoming = Pipe(), outgoing = Pipe()
        engine.standardInput = incoming
        engine.standardOutput = outgoing
        // The engine's structured errors are the only user-visible errors.
        // Discard raw stderr, which may contain provider or transport secrets.
        engine.standardError = FileHandle.nullDevice
        engine.terminationHandler = { [weak self] stopped in
            Task { @MainActor in
                guard self?.process === stopped else { return }
                self?.failAll("El motor se ha detenido. Abre de nuevo la configuración para reintentar; tus datos siguen guardados.")
            }
        }
        try engine.run()
        process = engine
        input = incoming.fileHandleForWriting
        output = outgoing.fileHandleForReading
        buffer.removeAll(keepingCapacity: false)
        outgoing.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { handle.readabilityHandler = nil; return }
            Task { @MainActor in self?.receive(data) }
        }
    }

    func request(_ method: String, _ params: [String: Any] = [:], timeout: Double = 900) async throws -> [String: Any] {
        try start()
        let id = UUID().uuidString
        let payload = try JSONSerialization.data(withJSONObject: ["id": id, "method": method, "params": params]) + Data([10])
        return try await withCheckedThrowingContinuation { continuation in
            pending[id] = continuation
            deadlines[id] = Task { [weak self] in
                try? await Task.sleep(nanoseconds: UInt64(timeout * 1_000_000_000))
                guard !Task.isCancelled else { return }
                guard let self, let waiting = self.pending.removeValue(forKey: id) else { return }
                self.deadlines.removeValue(forKey: id)
                // Do not kill an installer mid-transaction. Its next status
                // query reconciles the persisted state before any retry.
                waiting.resume(throwing: SetupFailure(message: "La operación sigue tardando. Comprueba el estado antes de volver a intentarlo."))
            }
            do { try input?.write(contentsOf: payload) }
            catch {
                pending.removeValue(forKey: id)
                deadlines.removeValue(forKey: id)?.cancel()
                continuation.resume(throwing: SetupFailure(message: "No se pudo contactar con el motor local. Cierra y vuelve a abrir la aplicación."))
            }
        }
    }

    private func receive(_ data: Data) {
        buffer.append(data)
        guard buffer.count <= 2_000_000 else {
            failAll("El motor devolvió una respuesta demasiado grande. Ejecuta el diagnóstico antes de reintentar.")
            return
        }
        while let end = buffer.firstIndex(of: 10) {
            let line = Data(buffer[..<end])
            buffer.removeSubrange(...end)
            guard !line.isEmpty,
                  let envelope = try? JSONSerialization.jsonObject(with: line) as? [String: Any] else { continue }
            if let event = envelope["event"] as? String {
                onProgress?(event, envelope["data"] as? [String: Any] ?? [:])
                continue
            }
            guard let id = envelope["id"] as? String, let waiting = pending.removeValue(forKey: id) else { continue }
            deadlines.removeValue(forKey: id)?.cancel()
            if let error = envelope["error"] as? [String: Any] {
                waiting.resume(throwing: SetupFailure(message: Self.safeMessage(error["message"] as? String)))
            } else {
                waiting.resume(returning: envelope["result"] as? [String: Any] ?? [:])
            }
        }
    }

    static func safeMessage(_ value: String?) -> String {
        guard let value, !value.isEmpty, value.count < 1_500 else {
            return "No se pudo completar la operación. Comprueba el diagnóstico y vuelve a intentarlo."
        }
        // Engine errors are contracted to be redacted; suppress recognizable
        // credential-bearing output as a second layer without logging it.
        let lower = value.lowercased()
        if lower.contains("bearer ") || lower.contains("api_key=") || lower.contains("access_token=") || lower.contains("sk-") {
            return "La configuración del proveedor necesita atención. Revisa tus credenciales en Configuración."
        }
        return value
    }

    private func failAll(_ message: String) {
        output?.readabilityHandler = nil
        for deadline in deadlines.values { deadline.cancel() }
        deadlines.removeAll()
        let outstanding = pending
        pending.removeAll()
        for waiting in outstanding.values { waiting.resume(throwing: SetupFailure(message: message)) }
    }
}
