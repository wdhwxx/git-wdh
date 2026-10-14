# 文件名：myquant/core/cache.py
"""
cache.py - 计算结果缓存
========================
功能:
  1. 内存 LRU 缓存 (带TTL过期)
  2. Redis 缓存适配 (可选)
  3. 装饰器模式 @cached
  4. 缓存统计 (命中率)
  5. 线程安全

依赖: time, threading, hashlib, json, functools
可选: redis
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from functools import wraps
from typing import Any, Callable, Dict, Optional, Tuple

from myquant.core.constants import CACHE_DEFAULT_TTL, CACHE_MAX_SIZE

# ═══════════════════════════════════════════════
#  内存 LRU 缓存
# ═══════════════════════════════════════════════

class MemoryCache:
    """
    线程安全的内存 LRU 缓存，支持 TTL 过期

    Usage:
        cache = MemoryCache(max_size=512, default_ttl=300)
        cache.set("key1", {"data": 123})
        val = cache.get("key1")  # {"data": 123}

        # 装饰器用法
        @cache.memoize(ttl=60)
        def expensive_calc(x, y):
            return x ** y
    """

    def __init__(
        self,
        max_size: int = CACHE_MAX_SIZE,
        default_ttl: int = CACHE_DEFAULT_TTL,
    ):
        self._max_size = max_size
        self._default_ttl = default_ttl
        self._store: OrderedDict[str, Tuple[Any, float]] = OrderedDict()  # key -> (value, expire_ts)
        self._lock = threading.RLock()

        # 统计
        self._hits = 0
        self._misses = 0

    # ── 核心操作 ──
    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            if key not in self._store:
                self._misses += 1
                return default
            value, expire_ts = self._store[key]
            if expire_ts > 0 and time.time() > expire_ts:
                del self._store[key]
                self._misses += 1
                return default
            # LRU: 移到末尾
            self._store.move_to_end(key)
            self._hits += 1
            return value

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        with self._lock:
            if ttl is None:
                ttl = self._default_ttl
            expire_ts = time.time() + ttl if ttl > 0 else 0
            self._store[key] = (value, expire_ts)
            self._store.move_to_end(key)
            # 淘汰最旧
            while len(self._store) > self._max_size:
                self._store.popitem(last=False)

    def delete(self, key: str) -> bool:
        with self._lock:
            if key in self._store:
                del self._store[key]
                return True
            return False

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self._hits = 0
            self._misses = 0

    def exists(self, key: str) -> bool:
        return self.get(key) is not None

    def keys(self) -> list:
        with self._lock:
            return list(self._store.keys())

    # ── 批量操作 ──
    def mget(self, keys: list, default: Any = None) -> Dict[str, Any]:
        return {k: self.get(k, default) for k in keys}

    def mset(self, mapping: Dict[str, Any], ttl: Optional[int] = None) -> None:
        for k, v in mapping.items():
            self.set(k, v, ttl)

    # ── 装饰器 ──
    def memoize(self, ttl: Optional[int] = None, key_prefix: str = ""):
        """
        缓存装饰器

        @cache.memoize(ttl=60)
        def calc_indicator(symbol, period):
            ...
        """
        def decorator(func: Callable) -> Callable:
            @wraps(func)
            def wrapper(*args, **kwargs):
                cache_key = self._build_key(key_prefix or func.__name__, args, kwargs)
                result = self.get(cache_key)
                if result is not None:
                    return result
                result = func(*args, **kwargs)
                if result is not None:
                    self.set(cache_key, result, ttl)
                return result
            wrapper.cache_clear = lambda: self.clear()
            wrapper.cache_key = lambda *a, **kw: self._build_key(key_prefix or func.__name__, a, kw)
            return wrapper
        return decorator

    # ── 统计 ──
    @property
    def stats(self) -> Dict[str, Any]:
        total = self._hits + self._misses
        return {
            "size": len(self._store),
            "max_size": self._max_size,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": self._hits / total if total > 0 else 0.0,
        }

    @property
    def size(self) -> int:
        return len(self._store)

    # ── 内部 ──
    @staticmethod
    def _build_key(prefix: str, args: tuple, kwargs: dict) -> str:
        # L1修复: 规范化序列化——json sort_keys 消除 dict 无序碰撞（同值不同 str → 假 miss）；
        # default=str 兜底非 JSON 类型；消除分隔符歧义（原 f-string 拼接在参数含逗号+空格时
        # 碰撞），并修复混合类型 kwargs 排序直接抛 TypeError 的问题
        payload = json.dumps(
            {"p": prefix, "a": args, "k": kwargs},
            sort_keys=True,
            default=str,
        )
        return hashlib.md5(payload.encode()).hexdigest()

    def __len__(self) -> int:
        return len(self._store)

    def __repr__(self) -> str:
        return f"MemoryCache(size={len(self._store)}, max={self._max_size})"


# ═══════════════════════════════════════════════
#  Redis 缓存适配 (可选)
# ═══════════════════════════════════════════════

class RedisCache:
    """
    Redis 缓存适配器
    接口与 MemoryCache 对齐，方便切换

    需要: pip install redis
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6379,
        db: int = 0,
        prefix: str = "myquant:",
        default_ttl: int = CACHE_DEFAULT_TTL,
        **redis_kwargs,
    ):
        try:
            import redis
        except ImportError:
            raise ImportError("Redis缓存需要安装: pip install redis")

        self._prefix = prefix
        self._default_ttl = default_ttl
        # L2修复: 默认连接/读写超时（Redis 挂起时不再无限阻塞；可经 redis_kwargs 覆盖）
        redis_kwargs.setdefault("socket_connect_timeout", 5.0)
        redis_kwargs.setdefault("socket_timeout", 5.0)
        self._client = redis.Redis(
            host=host, port=port, db=db,
            decode_responses=True,
            **redis_kwargs,
        )
        # 连接测试（超时兜底，启动即失败而非挂死）
        try:
            self._client.ping()
        except Exception as e:
            raise ConnectionError(
                f"Redis 连接失败（host={host}:{port} db={db}）: {e}"
            ) from e

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def get(self, key: str, default: Any = None) -> Any:
        raw = self._client.get(self._key(key))
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        ttl = ttl or self._default_ttl
        serialized = json.dumps(value, default=str)
        self._client.setex(self._key(key), ttl, serialized)

    def delete(self, key: str) -> bool:
        return bool(self._client.delete(self._key(key)))

    def clear(self) -> None:
        # L6修复: KEYS 阻塞式全量扫描 → SCAN 分批游标删除（大 key 空间不阻塞 Redis 主线程）
        cursor = 0
        while True:
            cursor, keys = self._client.scan(
                cursor=cursor, match=f"{self._prefix}*", count=500
            )
            if keys:
                self._client.delete(*keys)
            if cursor == 0:
                break

    def exists(self, key: str) -> bool:
        return bool(self._client.exists(self._key(key)))

    @property
    def stats(self) -> Dict[str, Any]:
        info = self._client.info("stats")
        return {
            "hits": info.get("keyspace_hits", 0),
            "misses": info.get("keyspace_misses", 0),
        }

    def __repr__(self) -> str:
        return f"RedisCache(prefix='{self._prefix}')"


# ═══════════════════════════════════════════════
#  全局缓存工厂
# ═══════════════════════════════════════════════

_global_cache: Optional[MemoryCache] = None
_cache_lock = threading.Lock()


def get_cache(backend: str = "memory", **kwargs) -> Any:
    """
    获取全局缓存实例

    Parameters
    ----------
    backend : "memory" | "redis"
    """
    global _global_cache
    with _cache_lock:
        if _global_cache is None:
            if backend == "redis":
                _global_cache = RedisCache(**kwargs)
            else:
                _global_cache = MemoryCache(**kwargs)
        return _global_cache