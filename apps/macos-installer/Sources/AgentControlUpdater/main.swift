import AppKit
import CryptoKit
import Darwin
import Foundation
import ServiceManagement

struct UpdateFailure: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

func loadedExecutable() -> URL {
    var size: UInt32 = 0
    _NSGetExecutablePath(nil, &size)
    var bytes = [CChar](repeating: 0, count: Int(size))
    guard _NSGetExecutablePath(&bytes, &size) == 0 else { exit(78) }
    return URL(fileURLWithPath: String(cString: bytes)).resolvingSymlinksInPath()
}
if CommandLine.arguments.dropFirst() == ["--executable-path"] {
    print(loadedExecutable().path)
    exit(0)
}

func tool(_ executable: String, _ arguments: [String]) throws -> String {
    let task = Process(), output = Pipe()
    task.executableURL = URL(fileURLWithPath: executable); task.arguments = arguments
    task.standardInput = FileHandle.nullDevice; task.standardOutput = output; task.standardError = FileHandle.nullDevice
    try task.run()
    let data = output.fileHandleForReading.readDataToEndOfFile()
    task.waitUntilExit()
    guard task.terminationStatus == 0 else { throw UpdateFailure(message: "La verificación o el cambio de aplicación no se completó. Se conservará la versión anterior.") }
    return String(data: data, encoding: .utf8) ?? ""
}

func engine(_ app: URL, _ method: String, _ params: [String: Any]) throws -> [String: Any] {
    let root = app.appendingPathComponent("Contents/Resources/runtime")
    let task = Process(), input = Pipe(), output = Pipe()
    task.executableURL = root.appendingPathComponent("python/bin/python3")
    task.arguments = ["-s", "-B", "-m", "agent_control_connector.setup_engine", "--rpc", "--release-root", root.path, "--data-dir", managed.path]
    task.environment = ["HOME": home.path, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8",
                        "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
                        "SSL_CERT_FILE": root.appendingPathComponent("python/lib/python3.12/site-packages/certifi/cacert.pem").path,
                        "PYTHONPATH": root.appendingPathComponent("connector").path + ":" + root.appendingPathComponent("hermes").path]
    task.standardInput = input; task.standardOutput = output; task.standardError = FileHandle.nullDevice
    try task.run()
    let id = UUID().uuidString
    try input.fileHandleForWriting.write(contentsOf: JSONSerialization.data(withJSONObject: ["id": id, "method": method, "params": params]) + Data([10]))
    try input.fileHandleForWriting.close()
    let data = output.fileHandleForReading.readDataToEndOfFile()
    task.waitUntilExit()
    guard data.count <= 2_000_000, task.terminationStatus == 0 else { throw UpdateFailure(message: "El motor de actualización no respondió. Abre Agent Control para recuperar la operación.") }
    for line in data.split(separator: 10) {
        if let value = try JSONSerialization.jsonObject(with: Data(line)) as? [String: Any], value["id"] as? String == id {
            if let error = value["error"] as? [String: Any] {
                throw UpdateFailure(message: error["message"] as? String ?? "No se puede continuar con seguridad.")
            }
            return value["result"] as? [String: Any] ?? [:]
        }
    }
    throw UpdateFailure(message: "No se recibió la confirmación de la operación. Abre Agent Control para recuperar la versión anterior.")
}

func emit(_ event: String, _ value: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: ["event": event, "data": value]) {
        try? FileHandle.standardOutput.write(contentsOf: data + Data([10]))
    }
}

func progress(_ message: String) { emit("progress", ["message": message]) }

final class Download: NSObject, URLSessionDownloadDelegate {
    let finished = DispatchSemaphore(value: 0)
    let destination: URL
    var failure: Error?
    init(_ destination: URL) { self.destination = destination }
    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didWriteData bytesWritten: Int64, totalBytesWritten: Int64, totalBytesExpectedToWrite: Int64) {
        if totalBytesWritten > 4_000_000_000 { downloadTask.cancel() }
    }
    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didFinishDownloadingTo location: URL) {
        do {
            guard (downloadTask.response as? HTTPURLResponse)?.statusCode == 200,
                  downloadTask.response?.url?.host == "agentcontrol.jemailabs.com" else {
                throw UpdateFailure(message: "No se pudo descargar una actualización del servidor de Agent Control.")
            }
            try FileManager.default.moveItem(at: location, to: destination)
        } catch { failure = error }
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        if let error { failure = error }; finished.signal()
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

func download(_ url: URL, to target: URL, sha256: String) throws {
    guard url.scheme == "https", url.host == "agentcontrol.jemailabs.com", url.user == nil, url.password == nil,
          sha256.range(of: "^[a-f0-9]{64}$", options: .regularExpression) != nil else {
        throw UpdateFailure(message: "La actualización no tiene una descarga verificada.")
    }
    let delegate = Download(target)
    let configuration = URLSessionConfiguration.ephemeral
    configuration.timeoutIntervalForRequest = 60; configuration.timeoutIntervalForResource = 900
    let session = URLSession(configuration: configuration, delegate: delegate, delegateQueue: nil)
    session.downloadTask(with: url).resume()
    delegate.finished.wait(); session.finishTasksAndInvalidate()
    if delegate.failure != nil { throw UpdateFailure(message: "No se pudo descargar la actualización. Comprueba Internet y vuelve a intentarlo.") }
    var digest = SHA256()
    let file = try FileHandle(forReadingFrom: target)
    defer { try? file.close() }
    while let chunk = try file.read(upToCount: 262_144), !chunk.isEmpty { digest.update(data: chunk) }
    guard digest.finalize().map({ String(format: "%02x", $0) }).joined() == sha256 else {
        throw UpdateFailure(message: "La descarga no coincide con la versión firmada. La versión actual sigue intacta.")
    }
}

let home = FileManager.default.homeDirectoryForCurrentUser
let installed = home.appendingPathComponent("Applications/Agent Control.app")
let managed = home.appendingPathComponent("Library/Application Support/Agent Control/managed")
let appID = "com.jemailabs.agent-control.setup"
let service = SMAppService.agent(plistName: "com.jemailabs.agent-control.managed.plist")

final class UpdateLock {
    let descriptor: Int32
    init() throws {
        descriptor = open(managed.appendingPathComponent("mac-updater.lock").path, O_CREAT | O_WRONLY | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard descriptor >= 0 else { throw UpdateFailure(message: "No se puede bloquear la actualización. Revisa los permisos de Agent Control.") }
        var info = stat()
        guard fstat(descriptor, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG,
              info.st_uid == getuid(), (info.st_mode & 0o077) == 0,
              flock(descriptor, LOCK_EX | LOCK_NB) == 0 else {
            close(descriptor)
            throw UpdateFailure(message: "Otra actualización está en curso o su archivo de control no es seguro. Espera a que termine y vuelve a intentarlo.")
        }
    }
    deinit { close(descriptor) }
}

func verifyApp(_ app: URL, team: String, revision: String) throws {
    let info = try Data(contentsOf: app.appendingPathComponent("Contents/Info.plist"))
    guard let metadata = try PropertyListSerialization.propertyList(from: info, format: nil) as? [String: Any],
          metadata["CFBundleIdentifier"] as? String == appID,
          metadata["AgentControlRevision"] as? String == revision else { throw UpdateFailure(message: "La aplicación descargada no corresponde a la actualización solicitada.") }
    let requirement = "anchor apple generic and certificate 1[field.1.2.840.113635.100.6.2.6] exists and certificate leaf[field.1.2.840.113635.100.6.1.13] exists and certificate leaf[subject.OU] = \"\(team)\" and identifier \"\(appID)\" and notarized"
    _ = try tool("/usr/bin/codesign", ["--verify", "--strict", "--deep", "--check-notarization", "-R=" + requirement, app.path])
    _ = try tool("/usr/sbin/spctl", ["--assess", "--type", "execute", app.path])
}

func ownTeam() throws -> String {
    // codesign displays metadata on stderr, separate from generic tools.
    let command = Process(), output = Pipe()
    command.executableURL = URL(fileURLWithPath: "/usr/bin/codesign")
    command.arguments = ["--display", "--verbose=4", installed.path]
    command.standardError = output; command.standardOutput = FileHandle.nullDevice
    try command.run(); let data = output.fileHandleForReading.readDataToEndOfFile(); command.waitUntilExit()
    let line = String(data: data, encoding: .utf8)?.split(separator: "\n").first { $0.hasPrefix("TeamIdentifier=") }
    let team = line.map { String($0.dropFirst("TeamIdentifier=".count)) } ?? ""
    guard command.terminationStatus == 0, team.range(of: "^[A-Z0-9]{10}$", options: .regularExpression) != nil else {
        throw UpdateFailure(message: "La firma de la aplicación actual no es válida. Descarga de nuevo Agent Control.")
    }
    return team
}

func register() throws {
    try service.register()
    guard service.status == .enabled else { throw UpdateFailure(message: "Permite Agent Control en Ítems de inicio para terminar de recuperar la conexión.") }
}

func stop() async throws {
    if service.status == .enabled || service.status == .requiresApproval { try await service.unregister() }
}

func exchange(_ first: URL, _ second: URL) throws {
    // Both apps are on the user's Applications volume. RENAME_SWAP guarantees
    // there is never a crash window with no app at the canonical location.
    guard renamex_np(first.path, second.path, UInt32(RENAME_SWAP)) == 0 else {
        throw UpdateFailure(message: "No se pudo cambiar la aplicación de forma atómica. La versión actual se conservó.")
    }
}

func reopen() async throws {
    let config = NSWorkspace.OpenConfiguration(); config.createsNewApplicationInstance = true
    try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
        NSWorkspace.shared.openApplication(at: installed, configuration: config) { _, error in
            if let error { continuation.resume(throwing: error) }
            else { continuation.resume() }
        }
    }
}

func apply(_ request: [String: Any]) async throws {
    let method = request["method"] as? String ?? "update"
    guard ["update", "rollback"].contains(method) else { throw UpdateFailure(message: "Operación no válida.") }
    let executable = loadedExecutable()
    guard executable.deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent() == installed else {
        throw UpdateFailure(message: "Abre Agent Control desde tu carpeta Aplicaciones para actualizar.")
    }
    let operationLock = try UpdateLock()
    defer { withExtendedLifetime(operationLock) {} }
    let team = try ownTeam()
    let info = try PropertyListSerialization.propertyList(from: Data(contentsOf: installed.appendingPathComponent("Contents/Info.plist")), format: nil) as? [String: Any]
    guard let oldRevision = info?["AgentControlRevision"] as? String else { throw UpdateFailure(message: "La aplicación actual no tiene una revisión válida.") }
    try verifyApp(installed, team: team, revision: oldRevision)
    // Fetch the offer again in the helper. UI parameters cannot authorize an
    // arbitrary URL, hash, app, or replacement revision.
    let offer = try engine(installed, method, [:])
    if offer["recoveryRequired"] as? Bool == true {
        try await recover(offer, team: team)
        return
    }
    guard offer["actionRequired"] as? Bool == true else { emit("complete", ["status": "current"]); return }
    guard let revision = offer["revision"] as? String,
          revision.range(of: "^[a-f0-9]{40}$", options: .regularExpression) != nil else { throw UpdateFailure(message: "Revisión de actualización no válida.") }
    let stage = home.appendingPathComponent("Applications/.agent-control-update-" + UUID().uuidString)
    try FileManager.default.createDirectory(at: stage, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
    var preserveStage = false
    defer { if !preserveStage { try? FileManager.default.removeItem(at: stage) } }
    let candidate = stage.appendingPathComponent("Agent Control.app")
    if method == "rollback" {
        guard let path = offer["appPath"] as? String else { throw UpdateFailure(message: "No hay una aplicación anterior disponible.") }
        let previous = managed.appendingPathComponent("app-versions/" + revision + "/Agent Control.app")
        guard URL(fileURLWithPath: path).standardizedFileURL == previous.standardizedFileURL else { throw UpdateFailure(message: "La versión anterior no pertenece a esta instalación.") }
        try verifyApp(previous, team: team, revision: revision)
        try FileManager.default.copyItem(at: previous, to: candidate)
    } else {
        guard let source = offer["downloadUrl"] as? String, let url = URL(string: source), let hash = offer["sha256"] as? String else { throw UpdateFailure(message: "No hay una descarga verificada.") }
        progress("Descargando la actualización…")
        let dmg = stage.appendingPathComponent("update.dmg")
        try download(url, to: dmg, sha256: hash)
        _ = try tool("/usr/bin/hdiutil", ["verify", dmg.path])
        _ = try tool("/usr/sbin/spctl", ["--assess", "--type", "open", "--context", "context:primary-signature", dmg.path])
        let mount = stage.appendingPathComponent("volume")
        try FileManager.default.createDirectory(at: mount, withIntermediateDirectories: false)
        _ = try tool("/usr/bin/hdiutil", ["attach", "-readonly", "-nobrowse", "-noautoopen", "-mountpoint", mount.path, dmg.path])
        do {
            let payload = mount.appendingPathComponent("Agent Control.app")
            try verifyApp(payload, team: team, revision: revision)
            try FileManager.default.copyItem(at: payload, to: candidate)
            _ = try tool("/usr/bin/hdiutil", ["detach", mount.path])
        } catch { _ = try? tool("/usr/bin/hdiutil", ["detach", mount.path]); throw error }
    }
    try verifyApp(candidate, team: team, revision: revision)
    progress("Comprobando que tus agentes hayan terminado…")
    let prepared = try engine(installed, method, ["phase": "prepare", "targetRoot": candidate.appendingPathComponent("Contents/Resources/runtime").path, "revision": revision])
    guard let transaction = prepared["transactionId"] as? String else { throw UpdateFailure(message: "No se autorizó el cambio de versión.") }
    let previous = managed.appendingPathComponent("app-versions/" + oldRevision + "/Agent Control.app")
    var swapped = false
    var savedOld = candidate
    do {
        try await stop()
        progress("Guardando una copia de tus datos…")
        _ = try engine(installed, method, ["phase": "stopped", "transactionId": transaction])
        try FileManager.default.createDirectory(at: previous.deletingLastPathComponent(), withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        try exchange(installed, candidate); swapped = true
        if FileManager.default.fileExists(atPath: previous.path) { try verifyApp(previous, team: team, revision: oldRevision) }
        else { try FileManager.default.moveItem(at: candidate, to: previous); savedOld = previous }
        _ = try engine(installed, method, ["phase": "activate", "transactionId": transaction])
        try register()
        progress("Verificando Hermes y la conexión…")
        _ = try engine(installed, method, ["phase": "complete", "transactionId": transaction])
        // The transaction is committed. Failure to raise the UI must never
        // roll back a healthy running service after its journal was cleared.
        do { try await reopen(); emit("complete", ["status": "complete", "restartApp": true]) }
        catch { emit("complete", ["status": "complete", "restartApp": false]) }
    } catch {
        progress("Recuperando la versión anterior…")
        do {
            _ = try engine(installed, method, ["phase": "recovery-check", "transactionId": transaction])
            try await stop()
            if swapped { try exchange(installed, savedOld) }
            _ = try engine(installed, method, ["phase": "abort", "transactionId": transaction])
            try register()
            _ = try engine(installed, method, ["phase": "resume", "transactionId": transaction])
        } catch {
            preserveStage = true
            throw UpdateFailure(message: "La actualización se detuvo y necesita recuperación. Conservamos la aplicación anterior y tus datos. Abre Agent Control y vuelve a pulsar Actualizar para recuperar la operación; no borres sus archivos.")
        }
        throw UpdateFailure(message: "La nueva versión no pasó la comprobación. Se restauró la versión anterior y tus datos siguen disponibles.")
    }
}

func recover(_ offer: [String: Any], team: String) async throws {
    guard let transaction = offer["transactionId"] as? String else { throw UpdateFailure(message: "No se puede identificar la actualización pendiente.") }
    let detail = try engine(installed, "update", ["phase": "inspect", "transactionId": transaction])
    if detail["phase"] as? String == "complete" {
        // A crash after commit must not roll back a healthy release. Recheck
        // readiness and finish journal cleanup without stopping either agent.
        _ = try engine(installed, "update", ["phase": "complete", "transactionId": transaction])
        do { try await reopen(); emit("complete", ["status": "complete", "restartApp": true]) }
        catch { emit("complete", ["status": "complete", "restartApp": false]) }
        return
    }
    guard let old = detail["oldRelease"] as? String else { throw UpdateFailure(message: "No se pudo identificar la versión anterior.") }
    let previous = managed.appendingPathComponent("app-versions/" + old + "/Agent Control.app")
    let metadata = try PropertyListSerialization.propertyList(from: Data(contentsOf: installed.appendingPathComponent("Contents/Info.plist")), format: nil) as? [String: Any]
    _ = try engine(installed, "update", ["phase": "recovery-check", "transactionId": transaction])
    try await stop()
    if metadata?["AgentControlRevision"] as? String != old {
        var restore = previous
        if !FileManager.default.fileExists(atPath: restore.path), let root = detail["targetRoot"] as? String {
            let staged = URL(fileURLWithPath: root).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            let parent = staged.deletingLastPathComponent()
            guard parent.deletingLastPathComponent() == installed.deletingLastPathComponent(), parent.lastPathComponent.hasPrefix(".agent-control-update-") else {
                throw UpdateFailure(message: "No se pudo localizar una copia anterior propia y verificada.")
            }
            restore = staged
        }
        try verifyApp(restore, team: team, revision: old)
        try exchange(installed, restore)
    }
    _ = try engine(installed, "update", ["phase": "abort", "transactionId": transaction])
    try register()
    _ = try engine(installed, "update", ["phase": "resume", "transactionId": transaction])
    do { try await reopen(); emit("complete", ["status": "restored", "restartApp": true]) }
    catch { emit("complete", ["status": "restored", "restartApp": false]) }
}

Task {
    do {
        let data = FileHandle.standardInput.readDataToEndOfFile()
        guard data.count <= 4096, let request = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { throw UpdateFailure(message: "Solicitud no válida.") }
        try await apply(request)
        exit(0)
    } catch {
        emit("error", ["message": (error as? UpdateFailure)?.message ?? "La actualización se detuvo. Comprueba el diagnóstico antes de reintentar."])
        exit(1)
    }
}
dispatchMain()
