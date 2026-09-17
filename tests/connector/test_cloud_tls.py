"""Exercise the connector's real WSS handshake without system CA files."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import ssl
import urllib.error
from unittest.mock import AsyncMock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from websockets.asyncio.server import serve

from agent_control_connector import manage, runtime as module, tls
from agent_control_connector.cli import status
from agent_control_connector.runtime import ConnectorRuntime
from hermes_client import InMemoryHermesProvider
from hermes_client.connector_protocol import FrameReader, frames


@pytest.fixture
def local_tls(tmp_path):
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Connector test CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, None, None), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    certificate = (x509.CertificateBuilder()
                   .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
                   .issuer_name(ca_name).public_key(key.public_key())
                   .serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
                   .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                   .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
                   .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
                   .sign(ca_key, hashes.SHA256()))
    ca_path, cert_path, key_path = (tmp_path / name for name in ("ca.pem", "server.pem", "key.pem"))
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return ca_path, context


def connector(tmp_path, server):
    return ConnectorRuntime(tmp_path / "connector", {
        "gatewayId": "gateway", "profiles": ["default"], "server": server,
        "restUrl": "http://127.0.0.1:9119", "wsUrl": "ws://127.0.0.1:9119/api/ws",
        "sourceSha": "f" * 40, "hermesHome": str(tmp_path),
    }, {"hermesToken": "local-test-token", "accessToken": "cloud-test-token"}, InMemoryHermesProvider)


@pytest.mark.asyncio
@pytest.mark.parametrize("hostname,trusted,accepted", [
    ("localhost", True, True),
    ("127.0.0.1", True, False),
    ("localhost", False, False),
])
async def test_wss_uses_bundled_roots_and_verifies_identity(tmp_path, monkeypatch, local_tls, hostname, trusted, accepted):
    ca_path, server_context = local_tls
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing-build-runner-ca.pem"))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "missing-system-ca-directory"))
    if trusted:
        monkeypatch.setattr(tls.certifi, "where", lambda: str(ca_path))
    received = []

    async def peer(websocket):
        for frame in frames({"v": 1, "type": "welcome", "gatewayId": "gateway", "profiles": ["default"]}):
            await websocket.send(frame)
        received.append(FrameReader().feed(await websocket.recv()))

    async with serve(peer, "127.0.0.1", 0, ssl=server_context) as server:
        port = server.sockets[0].getsockname()[1]
        runtime = connector(tmp_path, f"https://{hostname}:{port}")
        runtime.connection_error = "CLOUD_CONNECTION_FAILED"
        try:
            if accepted:
                await asyncio.wait_for(runtime._connection(), 5)
                assert received[0]["type"] == "event"
                assert runtime.connection_error is None
            else:
                with pytest.raises(ssl.SSLCertVerificationError):
                    await asyncio.wait_for(runtime._connection(), 5)
                assert not received
        finally:
            runtime.ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("hostname,trusted,accepted", [
    ("localhost", True, True),
    ("127.0.0.1", True, False),
    ("localhost", False, False),
])
async def test_updater_uses_bundled_roots_and_verifies_identity(tmp_path, monkeypatch, local_tls, hostname, trusted, accepted):
    ca_path, server_context = local_tls
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing-build-runner-ca.pem"))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "missing-system-ca-directory"))
    if trusted:
        monkeypatch.setattr(tls.certifi, "where", lambda: str(ca_path))

    async def peer(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\nConnection: close\r\n\r\nr1\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async with await asyncio.start_server(peer, "127.0.0.1", 0, ssl=server_context) as server:
        port = server.sockets[0].getsockname()[1]
        destination = tmp_path / "VERSION"
        download = asyncio.to_thread(manage.download, f"https://{hostname}:{port}/VERSION", destination, 128)
        if accepted:
            await asyncio.wait_for(download, 5)
            assert destination.read_bytes() == b"r1\n"
        else:
            with pytest.raises(urllib.error.URLError) as error:
                await asyncio.wait_for(download, 5)
            assert isinstance(error.value.reason, ssl.SSLCertVerificationError)
            assert not destination.exists()


@pytest.mark.asyncio
async def test_retry_failure_is_reported_without_exception_secrets(tmp_path, monkeypatch):
    runtime = connector(tmp_path, "https://control.test")

    async def unavailable():
        runtime.closed = True
        raise ssl.SSLCertVerificationError(1, "private-token-in-error: cloud-test-token /private/path")

    monkeypatch.setattr(runtime, "_connection", unavailable)
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    await runtime.run()
    report = status(runtime.directory)
    assert report["connectionError"] == "CLOUD_TLS_CERTIFICATE_INVALID"
    assert report["connected"] is False
    stored = json.dumps(report)
    assert "private-token-in-error" not in stored and "cloud-test-token" not in stored and "/private/path" not in stored
