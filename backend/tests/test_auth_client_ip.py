"""_client_ip 的 X-Forwarded-For 采信规则。

反代 (Nginx proxy_add_x_forwarded_for) 把对端地址追加到客户端自带的 XFF 之后,
最左项由客户端任意填写。若采信最左项, 公网请求伪造 `X-Forwarded-For: 127.0.0.1`
即可冒充本机: 未设密码时被放行并抢占 setup, 已设密码时绕过登录限流。
"""
from __future__ import annotations

from starlette.requests import Request

from app.api.auth import _client_ip, _is_local_network


def _request(peer: str, xff: str | None = None) -> Request:
    headers = [] if xff is None else [(b"x-forwarded-for", xff.encode())]
    return Request({"type": "http", "headers": headers, "client": (peer, 12345)})


def test_spoofed_leftmost_xff_behind_proxy_is_ignored() -> None:
    # 客户端自带 "127.0.0.1", 反代追加真实公网地址
    ip = _client_ip(_request("127.0.0.1", "127.0.0.1, 203.0.113.9"))
    assert ip == "203.0.113.9"
    assert not _is_local_network(ip)


def test_single_hop_xff_behind_proxy() -> None:
    assert _client_ip(_request("127.0.0.1", "203.0.113.9")) == "203.0.113.9"
    assert _client_ip(_request("127.0.0.1", "192.168.1.20")) == "192.168.1.20"


def test_xff_ignored_when_peer_is_public() -> None:
    assert _client_ip(_request("203.0.113.9", "127.0.0.1")) == "203.0.113.9"


def test_no_xff_uses_direct_peer() -> None:
    assert _client_ip(_request("192.168.1.20")) == "192.168.1.20"


def test_empty_xff_falls_back_to_direct_peer() -> None:
    assert _client_ip(_request("127.0.0.1", " , ")) == "127.0.0.1"
