# Agent Control para macOS

Companion nativo SwiftUI para macOS 13 o posterior, Apple Silicon. El DMG
incluye Python, Hermes y el conector. No requiere Terminal, Homebrew, un Python
del sistema ni permisos de administrador del usuario final.

## Flujo y archivos propios

Al abrir el DMG, **Instalar y abrir** copia la aplicación a
`~/Applications/Agent Control.app`. No reemplaza una aplicación existente:
las actualizaciones se realizan desde el menú de la aplicación.

El asistente permite preparar un Hermes propio o conectar uno compatible que
ya existe. En el segundo caso conserva su configuración y servicios. Una
vinculación previa del conector se muestra sin sustituir sus LaunchAgents.
La selección de proveedor y modelo solo modifica la instalación administrada.
Las claves se envían por stdin al motor local; no se ponen en argumentos,
variables de entorno, logs ni almacenamiento de SwiftUI.

- Bundle ID: `com.jemailabs.agent-control.setup`.
- Datos administrados: `~/Library/Application Support/Agent Control/managed`.
- Identidad del conector existente: `~/.agent-control-connector`.
- Payload inmutable: `Contents/Resources/runtime` con `python`, `hermes`,
  `connector` y `bin/agent-control-setup`.
- Servicio de usuario: `com.jemailabs.agent-control.managed`, registrado con
  `SMAppService.agent(plistName:)`. La app guía el permiso de Ítems de inicio.
- Plist: `Contents/Library/LaunchAgents/com.jemailabs.agent-control.managed.plist`.
  `BundleProgram` es `Contents/MacOS/AgentControlService`; `ProgramArguments`
  contiene `AgentControlService`. El launcher resuelve su binario cargado con
  `_NSGetExecutablePath`, independientemente de argv[0] o el directorio actual.
- El launcher ejecuta el Python empaquetado con
  `-s -B -m agent_control_connector.setup_engine --service --release-root
  <Contents/Resources/runtime> --data-dir <managed>` y reenvía SIGTERM/SIGINT.
  El supervisor solo controla procesos propios. Si Hermes ya existe, ejecuta
  únicamente el conector.

La UI y el actualizador usan el mismo comando con `--rpc` en lugar de
`--service`. El protocolo es JSON Lines: `{id,method,params}` y
`{id,result}` / `{id,error:{code,message}}`, con eventos `{event,data}`.
`PYTHONPATH` contiene `runtime/connector:runtime/hermes`; no se heredan variables
de Python del usuario. La salida de errores cruda no se presenta ni se guarda.

## Actualizar y recuperar

`AgentControlUpdater` vuelve a obtener la oferta y sus firmas RSA desde el
motor. Descarga el DMG por HTTPS, comprueba SHA-256, Gatekeeper y firma de la
app con el mismo Team ID, Developer ID Application, identificador y revisión.
Monta el DMG en modo de solo lectura y prepara una copia antes de solicitar
cualquier detención. Un flock exclusivo permanece adquirido durante toda la
operación, incluida la recuperación.

El motor persiste una transacción y un drenaje del conector, comprueba todos
los perfiles y el registro de operaciones inciertas. El helper desregistra
solo su servicio; el motor comprueba que sus locks se liberaron y copia el
Hermes home propio. `RENAME_SWAP` cambia ambas apps atómicamente. Se conserva
la app anterior en `managed/app-versions/<revision>/Agent Control.app`.
El nuevo servicio debe confirmar disponibilidad antes del commit. Un fallo
restaura app y configuración previas, conservando historial y registro de
operaciones. No se permiten cambios de versión del esquema de datos.

Un cierre interrumpido deja `mac-update.json`; **Actualizar** recupera esa
transacción explícitamente. No reenvía prompts. Un journal ya comprometido se
limpia tras comprobar disponibilidad, sin revertir una versión saludable.
**Reiniciar** llama al ciclo de vida del motor; también exige inactividad y es
el paso explícito para activar una función opcional recién instalada.

La desinstalación detiene componentes propios y conserva datos. La aplicación
puede enviarse a la Papelera después. Las funciones opcionales solo se ofrecen
si el catálogo firmado contiene una versión válida para la plataforma.

## Compilar, firmar y notarizar

Usar un host Apple Silicon de firma autorizado con Xcode, certificado
Developer ID Application y un perfil de credenciales `notarytool` en Keychain.
La contraseña del certificado, credenciales Apple y clave RSA no se incluyen
en el repositorio ni en estos comandos. Los argumentos de clave indican rutas.
Las herramientas no instalan la app ni registran servicios.

```sh
swift build --package-path apps/macos-installer -c release --triple arm64-apple-macosx13.0

python -m deploy.managed.macos.build_app \
  --runtime /ruta/al/agent-control-runtime \
  --output /ruta/release/app \
  --revision SHA_COMPLETO \
  --identity SHA1_CERTIFICADO --team TEAM_ID \
  --manifest-private-key /ruta/protegida/release-key.pem

python -m deploy.managed.macos.notarize_dmg \
  --app '/ruta/release/app/Agent Control.app' \
  --output /ruta/release/distribution --work /ruta/release/notary-work \
  --revision SHA_COMPLETO \
  --identity SHA1_CERTIFICADO --team TEAM_ID \
  --notary-profile NOMBRE_KEYCHAIN \
  --manifest-public-key /ruta/release-public.pem
```

El orden es: firmar cada Mach-O del runtime → generar y firmar su inventario
RSA → firmar ejecutables propios y app → notarizar ZIP inmutable → staple de
una copia → crear y firmar DMG → notarizar DMG inmutable → staple de la copia
distribuible. Se verifica además el payload realmente montado del DMG final.
No se usa `codesign --deep` para firmar ni excepciones al hardened runtime.

Una notarización pendiente sale con código 3. Repetir el mismo comando con
los mismos bytes y directorio de trabajo recupera el ID existente; no vuelve
a firmar ni envía automáticamente otro trabajo incierto. La salida final es
`Agent-Control-<revision>-macos-arm64.dmg` y su `.verification.json`. El
publicador comprueba este recibo y los bytes exactos antes de anunciarlo.

El catálogo opcional puede pasarse con `build_app --extras-catalog`; se incluye
antes de sellar el inventario y la app. Nunca se inyecta después de notarizar.

## Verificación

Los tests `tests/backend/test_managed_macos.py` cubren transacciones, trabajo
activo/incierto, locks, nonces, caducidad, schema mismatch y recuperación tras
fallar readiness. La compilación real de los tres ejecutables comprueba las
APIs de macOS. Ambos helpers aceptan `--executable-path` como diagnóstico sin
arrancar servicios; permite comprobar argv[0] falsificado.

Antes de publicar se requiere el flujo firmado/notarizado completo y la
matriz en un Mac de prueba: instalación limpia, permiso de inicio, proveedor,
vinculación, primer chat, cierre de sesión, actualización, rollback y
desinstalación. Los tests del motor y la compilación no sustituyen esas pruebas
con LaunchServices/Gatekeeper y cuentas reales.
