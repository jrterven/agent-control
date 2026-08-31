self.addEventListener("push", (event) => {
  let payload;
  try {
    payload = event.data ? event.data.json() : null;
  } catch {
    payload = null;
  }
  if (!payload || typeof payload !== "object") return;
  const data = payload.data && typeof payload.data === "object" ? payload.data : {};
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const client of windows) {
      client.postMessage({
        type: "agent-control:notification",
        sessionId: data.sessionId,
        occurredAt: data.occurredAt,
      });
    }
    if (windows.some((client) => client.visibilityState === "visible")) return;
    await self.registration.showNotification(String(payload.title || "Agent Control"), {
      body: String(payload.body || ""),
      icon: "/icon-192.png",
      badge: "/icon-192.png",
      tag: String(payload.tag || "agent-control-task"),
      data,
    });
  })());
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const data = event.notification.data && typeof event.notification.data === "object"
    ? event.notification.data
    : {};
  const targetUrl = typeof data.url === "string" ? data.url : "/chats";
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const client = windows[0];
    if (client) {
      const navigated = "navigate" in client ? await client.navigate(targetUrl) : null;
      await (navigated || client).focus();
      return;
    }
    await self.clients.openWindow(targetUrl);
  })());
});
