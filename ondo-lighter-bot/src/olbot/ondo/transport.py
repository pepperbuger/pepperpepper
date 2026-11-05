"""Ondo IP 허용목록은 IPv4 기준이므로 이 클라이언트의 연결만 IPv4로 한다.

TLS 호스트 검증은 표준 HTTPSConnection에 맡긴다. 전역 DNS·OS 설정은 바꾸지 않는다.
"""

from __future__ import annotations

import http.client
import socket
import urllib.request


def create_ipv4_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
    last_error = None
    for family, kind, proto, _, sockaddr in socket.getaddrinfo(
        address[0], address[1], socket.AF_INET, socket.SOCK_STREAM
    ):
        sock = socket.socket(family, kind, proto)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as error:
            sock.close()
            last_error = error
    if last_error is not None:
        raise last_error
    raise OSError("Ondo 호스트의 IPv4 주소를 찾지 못함")


class IPv4HTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = create_ipv4_connection


class IPv4HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(IPv4HTTPSConnection, request, context=self._context)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def rest_opener():
    # 인증 헤더가 프록시·리디렉션 대상으로 전달되지 않게 직접 연결한다.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), IPv4HTTPSHandler(), NoRedirect())


def ws_connect_options() -> dict:
    return {"family": socket.AF_INET, "proxy": None}
