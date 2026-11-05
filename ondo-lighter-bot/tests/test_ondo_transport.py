import json
import socket
import ssl
from unittest.mock import Mock

import pytest

from olbot.ondo import transport
from olbot.market import MarketBoard
from olbot.ondo.ws import OndoPublicWS
from olbot.ondo.private_ws import OndoPrivateWS
from olbot.lighter.ws import LighterPublicWS
from olbot.verify import _ondo_ws_login


def test_ipv4_dns_and_failed_address_cleanup(monkeypatch):
    original_resolver = socket.getaddrinfo
    resolutions = []

    def resolve(host, port, family, kind):
        resolutions.append((host, port, family, kind))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port))
                for ip in ('192.0.2.1', '192.0.2.2')]

    sockets = [Mock(), Mock()]
    sockets[0].connect.side_effect = OSError('first address unavailable')
    factory = Mock(side_effect=sockets)
    # Patch the module's socket facade, never the process-wide resolver.
    monkeypatch.setattr(transport, 'socket', Mock(AF_INET=socket.AF_INET, SOCK_STREAM=socket.SOCK_STREAM,
                        _GLOBAL_DEFAULT_TIMEOUT=socket._GLOBAL_DEFAULT_TIMEOUT,
                        getaddrinfo=resolve, socket=factory))
    connected = transport.create_ipv4_connection(('api.ondoperps.xyz', 443), 7, ('127.0.0.1', 0))
    assert connected is sockets[1]
    assert resolutions == [('api.ondoperps.xyz', 443, socket.AF_INET, socket.SOCK_STREAM)]
    sockets[0].close.assert_called_once()
    sockets[1].connect.assert_called_once_with(('192.0.2.2', 443))
    sockets[1].settimeout.assert_called_once_with(7)
    sockets[1].bind.assert_called_once_with(('127.0.0.1', 0))
    assert socket.getaddrinfo is original_resolver


def test_no_ipv4_address_fails_without_ipv6_fallback(monkeypatch):
    factory = Mock()
    monkeypatch.setattr(transport, 'socket', Mock(AF_INET=socket.AF_INET, SOCK_STREAM=socket.SOCK_STREAM,
                        getaddrinfo=Mock(return_value=[]), socket=factory))
    with pytest.raises(OSError, match='IPv4'):
        transport.create_ipv4_connection(('api.ondoperps.xyz', 443), 5)
    factory.assert_not_called()


def test_ipv4_tls_still_validates_original_hostname(monkeypatch):
    raw_socket = Mock()
    create = Mock(return_value=raw_socket)
    monkeypatch.setattr(transport, 'create_ipv4_connection', create)
    wrapped = []

    def wrap(context, sock, **kwargs):
        wrapped.append((context.check_hostname, context.verify_mode, kwargs['server_hostname']))
        assert sock is raw_socket
        return Mock()

    monkeypatch.setattr(ssl.SSLContext, 'wrap_socket', wrap)
    connection = transport.IPv4HTTPSConnection('api.ondoperps.xyz', timeout=9)
    connection.connect()
    assert create.call_args.args[:2] == (('api.ondoperps.xyz', 443), 9)
    assert wrapped == [(True, ssl.CERT_REQUIRED, 'api.ondoperps.xyz')]
    connection.close()


def test_authenticated_rest_does_not_inherit_proxy_or_follow_redirects(monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    import urllib.error
    import urllib.request

    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:9999')
    opener = transport.rest_opener()
    assert not any(isinstance(h, urllib.request.ProxyHandler) and h.proxies for h in opener.handlers)
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.path)
            self.send_response(302)
            self.send_header('Location', '/must-not-follow')
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/balance',
                                         headers={'ONDO-KEY-ID': 'test-id'})
        with pytest.raises(urllib.error.HTTPError) as error:
            opener.open(request, timeout=2)
        assert error.value.code == 302
        assert received == ['/balance']
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_ondo_options_do_not_change_lighter():
    board = MarketBoard(['LIT'])
    public = OndoPublicWS('wss://api.ondoperps.xyz/ws', board, 30)
    private = OndoPrivateWS('wss://api.ondoperps.xyz/ws', 'test-id', 'test-secret', ['LIT-USD.P'],
                            30, 10, lambda fill: None)
    lighter = LighterPublicWS('wss://mainnet.zklighter.elliot.ai/stream', board, {'LIT': 1}, 60)
    assert public.connect_options() == private.connect_options() == {'family': socket.AF_INET, 'proxy': None}
    assert lighter.connect_options() == {}


async def test_reconnecting_ws_keeps_ipv4_on_every_connection(monkeypatch):
    from olbot import wsbase

    client = OndoPublicWS('wss://api.ondoperps.xyz/ws', MarketBoard(['LIT']), 30)
    calls = []

    class FakeSocket:
        async def send(self, raw):
            pass

        async def recv(self):
            if len(calls) == 1:
                raise OSError('connection lost')
            client.stop()
            return '{}'

    class Connection:
        async def __aenter__(self):
            return FakeSocket()

        async def __aexit__(self, *args):
            return False

    def connect(url, **kwargs):
        calls.append(kwargs)
        assert kwargs['family'] == socket.AF_INET
        assert kwargs['proxy'] is None
        return Connection()

    monkeypatch.setattr(wsbase.websockets, 'connect', connect)
    await client.run()
    assert len(calls) == 2


async def test_verify_ws_is_ipv4_login_only(monkeypatch):
    from olbot import verify

    sent = []

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def send(self, raw):
            sent.append(json.loads(raw))

        async def recv(self):
            return json.dumps({'type': 'loggedIn', 'msg': 'Login successful'})

    def connect(url, **kwargs):
        assert kwargs['family'] == socket.AF_INET and kwargs['proxy'] is None
        return Connection()

    monkeypatch.setattr(verify.websockets, 'connect', connect)
    assert await _ondo_ws_login('wss://api.ondoperps.xyz/ws', 'test-id', 'test-secret') == 'ok'
    assert len(sent) == 1 and sent[0]['op'] == 'login'
