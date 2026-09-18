"""Read-only post-release checks. Never prints response bodies or credentials."""
from __future__ import annotations

import argparse
import json
import re
from html.parser import HTMLParser
import urllib.parse
import urllib.request


class PwaHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.manifest = None
        self.assets: set[str] = set()
        self.icons: set[str] = set()

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if tag == "script" and attributes.get("src"):
            self.assets.add(attributes["src"])
        if tag != "link" or not attributes.get("href"):
            return
        relations = set(attributes.get("rel", "").split())
        if "manifest" in relations:
            self.manifest = attributes["href"]
        if relations & {"stylesheet", "modulepreload"}:
            self.assets.add(attributes["href"])
        if relations & {"icon", "apple-touch-icon"}:
            self.icons.add(attributes["href"])


def verify(origin: str, *, registration_mode: str | None = None,
           beta_max_users: int | None = None) -> None:
    parsed = urllib.parse.urlsplit(origin)
    if parsed.scheme != "https" or not parsed.hostname or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username:
        raise ValueError("Supply an HTTPS origin")
    origin = origin.rstrip("/")

    def fetch(path: str, limit: int = 2_000_000) -> tuple[bytes, str]:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Asset must have a same-origin path")
        with urllib.request.urlopen(origin + path, timeout=15) as response:
            final = urllib.parse.urlsplit(response.url)
            if (final.scheme, final.netloc) != (parsed.scheme, parsed.netloc):
                raise ValueError("Unexpected cross-origin redirect")
            body = response.read(limit + 1)
            if len(body) > limit:
                raise ValueError("Response too large")
            return body, response.headers.get_content_type()

    health, _ = fetch("/api/v1/health")
    if json.loads(health).get("status") != "ok":
        raise ValueError("Health check failed")
    ready, _ = fetch("/api/v1/ready")
    if json.loads(ready).get("status") != "ready":
        raise ValueError("Readiness check failed")
    methods, _ = fetch("/api/v1/auth/methods")
    authentication = json.loads(methods)
    if authentication.get("mode") != "cloud" or authentication.get("googleEnabled") is not True:
        raise ValueError("Cloud Google sign-in is not enabled")
    if registration_mode is not None and authentication.get("registrationMode") != registration_mode:
        raise ValueError("Cloud registration policy does not match the release")
    if beta_max_users is not None and authentication.get("betaMaxUsers") != beta_max_users:
        raise ValueError("Cloud registration capacity does not match the release")
    html, kind = fetch("/")
    if kind != "text/html":
        raise ValueError("PWA HTML is missing")
    page = PwaHTML()
    page.feed(html.decode("utf-8"))
    if not page.manifest:
        raise ValueError("PWA manifest link is missing")
    manifest_path = urllib.parse.urljoin("/", page.manifest)
    manifest_data, _ = fetch(manifest_path)
    manifest = json.loads(manifest_data)
    start = str(manifest.get("start_url", ""))
    if not manifest.get("name") or not start.startswith("/") or start.startswith("//") or not manifest.get("icons"):
        raise ValueError("Invalid PWA manifest")
    for icon in manifest["icons"]:
        page.icons.add(urllib.parse.urljoin(manifest_path, icon["src"]))
    for icon in page.icons:
        data, kind = fetch(icon)
        if not data or not kind.startswith("image/"):
            raise ValueError("PWA icon is missing")
    if not any(path.startswith("/assets/") for path in page.assets):
        raise ValueError("Production bundle is missing")
    for path in page.assets:
        data, kind = fetch(path, limit=10_000_000)
        if not data or kind == "text/html":
            raise ValueError("Production asset is missing")
    worker, kind = fetch("/sw.js")
    if not worker or kind == "text/html":
        raise ValueError("Service worker is missing")
    # Workbox uses an AMD loader; checking sw.js alone can miss a missing runtime
    # chunk or notification worker that prevents installation on a fresh device.
    source = worker.decode("utf-8")
    imports = set(re.findall(r'''importScripts\(\s*["']([^"']+)["']''', source))
    imports.update(path + ".js" for path in re.findall(r'''["'](\./workbox-[A-Za-z0-9_-]+)["']''', source))
    for path in imports:
        data, kind = fetch(urllib.parse.urljoin("/sw.js", path))
        if not data or kind == "text/html":
            raise ValueError("Service worker dependency is missing")
    print("Verified cloud sign-in configuration, health, readiness, PWA HTML, manifest, icons, bundles and service worker dependencies.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("origin")
    parser.add_argument("--registration-mode", choices=("invite_only", "open"))
    parser.add_argument("--beta-max-users", type=int, choices=range(1, 21))
    args = parser.parse_args()
    verify(args.origin, registration_mode=args.registration_mode, beta_max_users=args.beta_max_users)
