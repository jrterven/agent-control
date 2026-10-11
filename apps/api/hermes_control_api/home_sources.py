"""Explicitly selected, owner-scoped, bounded read-only Home inputs."""
import asyncio
from datetime import datetime, timezone
import http.client
import json
import ssl
from urllib.parse import urlsplit

from sqlalchemy import select
from .home import digest, owned_resources, validate_sources
from .mail_providers import PlainHTML, public_socket, search_messages
from .mail_service import MailService
from .models import AutomationRun
from .services import GatewayService


class HomeUnavailable(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def public_text(url):
    """Connect to a validated pinned public address, preserving TLS/SNI.

    No cookies, credentials, proxies, redirects, subresource loads or compressed
    responses. DNS cannot change the address between validation and connect.
    """
    from .home_schemas import HomeSources
    HomeSources(urls=[url])
    parsed = urlsplit(url)
    host = parsed.hostname.encode('idna').decode('ascii')
    connection = http.client.HTTPSConnection(host, timeout=10)
    sock = public_socket(host, 443, timeout=10)
    try:
        connection.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        path = (parsed.path or '/') + ('?' + parsed.query if parsed.query else '')
        connection.request('GET', path, headers={'Accept': 'text/plain,text/html,application/rss+xml,application/atom+xml,application/xml', 'Accept-Encoding': 'identity', 'User-Agent': 'AgentControl-Home/1'})
        response = connection.getresponse()
        if response.status != 200 or response.getheader('Content-Encoding', 'identity') != 'identity':
            raise HomeUnavailable('HOME_PUBLIC_SOURCE_UNAVAILABLE')
        mime = response.getheader('Content-Type', '').split(';')[0].strip().lower()
        if mime not in {'text/plain', 'text/html', 'application/rss+xml', 'application/atom+xml', 'application/xml', 'text/xml'}:
            raise HomeUnavailable('HOME_PUBLIC_SOURCE_UNSUPPORTED')
        data = response.read(262145)
        if len(data) > 262144:
            raise HomeUnavailable('HOME_SOURCE_TOO_LARGE')
        value = data.decode('utf-8', errors='replace')
        if mime != 'text/plain':
            parser = PlainHTML()
            parser.feed(value)
            value = ''.join(parser.parts)
        return value[:6000]
    finally:
        connection.close()
        sock.close()


async def provider_for(db, services, profile):
    connection = await GatewayService(services).connection(db, profile.gateway_id, profile.profile_name)
    return await services.provider_pool.get(connection)


async def collect_sources(db, services, owner, sources):
    resources = owned_resources(db, owner, services.settings.deployment_mode == 'cloud')
    validate_sources(sources, resources)
    rows = []
    profiles = {(p.gateway_id, p.profile_name): p for p in resources['profiles']}
    selected = [s for s in resources['sessions'] if s.id in sources.session_ids or s.workspace_id in sources.workspace_ids]
    # Stable bounded selection, newest first; never resume a chat to read it.
    selected = selected[:40]
    for session in selected:
        profile = profiles.get((session.gateway_id, session.profile_name))
        if profile is None:
            raise HomeUnavailable('HOME_SOURCE_UNAVAILABLE')
        provider = await provider_for(db, services, profile)
        messages = await asyncio.wait_for(provider.history_readonly(session.stored_session_id), 15)
        text = '\n'.join(f"{m.get('role', '')}: {m.get('content', '')}" for m in messages[-12:] if m.get('role') in {'user', 'assistant'} and isinstance(m.get('content'), str))[-1800:]
        rows.append({'key': f'chat:{session.id}', 'title': session.display_title or session.title or 'Chat', 'text': text,
            'sessionId': session.id, 'action': 'follow_up'})
    for automation in resources['automations']:
        if automation.id not in sources.automation_ids:
            continue
        run = db.scalar(select(AutomationRun).where(AutomationRun.automation_id == automation.id).order_by(AutomationRun.created_at.desc()).limit(1))
        if run:
            rows.append({'key': f'automation:{automation.id}:{run.id}', 'title': automation.name,
                'text': f'{automation.name}\nstatus: {run.status}\ncompleted: {run.finished_at or ""}',
                'sessionId': run.session_link_id, 'action': 'repair' if run.status in {'failed', 'error'} else 'review'})
    mail = MailService(services)
    for account in resources['mailAccounts']:
        if account.id not in sources.mail_account_ids:
            continue
        secret = await mail.credentials(db, account)
        messages = await asyncio.wait_for(search_messages(account, secret, '', 8), 30)
        for message in messages:
            rows.append({'key': f'mail:{account.id}:{digest(message.get("threadId") or message["id"])}', 'title': message.get('subject', '')[:160],
                'text': '\n'.join(str(message.get(k) or '') for k in ('subject', 'senderAddress', 'receivedAt', 'snippet', 'bodyText'))[:1800],
                'action': 'reply', 'url': message.get('sourceUrl')})
    for url in sources.urls:
        rows.append({'key': f'url:{digest(url)}', 'title': url, 'url': url,
            'text': await asyncio.to_thread(public_text, url), 'action': 'review'})
    # Bind every citation to a source we actually retrieved; never take links from a model.
    result = []
    remaining = 36000
    seen = set()
    for row in rows:
        if row['key'] in seen:
            continue
        seen.add(row['key'])
        row['title'] = row['title'][:160]
        row['text'] = row['text'][:min(1800, max(0, remaining))]
        remaining -= len(json.dumps(row, ensure_ascii=False).encode())
        if remaining < 0:
            break
        if row.get('url') and not row['url'].startswith('https://'):
            row.pop('url')
        result.append(row)
    return result
