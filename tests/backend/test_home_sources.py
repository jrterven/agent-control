from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from hermes_control_api.home_sources import public_text, HomeUnavailable
from hermes_control_api.mail_providers import MailError


def test_public_panel_source_cannot_rebind_to_private_ip(monkeypatch):
    from hermes_control_api import mail_providers
    monkeypatch.setattr(mail_providers.socket, 'getaddrinfo', Mock(return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]))
    connect = Mock()
    monkeypatch.setattr(mail_providers.socket, 'socket', connect)
    with pytest.raises(MailError):
        public_text('https://news.example.com/feed')
    connect.assert_not_called()


def test_public_source_does_not_follow_redirect_or_execute_html(monkeypatch):
    from hermes_control_api import home_sources
    sock = Mock()
    response = SimpleNamespace(status=302, getheader=lambda *args: 'identity')
    connection = Mock(getresponse=Mock(return_value=response))
    monkeypatch.setattr(home_sources, 'public_socket', Mock(return_value=sock))
    monkeypatch.setattr(home_sources.ssl, 'create_default_context', lambda: SimpleNamespace(wrap_socket=lambda *a, **kw: sock))
    monkeypatch.setattr(home_sources.http.client, 'HTTPSConnection', Mock(return_value=connection))
    with pytest.raises(HomeUnavailable):
        public_text('https://news.example.com/feed')
    assert connection.request.call_count == 1
    assert connection.request.call_args.args[0] == 'GET'
    assert 'Authorization' not in connection.request.call_args.kwargs['headers']
    connection.close.assert_called_once()
