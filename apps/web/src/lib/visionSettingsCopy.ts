const en = {
  title: "Camera vision",
  description: "Choose the model that answers your camera questions in chat and GPT Live. The camera is analyzed only when you ask.",
  model: "Camera model",
  modelHint: "This choice applies to camera analysis. Your agent keeps its own model.",
  luna: "GPT-5.6 Luna · default",
  terra: "GPT-5.6 Terra",
  sol: "GPT-5.6 Sol",
  configured: "OpenAI connected",
  notConfigured: "OpenAI key needed to use the camera",
  configure: "Manage the OpenAI connection",
  disclosure: "Camera analysis shares the OpenAI key used by GPT Live and consumes your OpenAI quota. Opening the camera requires your action and device permission. We save observations and only the images you choose to save. Requesting that OpenAI not store responses does not guarantee zero provider retention.",
  save: "Save camera preferences",
  saving: "Saving…",
  saved: "Camera preferences saved. Activate the camera again to use them.",
  loading: "Loading camera preferences…",
  loadError: "Camera preferences could not be loaded.",
  saveError: "Camera preferences could not be saved. Try again.",
  retry: "Retry loading",
  offline: "Connect to save camera preferences.",
  unavailable: "Sign in to manage camera preferences.",
};

const es: typeof en = {
  title: "Visión con cámara",
  description: "Elige el modelo que responde tus preguntas sobre la cámara en el chat y GPT Live. La cámara se analiza cuando preguntas.",
  model: "Modelo de la cámara",
  modelHint: "Esta elección se aplica al análisis de la cámara. Tu agente conserva su propio modelo.",
  luna: "GPT-5.6 Luna · predeterminado",
  terra: "GPT-5.6 Terra",
  sol: "GPT-5.6 Sol",
  configured: "OpenAI conectado",
  notConfigured: "Necesitas una clave de OpenAI para usar la cámara",
  configure: "Administrar la conexión de OpenAI",
  disclosure: "El análisis de cámara comparte la clave de OpenAI de GPT Live y consume tu cuota de OpenAI. Abrir la cámara requiere tu acción y el permiso del dispositivo. Guardamos las observaciones y solo las imágenes que elijas guardar. Solicitar que OpenAI no guarde respuestas no garantiza que el proveedor no conserve datos.",
  save: "Guardar preferencias de cámara",
  saving: "Guardando…",
  saved: "Preferencias de cámara guardadas. Activa de nuevo la cámara para usarlas.",
  loading: "Cargando preferencias de cámara…",
  loadError: "No se pudieron cargar las preferencias de cámara.",
  saveError: "No se pudieron guardar las preferencias de cámara. Inténtalo de nuevo.",
  retry: "Reintentar carga",
  offline: "Conéctate para guardar las preferencias de cámara.",
  unavailable: "Inicia sesión para administrar las preferencias de cámara.",
};

export function visionSettingsCopy(language: string) {
  return language.toLowerCase().startsWith("es") ? es : en;
}
