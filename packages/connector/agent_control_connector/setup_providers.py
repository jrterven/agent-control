"""Local-only provider setup. Neither credentials nor OAuth codes go to cloud."""
from __future__ import annotations

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import secrets
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

PROVIDERS = {
    "openai": ("OPENAI_API_KEY", "https://api.openai.com/v1/models"),
    "anthropic": ("ANTHROPIC_API_KEY", "https://api.anthropic.com/v1/models"),
    "gemini": ("GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/models"),
    "openrouter": ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1/key"),
}


def validate_key(provider: str, value: str) -> None:
    if provider not in PROVIDERS or not isinstance(value, str) or not 8 <= len(value) <= 4096 or any(c.isspace() for c in value):
        raise ValueError("Introduce una clave válida del proveedor seleccionado.")
    headers = {"Authorization": "Bearer " + value}
    if provider == "anthropic":
        headers = {"x-api-key": value, "anthropic-version": "2023-06-01"}
    elif provider == "gemini":
        headers = {"x-goog-api-key": value}
    try:
        with httpx.Client(timeout=20, trust_env=False, follow_redirects=False) as client:
            response = client.get(PROVIDERS[provider][1], headers=headers)
    except httpx.HTTPError:
        raise ValueError("No se pudo contactar al proveedor. Comprueba Internet y vuelve a intentar.") from None
    if response.status_code != 200:
        raise ValueError(f"El proveedor rechazó la comprobación (HTTP {response.status_code}). Revisa la clave, permisos o saldo.")


class OpenRouterFlow:
    """One-use PKCE callback on an ephemeral loopback port and random path."""
    def __init__(self):
        self.verifier = secrets.token_urlsafe(48)
        self.path = "/callback/" + secrets.token_urlsafe(32)
        self.code = None
        self.cancelled = False
        self.expires = time.monotonic() + 600
        flow = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                parsed = urlsplit(self.path)
                values = parse_qs(parsed.query)
                codes = values.get("code", [])
                valid = (parsed.path == flow.path and len(codes) == 1 and 1 <= len(codes[0]) <= 4096
                         and flow.code is None and not flow.cancelled and time.monotonic() < flow.expires)
                if valid:
                    flow.code = codes[0]
                self.send_response(200 if valid else 400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(("Vuelve a Agent Control para completar la conexión." if valid else "Autorización no válida.").encode())

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).rstrip(b"=").decode()
        self.url = "https://openrouter.ai/auth?" + urlencode({
            "callback_url": f"http://127.0.0.1:{self.server.server_port}{self.path}",
            "code_challenge": challenge, "code_challenge_method": "S256"})

    def poll(self) -> str | None:
        if self.cancelled or time.monotonic() >= self.expires:
            self.close()
            raise ValueError("La autorización caducó o fue cancelada. Iníciala de nuevo.")
        if self.code is None:
            return None
        code, self.code = self.code, None
        self.close()
        try:
            with httpx.Client(timeout=20, trust_env=False, follow_redirects=False) as client:
                result = client.post("https://openrouter.ai/api/v1/auth/keys", json={
                    "code": code, "code_verifier": self.verifier, "code_challenge_method": "S256"})
            if result.status_code != 200 or not isinstance(result.json().get("key"), str):
                raise ValueError("OpenRouter no completó la autorización. Inténtalo de nuevo.")
            return result.json()["key"]
        except httpx.HTTPError:
            raise ValueError("OpenRouter no respondió. Inicia una nueva autorización.") from None

    def close(self):
        self.cancelled = True
        self.server.shutdown()
        self.server.server_close()
