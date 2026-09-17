"""Verified TLS for standalone runtimes, independent of build-machine paths."""
import ssl

import certifi


def cloud_ssl_context() -> ssl.SSLContext:
    # Frozen Python's compiled OpenSSL CA path may only exist on the build
    # runner. Use the bundled trust roots that HTTPX also uses for pairing.
    return ssl.create_default_context(cafile=certifi.where())
