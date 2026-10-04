# 文件名：myquant/api/middleware/rate_limit.py
"""
rate_limit.py - API限流中间件
==============================
REST API限流中间件，支持固定窗口/令牌桶限流。

v1实现范围（基础骨架）:
  - RateLimiter 限流基类
  - FixedWindowLimiter 固定窗口限流
  - TokenBucketLimiter 令牌桶限流
  - 按IP/API Key/用户维度限流
  - 限流状态查询（剩余次数/重置时间）

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md）
  2. 为实时行情及实盘无缝对接做好充足准备（分布式限流/Redis后端全部预留）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 限流只基于当前时间和历史请求，不预测未来

防未来函数说明:
  - 限流只基于当前时间和已发生的请求计数
  - 不预测未来请求量
  - 窗口重置时间基于当前时间计算，不包含未来信息

TODO(后续迭代):
  - 分布式限流（Redis后端）
  - 滑动窗口限流
  - 漏桶算法
  - 分级限流（全局/接口/用户）
  - 限流告警通知
  - 限流日志/审计
  - 动态限流阈值调整
  - WebSocket限流

依赖: typing, time, collections, myquant.core.logger
"""

from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, Optional

from myquant.core.logger import get_logger

logger = get_logger("api.middleware.rate_limit")

# M2修复: 限流器最大跟踪键数——攻击者用随机 IP/API Key 打满请求时，
# 字典键无限增长会耗尽内存；超上限后惰性清理已过期/满额的键
DEFAULT_MAX_KEYS: int = 10_000


# ═══════════════════════════════════════════════
#  限流结果
# ═══════════════════════════════════════════════

@dataclass
class RateLimitResult:
    """
    限流检查结果

    Attributes
    ----------
    allowed : bool
        是否允许请求
    remaining : int
        剩余请求次数
    limit : int
        限制次数
    reset_time : float
        重置时间戳（Unix时间）
    retry_after : float
        需要等待的秒数
    """
    allowed: bool = True
    remaining: int = 0
    limit: int = 0
    reset_time: float = 0.0
    retry_after: float = 0.0


# ═══════════════════════════════════════════════
#  限流器基类
# ═══════════════════════════════════════════════

class RateLimiter(ABC):
    """
    限流器抽象基类

    所有限流器必须继承此类，实现 allow 和 get_remaining 方法。
    """

    def __init__(self, name: str = "default"):
        self.name = name
        self._request_counts: Dict[str, int] = defaultdict(int)

    @abstractmethod
    def allow(self, key: str) -> RateLimitResult:
        """
        检查是否允许请求

        Parameters
        ----------
        key : str
            限流键（IP/API Key/用户ID）

        Returns
        -------
        RateLimitResult : 限流结果
        """
        pass

    @abstractmethod
    def get_remaining(self, key: str) -> int:
        """
        获取剩余请求次数

        Parameters
        ----------
        key : str
            限流键

        Returns
        -------
        int : 剩余请求次数
        """
        pass

    def reset(self, key: Optional[str] = None) -> None:
        """
        重置限流计数

        Parameters
        ----------
        key : str | None
            限流键（None则重置所有）
        """
        if key:
            self._request_counts.pop(key, None)
        else:
            self._request_counts.clear()
        logger.info(f"[rate_limit] 重置限流: {self.name}, key={key or 'all'}")


# ═══════════════════════════════════════════════
#  固定窗口限流器
# ═══════════════════════════════════════════════

class FixedWindowLimiter(RateLimiter):
    """
    固定窗口限流器

    在固定时间窗口内限制请求次数，窗口结束后重置计数。

    使用方式:
        from myquant.api.middleware.rate_limit import FixedWindowLimiter

        limiter = FixedWindowLimiter(max_requests=100, window_seconds=60)
        result = limiter.allow("192.168.1.1")
        if result.allowed:
            # 处理请求
            pass
        else:
            # 返回429 Too Many Requests
            pass
    """

    def __init__(
        self,
        max_requests: int = 100,
        window_seconds: int = 60,
        name: str = "fixed_window",
        max_keys: int = DEFAULT_MAX_KEYS,
    ):
        """
        初始化固定窗口限流器

        Parameters
        ----------
        max_requests : int
            每个窗口最大请求次数
        window_seconds : int
            窗口大小（秒）
        name : str
            限流器名称
        """
        super().__init__(name=name)
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._window_start: Dict[str, float] = defaultdict(time.time)

    def allow(self, key: str) -> RateLimitResult:
        now = time.time()
        window_start = self._window_start[key]

        # 窗口过期，重置
        if now - window_start >= self.window_seconds:
            self._window_start[key] = now
            self._request_counts[key] = 0

        # M2修复: 跟踪键超上限 → 惰性清理已过期窗口的键（防无界增长）
        if len(self._window_start) > self.max_keys:
            _expired = [
                k for k, w in self._window_start.items()
                if now - w >= self.window_seconds
            ]
            for k in _expired:
                self._window_start.pop(k, None)
                self._request_counts.pop(k, None)

        count = self._request_counts[key]
        remaining = self.max_requests - count
        reset_time = self._window_start[key] + self.window_seconds

        if count >= self.max_requests:
            retry_after = reset_time - now
            logger.warning(
                f"[rate_limit] 请求被限流: key={key}, "
                f"count={count}/{self.max_requests}, "
                f"retry_after={retry_after:.1f}s"
            )
            return RateLimitResult(
                allowed=False,
                remaining=0,
                limit=self.max_requests,
                reset_time=reset_time,
                retry_after=retry_after,
            )

        # 允许请求，计数+1
        self._request_counts[key] += 1
        return RateLimitResult(
            allowed=True,
            remaining=remaining - 1,
            limit=self.max_requests,
            reset_time=reset_time,
            retry_after=0.0,
        )

    def get_remaining(self, key: str) -> int:
        now = time.time()
        window_start = self._window_start.get(key, now)
        if now - window_start >= self.window_seconds:
            return self.max_requests
        count = self._request_counts.get(key, 0)
        return max(0, self.max_requests - count)


# ═══════════════════════════════════════════════
#  令牌桶限流器
# ═══════════════════════════════════════════════

class TokenBucketLimiter(RateLimiter):
    """
    令牌桶限流器

    以固定速率向桶中添加令牌，每个请求消耗一个令牌。
    支持突发流量（桶中有足够令牌时允许突发）。

    使用方式:
        from myquant.api.middleware.rate_limit import TokenBucketLimiter

        limiter = TokenBucketLimiter(rate=10, capacity=50)
        result = limiter.allow("user_123")
        if result.allowed:
            # 处理请求
            pass
    """

    def __init__(
        self,
        rate: float = 10.0,
        capacity: int = 50,
        name: str = "token_bucket",
        max_keys: int = DEFAULT_MAX_KEYS,
    ):
        """
        初始化令牌桶限流器

        Parameters
        ----------
        rate : float
            令牌生成速率（个/秒）
        capacity : int
            桶容量（最大令牌数）
        name : str
            限流器名称
        """
        super().__init__(name=name)
        self.rate = rate
        self.capacity = capacity
        self.max_keys = max_keys
        self._tokens: Dict[str, float] = defaultdict(lambda: float(capacity))
        self._last_refill: Dict[str, float] = defaultdict(time.time)

    def allow(self, key: str) -> RateLimitResult:
        now = time.time()

        # 补充令牌
        last_refill = self._last_refill[key]
        elapsed = now - last_refill
        new_tokens = elapsed * self.rate
        self._tokens[key] = min(self.capacity, self._tokens[key] + new_tokens)
        self._last_refill[key] = now

        tokens = self._tokens[key]

        if tokens < 1.0:
            # 令牌不足，计算需要等待的时间
            needed = 1.0 - tokens
            retry_after = needed / self.rate if self.rate > 0 else float("inf")
            logger.warning(
                f"[rate_limit] 请求被限流(令牌桶): key={key}, "
                f"tokens={tokens:.2f}/{self.capacity}, "
                f"retry_after={retry_after:.1f}s"
            )
            return RateLimitResult(
                allowed=False,
                remaining=int(tokens),
                limit=self.capacity,
                reset_time=now + retry_after,
                retry_after=retry_after,
            )

        # M2修复: 跟踪键超上限 → 清理 1 小时未访问的键（防无界增长）
        if len(self._tokens) > self.max_keys:
            _stale = [
                k
                for k in list(self._tokens)
                if (now - self._last_refill[k]) > 3600.0
            ]
            for k in _stale:
                self._tokens.pop(k, None)
                self._last_refill.pop(k, None)

        # 消耗一个令牌
        self._tokens[key] -= 1.0
        return RateLimitResult(
            allowed=True,
            remaining=int(self._tokens[key]),
            limit=self.capacity,
            reset_time=now,
            retry_after=0.0,
        )

    def get_remaining(self, key: str) -> int:
        now = time.time()
        last_refill = self._last_refill.get(key, now)
        elapsed = now - last_refill
        tokens = min(self.capacity, self._tokens.get(key, self.capacity) + elapsed * self.rate)
        return int(tokens)


# ═══════════════════════════════════════════════
#  Redis 固定窗口限流器（KP-P1-05 分布式后端）
# ═══════════════════════════════════════════════

class RedisFixedWindowLimiter(RateLimiter):
    """Redis 固定窗口限流器（KP-P1-05：分布式后端，多实例共享计数）。

    键 = rl:{name}:{窗口桶}:{key}，INCR + EXPIRE 实现固定窗口；
    redis 缺依赖/连接失败时由 create_rate_limiter 降级内存并显式告警。
    """

    def __init__(
        self,
        max_requests: int = 100,
        window_seconds: int = 60,
        name: str = "redis_fixed_window",
        redis_client: Optional[Any] = None,
    ) -> None:
        super().__init__(name=name)
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        if redis_client is not None:
            self._redis = redis_client
        else:
            try:
                import redis
            except ImportError as exc:
                raise RuntimeError("Redis 限流后端需要安装: pip install redis") from exc
            self._redis = redis.Redis(
                host=os.environ.get("QUANT_REDIS_HOST", "localhost"),
                port=int(os.environ.get("QUANT_REDIS_PORT", "6379")),
                db=int(os.environ.get("QUANT_REDIS_DB", "0")),
                socket_connect_timeout=5.0,
                socket_timeout=5.0,
                decode_responses=True,
            )

    def _bucket(self, now: float) -> str:
        return str(int(now // self.window_seconds))

    def _key(self, key: str, bucket: str) -> str:
        return f"rl:{self.name}:{bucket}:{key}"

    def allow(self, key: str) -> RateLimitResult:
        now = time.time()
        rkey = self._key(key, self._bucket(now))
        try:
            count = int(self._redis.incr(rkey))
        except Exception as exc:
            logger.error("[rate_limit] Redis 限流计数失败（按拒绝处理）: %s", exc)
            return RateLimitResult(
                allowed=False, remaining=0, limit=self.max_requests,
                reset_time=now, retry_after=0.0,
            )
        if count == 1:
            try:
                self._redis.expire(rkey, self.window_seconds)
            except Exception:
                pass  # EXPIRE 失败不影响本次判定，窗口桶随 bucket 切换自然失效
        reset_time = (int(now // self.window_seconds) + 1) * self.window_seconds
        if count > self.max_requests:
            return RateLimitResult(
                allowed=False, remaining=0, limit=self.max_requests,
                reset_time=reset_time, retry_after=reset_time - now,
            )
        return RateLimitResult(
            allowed=True, remaining=self.max_requests - count,
            limit=self.max_requests, reset_time=reset_time, retry_after=0.0,
        )

    def get_remaining(self, key: str) -> int:
        now = time.time()
        try:
            val = self._redis.get(self._key(key, self._bucket(now)))
        except Exception:
            return 0
        return max(0, self.max_requests - int(val or 0))

    def reset(self, key: Optional[str] = None) -> None:
        now = time.time()
        try:
            if key:
                self._redis.delete(self._key(key, self._bucket(now)))
            else:
                logger.info("[rate_limit] Redis 全量重置 %s（当前窗口桶）", self.name)
        except Exception as exc:
            logger.error("[rate_limit] Redis 重置失败: %s", exc)


def create_rate_limiter(
    max_requests: int = 100,
    window_seconds: int = 60,
) -> RateLimiter:
    """按 QUANT_RATE_LIMIT_BACKEND（memory/redis）创建固定窗口限流器（KP-P1-05）。

    Redis 后端不可用（缺依赖/连接失败）→ 显式告警并降级内存，不崩 API。
    """
    backend = os.environ.get("QUANT_RATE_LIMIT_BACKEND", "memory").strip().lower()
    if backend == "redis":
        try:
            return RedisFixedWindowLimiter(
                max_requests=max_requests, window_seconds=window_seconds,
            )
        except Exception as exc:
            logger.error("Redis 限流后端初始化失败，降级内存（KP-P1-05）: %s", exc)
    return FixedWindowLimiter(max_requests=max_requests, window_seconds=window_seconds)


# ═══════════════════════════════════════════════
#  便捷函数
# ═══════════════════════════════════════════════

def create_fixed_window_limiter(
    max_requests: int = 100,
    window_seconds: int = 60,
) -> FixedWindowLimiter:
    """便捷函数：创建固定窗口限流器"""
    return FixedWindowLimiter(max_requests=max_requests, window_seconds=window_seconds)


def create_token_bucket_limiter(
    rate: float = 10.0,
    capacity: int = 50,
) -> TokenBucketLimiter:
    """便捷函数：创建令牌桶限流器"""
    return TokenBucketLimiter(rate=rate, capacity=capacity)
