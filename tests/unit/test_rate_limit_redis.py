# -*- coding: utf-8 -*-
"""T-44 KP-P1-05：CORS 缺省不开放 + 限流 Redis 后端与 QUANT_RATE_LIMIT_BACKEND 切换。"""
import builtins
from pathlib import Path

from myquant.api.middleware.rate_limit import (
    FixedWindowLimiter,
    RedisFixedWindowLimiter,
    create_rate_limiter,
)

ROOT = Path(__file__).resolve().parents[2]
ROUTES = ROOT / "myquant" / "api" / "routes.py"


class FakeRedis:
    def __init__(self):
        self.d = {}

    def incr(self, k):
        self.d[k] = self.d.get(k, 0) + 1
        return self.d[k]

    def expire(self, k, t):
        return True

    def get(self, k):
        return self.d.get(k)

    def delete(self, k):
        self.d.pop(k, None)
        return True


def test_redis_limiter_allows_until_limit():
    lim = RedisFixedWindowLimiter(max_requests=2, window_seconds=60, redis_client=FakeRedis())
    r1 = lim.allow("ip1")
    assert r1.allowed and r1.remaining == 1
    r2 = lim.allow("ip1")
    assert r2.allowed and r2.remaining == 0
    r3 = lim.allow("ip1")
    assert not r3.allowed and r3.remaining == 0


def test_redis_limiter_reset():
    fake = FakeRedis()
    lim = RedisFixedWindowLimiter(max_requests=2, window_seconds=60, redis_client=fake)
    lim.allow("ip1")
    lim.allow("ip1")
    assert lim.get_remaining("ip1") == 0
    lim.reset("ip1")
    assert lim.get_remaining("ip1") == 2


def test_redis_limiter_bucket_isolation():
    lim = RedisFixedWindowLimiter(max_requests=5, window_seconds=60, redis_client=FakeRedis())
    assert lim._bucket(0.0) != lim._bucket(61.0)


def test_create_memory_default():
    lim = create_rate_limiter(max_requests=9, window_seconds=30)
    assert isinstance(lim, FixedWindowLimiter)
    assert lim.max_requests == 9


def test_create_redis_degrade_when_no_redis(monkeypatch):
    monkeypatch.setenv("QUANT_RATE_LIMIT_BACKEND", "redis")
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "redis":
            raise ImportError("no redis installed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    lim = create_rate_limiter(max_requests=5, window_seconds=60)
    assert isinstance(lim, FixedWindowLimiter)


def test_routes_cors_closed_default_src():
    text = ROUTES.read_text(encoding="utf-8")
    assert 'os.environ.get("QUANT_CORS_ORIGINS", "")' in text
    assert "if cors_origins:" in text


def test_routes_uses_create_rate_limiter_src():
    text = ROUTES.read_text(encoding="utf-8")
    assert "from myquant.api.middleware.rate_limit import create_rate_limiter" in text
    assert "_limiter = create_rate_limiter(" in text