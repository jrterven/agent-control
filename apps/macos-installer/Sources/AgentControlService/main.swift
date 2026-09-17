import Foundation
import Darwin

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

// launchd owns this process. No shell, global PATH lookup, or inherited Python
// configuration is used to run the signed, app-bundled supervisor.
let executable = loadedExecutable()
let app = executable.deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
let expected = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Applications/Agent Control.app")
guard app.standardizedFileURL == expected.standardizedFileURL else { exit(78) }
let runtime = app.appendingPathComponent("Contents/Resources/runtime")
let data = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/Agent Control/managed")
// A stopped installation must not churn launchd/Python while the UI finishes
// unregistering. Default SIGTERM still terminates this child-free wait. Only
// an explicit resume removes the persisted stop request.
while FileManager.default.fileExists(atPath: data.appendingPathComponent("stop.request").path) {
    Thread.sleep(forTimeInterval: 1)
}
let process = Process()
process.executableURL = runtime.appendingPathComponent("python/bin/python3")
process.arguments = ["-s", "-B", "-m", "agent_control_connector.setup_engine", "--service", "--release-root", runtime.path, "--data-dir", data.path]
process.environment = [
    "HOME": FileManager.default.homeDirectoryForCurrentUser.path,
    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8",
    "PYTHONUNBUFFERED": "1", "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
    "SSL_CERT_FILE": runtime.appendingPathComponent("python/lib/python3.12/site-packages/certifi/cacert.pem").path,
    "PYTHONPATH": runtime.appendingPathComponent("connector").path + ":" + runtime.appendingPathComponent("hermes").path,
    "AGENT_CONTROL_APP_BUNDLE": app.path,
]
process.currentDirectoryURL = data
process.standardInput = FileHandle.nullDevice
process.standardOutput = FileHandle.nullDevice
process.standardError = FileHandle.nullDevice
do {
    try process.run()
    // Forward launchd shutdown instead of leaving an orphan supervisor.
    signal(SIGTERM, SIG_IGN)
    signal(SIGINT, SIG_IGN)
    let termination = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .global())
    let interruption = DispatchSource.makeSignalSource(signal: SIGINT, queue: .global())
    termination.setEventHandler { if process.isRunning { process.terminate() } }
    interruption.setEventHandler { if process.isRunning { process.terminate() } }
    termination.resume(); interruption.resume()
    process.waitUntilExit()
    termination.cancel(); interruption.cancel()
    exit(process.terminationStatus)
} catch { exit(78) }
