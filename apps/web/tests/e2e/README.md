# Playwright E2E

La suite usa el bundle productivo servido por `vite preview` y reemplaza únicamente las rutas `/api/v1/*` con respuestas deterministas del navegador. No requiere Hermes, credenciales ni un backend en ejecución.

```bash
npm run test:e2e:install
npm run test:e2e
```

`npm run test:e2e:chromium` ejecuta la matriz mínima de aceptación en 390×844, 1024×1366 y 1440×900. La suite completa añade Firefox de escritorio y WebKit móvil. `PLAYWRIGHT_BASE_URL` permite apuntar las pruebas a un servidor productivo ya iniciado sin levantar `vite preview`.

El escenario offline se ejecuta en Chromium móvil: instala/controla el service worker, activa la caché cifrada desde Ajustes, elimina las rutas API, desconecta el contexto y recarga desde cero. Después verifica que el shell, el historial cifrado y el borrador estén disponibles, y que no exista acción de envío en segundo plano.
