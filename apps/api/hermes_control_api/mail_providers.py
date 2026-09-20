"""Bounded provider adapters. Never follow user-supplied URLs or disable TLS."""
from __future__ import annotations

import asyncio
import base64
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import make_msgid, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
import imaplib
import ipaddress
import json
import re
import smtplib
import socket
import ssl
from urllib.parse import quote

import httpx


class MailError(Exception):
    def __init__(self, code="MAIL_PROVIDER_UNAVAILABLE", status=502):
        self.code, self.status = code, status
        super().__init__(code)


async def http_json(method, url, *, token=None, data=None, payload=None, raw=None):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if raw is not None:
        headers["Content-Type"] = "text/plain"
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
            async with client.stream(method, url, headers=headers, data=data, json=payload, content=raw) as response:
                if response.status_code in {401, 403}:
                    raise MailError("MAIL_RECONNECT_REQUIRED", 409)
                if response.status_code == 429:
                    raise MailError("MAIL_RATE_LIMITED", 429)
                if not 200 <= response.status_code < 300:
                    raise MailError()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 2 * 1024 * 1024:
                        raise MailError("MAIL_MESSAGE_TOO_LARGE", 413)
                return json.loads(body) if body else {}
    except MailError:
        raise
    except (httpx.HTTPError, ValueError, UnicodeError):
        raise MailError() from None


def public_socket(host: str, port: int, timeout=15):
    """Resolve once, validate every result and connect to the pinned address."""
    try:
        host = host.encode("idna").decode("ascii")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}", host):
            raise MailError("MAIL_INVALID_HOST", 422)
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        def allowed(value):
            address = ipaddress.ip_address(value)
            return address.is_global and not address.is_multicast and not address.is_reserved and not (
                address.version == 6 and (address.sixtofour or address.teredo or address in ipaddress.ip_network("64:ff9b::/96")))
        if not addresses or any(not allowed(row[4][0]) for row in addresses):
            raise MailError("MAIL_INVALID_HOST", 422)
        family, kind, proto, _, target = addresses[0]
        sock = socket.socket(family, kind, proto)
        sock.settimeout(timeout)
        try:
            sock.connect(target)
            return sock
        except BaseException:
            sock.close()
            raise
    except MailError:
        raise
    except (OSError, ValueError, UnicodeError):
        raise MailError() from None


class PublicIMAP(imaplib.IMAP4_SSL):
    def read(self, size):
        if size > 131072:
            raise MailError("MAIL_MESSAGE_TOO_LARGE", 413)
        return super().read(size)

    def _create_socket(self, timeout):
        return self.ssl_context.wrap_socket(public_socket(self.host, self.port, timeout), server_hostname=self.host)


class PublicSMTP(smtplib.SMTP):
    def _get_socket(self, host, port, timeout):
        return public_socket(host, port, timeout)


class PublicSMTPSSL(smtplib.SMTP_SSL):
    def _get_socket(self, host, port, timeout):
        return self.context.wrap_socket(public_socket(host, port, timeout), server_hostname=host)


class PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"p", "br", "div", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_html(value):
    parser = PlainHTML()
    parser.feed(value[:131072])
    return "".join(parser.parts)[:12000]


def received_at(value):
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError, IndexError):
        return None


def message_view(raw, identifier, address, provider="imap"):
    message = BytesParser(policy=policy.default).parsebytes(raw)
    body = message.get_body(preferencelist=("plain", "html"))
    try:
        text = body.get_content() if body else ""
    except (LookupError, UnicodeError):
        text = ""
    if not isinstance(text, str):
        text = ""
    if body and body.get_content_type() == "text/html":
        text = plain_html(text)
    return {"id": identifier, "provider": provider, "accountAddress": address,
            "subject": str(message.get("Subject", ""))[:500], "senderName": parseaddr(str(message.get("From", "")))[0][:500], "senderAddress": parseaddr(str(message.get("From", "")))[1][:320],
            "receivedAt": received_at(str(message.get("Date", ""))), "messageId": str(message.get("Message-ID", ""))[:1000],
            "bodyText": text[:12000], "snippet": text[:240], "truncated": len(raw) >= 131072}


def imap_work(config, secret, action, arguments):
    try:
        with PublicIMAP(config["imapHost"], 993, ssl_context=ssl.create_default_context(), timeout=15) as client:
            client.login(config["username"], secret["password"])
            if action == "test":
                client.noop()
                with smtp_client(config, secret) as sender:
                    sender.noop()
                return {"ok": True}
            status, _ = client.select("INBOX", readonly=True)
            if status != "OK":
                raise MailError()
            validity = (client.response("UIDVALIDITY")[1] or [b""])[0].decode()
            if not validity.isdigit():
                raise MailError()
            if action == "read":
                parts = arguments["messageId"].split(":")
                if len(parts) != 2 or parts[0] != validity or not parts[1].isdigit():
                    raise MailError("MAIL_MESSAGE_NOT_FOUND", 404)
                uids = [parts[1].encode()]
            else:
                text = arguments.get("text", "")
                if text:
                    client.literal = text.encode("utf-8")
                    status, rows = client.uid("SEARCH", "CHARSET", "UTF-8", "TEXT")
                else:
                    status, rows = client.uid("SEARCH", None, "ALL")
                if status != "OK":
                    raise MailError()
                uids = (rows[0] or b"").split()[-arguments.get("limit", 10):]
                if not all(uid.isdigit() for uid in uids):
                    raise MailError()
            if not uids:
                return []
            section = "BODY.PEEK[]<0.131072>" if action == "read" else "BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE MESSAGE-ID)]<0.8192>"
            status, rows = client.uid("FETCH", b",".join(uids), f"(UID {section})")
            if status != "OK":
                raise MailError()
            result = []
            for row in rows:
                if not isinstance(row, tuple) or len(row) != 2 or not isinstance(row[1], bytes):
                    continue
                uid = re.search(rb"\bUID (\d+)\b", row[0])
                if uid and uid.group(1) in uids:
                    result.append({**message_view(row[1][:131072], validity + ":" + uid.group(1).decode(), config["address"]), "uid": uid.group(1).decode(), "mailbox": "INBOX"})
            if action == "read":
                if not result:
                    raise MailError("MAIL_MESSAGE_NOT_FOUND", 404)
                return result[0]
            return result[::-1]
    except MailError:
        raise
    except (imaplib.IMAP4.error, smtplib.SMTPAuthenticationError):
        raise MailError("MAIL_RECONNECT_REQUIRED", 409) from None
    except (OSError, smtplib.SMTPException, ValueError, UnicodeError):
        raise MailError() from None


def smtp_client(config, secret):
    client = None
    try:
        if config["smtpPort"] == 465:
            client = PublicSMTPSSL(config["smtpHost"], 465, timeout=15, context=ssl.create_default_context())
        else:
            client = PublicSMTP(config["smtpHost"], 587, timeout=15)
            client.ehlo()
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
        client.login(config["username"], secret["password"])
        return client
    except BaseException:
        if client:
            client.close()
        raise


def smtp_send(config, secret, message, recipients):
    try:
        with smtp_client(config, secret) as client:
            refused = client.send_message(message, from_addr=config["address"], to_addrs=recipients)
            if refused:
                # Some recipients may already have accepted: do not repeat.
                raise MailError("MAIL_DELIVERY_UNKNOWN", 409)
    except MailError:
        raise
    except Exception:
        raise MailError("MAIL_DELIVERY_UNKNOWN", 409) from None
    return {"status": "accepted"}


async def identity(provider, token):
    if provider == "gmail":
        who = await http_json("GET", "https://openidconnect.googleapis.com/v1/userinfo", token=token)
        mailbox = await http_json("GET", "https://gmail.googleapis.com/gmail/v1/users/me/profile", token=token)
        return str(who["sub"]), str(mailbox["emailAddress"])
    who = await http_json("GET", "https://graph.microsoft.com/v1.0/me?$select=id,mail,userPrincipalName", token=token)
    return str(who["id"]), str(who.get("mail") or who["userPrincipalName"])


def outlook_view(row, account):
    body = row.get("body", {})
    text = str(body.get("content", ""))
    if body.get("contentType", "").lower() == "html":
        text = plain_html(text)
    return {"id": row["id"], "provider": "outlook", "accountAddress": account.address,
            "subject": str(row.get("subject", ""))[:500],
            "senderAddress": row.get("from", {}).get("emailAddress", {}).get("address", ""),
            "receivedAt": row.get("receivedDateTime"), "messageId": row.get("internetMessageId"),
            "sourceUrl": row.get("webLink"), "snippet": str(row.get("bodyPreview", ""))[:240], "bodyText": text[:12000]}


def gmail_view(row, account):
    headers = {item["name"].lower(): item["value"] for item in row.get("payload", {}).get("headers", [])}
    parts, plain, html = [row.get("payload", {})], [], []
    visited = 0
    while parts and visited < 100:
        part = parts.pop(0)
        visited += 1
        if part.get("filename"):
            continue
        parts.extend(part.get("parts", [])[:100])
        data = part.get("body", {}).get("data", "")
        if data and part.get("mimeType") in {"text/plain", "text/html"}:
            try:
                text = base64.urlsafe_b64decode(data[:174764] + "===").decode("utf-8", errors="replace")
            except ValueError:
                text = ""
            (plain if part["mimeType"] == "text/plain" else html).append(text)
    text = "\n".join(plain) if plain else plain_html("\n".join(html))
    return {"id": row["id"], "threadId": row.get("threadId"), "provider": "gmail", "accountAddress": account.address,
            "subject": headers.get("subject", "")[:500], "senderName": parseaddr(headers.get("from", ""))[0][:500], "senderAddress": parseaddr(headers.get("from", ""))[1][:320],
            "receivedAt": received_at(headers.get("date")), "messageId": headers.get("message-id"),
            "snippet": str(row.get("snippet", ""))[:240], "bodyText": text[:12000]}


async def read_message(account, secret, identifier):
    if account.provider in {"hostinger", "imap"}:
        return await asyncio.to_thread(imap_work, account.config, secret, "read", {"messageId": identifier})
    encoded = quote(identifier, safe="")
    if account.provider == "gmail":
        row = await http_json("GET", f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{encoded}?format=full", token=secret["access_token"])
        return gmail_view(row, account)
    row = await http_json("GET", f"https://graph.microsoft.com/v1.0/me/messages/{encoded}?$select=id,subject,from,receivedDateTime,internetMessageId,body,bodyPreview,webLink", token=secret["access_token"])
    return outlook_view(row, account)


async def search_messages(account, secret, text, limit):
    if account.provider in {"hostinger", "imap"}:
        return await asyncio.to_thread(imap_work, account.config, secret, "search", {"text": text, "limit": limit})
    if account.provider == "gmail":
        listing = await http_json("GET", f"https://gmail.googleapis.com/gmail/v1/users/me/messages?maxResults={limit}&q={quote(text, safe='')}", token=secret["access_token"])
        result = []
        for item in listing.get("messages", [])[:limit]:
            row = await http_json("GET", f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{quote(item['id'], safe='')}?format=metadata", token=secret["access_token"])
            result.append(gmail_view(row, account))
        return result
    query = f"$top={limit}&$select=id,subject,from,receivedDateTime,internetMessageId,bodyPreview,webLink"
    if text:
        query += "&$search=" + quote(json.dumps(text), safe="")
    rows = await http_json("GET", "https://graph.microsoft.com/v1.0/me/messages?" + query, token=secret["access_token"])
    return [outlook_view(row, account) for row in rows.get("value", [])[:limit]]


async def send_message(account, secret, arguments):
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = account.address, ", ".join(arguments.to), arguments.subject
    message["Message-ID"] = make_msgid(idstring=str(arguments.operation_id), domain="mail.agentcontrol.invalid")
    message.set_content(arguments.body)
    original = None
    if arguments.reply_to_message_id:
        original = await read_message(account, secret, arguments.reply_to_message_id)
        reference = original.get("messageId")
        if reference and not any(ord(c) < 32 for c in reference):
            message["In-Reply-To"] = reference
            message["References"] = reference
    if account.provider in {"hostinger", "imap"}:
        return await asyncio.to_thread(smtp_send, account.config, secret, message, arguments.to)
    raw = message.as_bytes(policy=policy.SMTP)
    if account.provider == "gmail":
        payload = {"raw": base64.urlsafe_b64encode(raw).decode()}
        if original and original.get("threadId"):
            payload["threadId"] = original["threadId"]
        await http_json("POST", "https://gmail.googleapis.com/gmail/v1/users/me/messages/send", token=secret["access_token"], payload=payload)
    else:
        await http_json("POST", "https://graph.microsoft.com/v1.0/me/sendMail", token=secret["access_token"], raw=base64.b64encode(raw))
    return {"status": "accepted"}
