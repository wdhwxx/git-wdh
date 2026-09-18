# 文件名：myquant/api/middleware/auth.py
"""
auth.py - API认证中间件
========================
REST API认证中间件，支持API Key认证。

v1实现范围（基础骨架）:
  - APIKeyAuth 认证类（API Key验证）
  - verify_api_key() 验证函数
  - get_current_user() 获取当前用户（FastAPI依赖注入）
  - 支持从Header/Query参数获取API Key
  - 支持多API Key配置（环境变量/配置文件）

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md）
  2. 为实时行情及实盘无缝对接做好充足准备（JWT/OAuth2/角色权限全部预留）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 认证只验证当前凭证，不预测未来

防未来函数说明:
  - 认证只验证当前传入的API Key/凭证
  - 不使用任何未来数据或预测
  - 令牌过期时间基于当前时间判断，不包含未来信息

TODO(后续迭代):
  - JWT Token认证
  - OAuth2认证
  - 角色权限管理（RBAC）
  - 用户登录/注册
  - 令牌刷新机制
  - API Key加密存储
  - 登录日志/审计
  - IP白名单

依赖: typing, os, secrets, myquant.core.logger
"""

from __future__ import annotations

import hashlib
import os
import secrets
from typing import Any, Dict, Optional, Set

from myquant.core.logger import get_logger

logger = get_logger("api.middleware.auth")


# ═══════════════════════════════════════════════
#  API Key认证
# ═══════════════════════════════════════════════

class APIKeyAuth:
    """
    API Key认证

    v1基础骨架：支持从Header/Query获取API Key，验证是否在允许列表中。

    使用方式:
        from myquant.api.middleware.auth import APIKeyAuth

        auth = APIKeyAuth(api_keys={"my-secret-key"})
        if auth.verify("my-secret-key"):
            # 认证通过
            pass

    与FastAPI集成（后续迭代）:
        from fastapi import Depends, HTTPException, Header

        async def get_api_key(x_api_key: str = Header(None)):
            if not auth.verify(x_api_key):
                raise HTTPException(status_code=401, detail="Invalid API Key")
            return x_api_key
    """

    def __init__(
        self,
        api_keys: Optional[Set[str]] = None,
        env_var: str = "QUANT_API_KEYS",
        header_name: str = "X-API-Key",
        query_param: str = "api_key",
        enabled: bool = True,
        allow_query_param: bool = False,
    ):
        """
        初始化API Key认证

        Parameters
        ----------
        api_keys : Set[str] | None
            允许的API Key集合（None则从环境变量读取）
        env_var : str
            环境变量名（多个Key用逗号分隔）
        header_name : str
            API Key的Header名称
        query_param : str
            API Key的Query参数名
        enabled : bool
            是否启用认证（False则跳过认证，开发环境用）
        allow_query_param : bool
            是否允许从 Query 参数提取 API Key（默认 False，H6 修复：
            Query 传 key 会进入浏览器历史/代理日志/访问日志，默认只走 Header；
            蓝图 v1 双通道能力保留，显式开启可用）
        """
        self.header_name = header_name
        self.query_param = query_param
        self.enabled = enabled
        self.allow_query_param = allow_query_param

        # 加载API Key
        self._api_keys: Set[str] = set()
        if api_keys:
            self._api_keys.update(api_keys)

        # 从环境变量加载
        env_keys = os.environ.get(env_var, "")
        if env_keys:
            for key in env_keys.split(","):
                key = key.strip()
                if key:
                    self._api_keys.add(key)

        if not self._api_keys and enabled:
            logger.warning(
                f"[auth] 未配置API Key，认证将拒绝所有请求。"
                f"请设置环境变量 {env_var} 或传入 api_keys 参数。"
            )

        logger.info(
            f"[auth] API Key认证初始化: enabled={enabled}, "
            f"keys={len(self._api_keys)}, header={header_name}"
        )

    @staticmethod
    def _key_fingerprint(api_key: str) -> str:
        """API Key 指纹（sha256 前 8 位），日志追踪用，不可还原明文。"""
        return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:8]

    def verify(self, api_key: Optional[str]) -> bool:
        """
        验证API Key

        Parameters
        ----------
        api_key : str | None
            待验证的API Key

        Returns
        -------
        bool : 是否验证通过
        """
        if not self.enabled:
            return True  # 认证禁用，全部通过

        if not api_key:
            logger.warning("[auth] 认证失败：未提供API Key")
            return False

        # 使用常量时间比较，防止时序攻击
        for valid_key in self._api_keys:
            if secrets.compare_digest(api_key, valid_key):
                return True

        logger.warning(
            f"[auth] 认证失败：无效API Key（sha256前8位: {self._key_fingerprint(api_key)}）"
        )
        return False

    def extract_from_headers(self, headers: Dict[str, Any]) -> Optional[str]:
        """
        从请求头提取API Key

        Parameters
        ----------
        headers : Dict
            请求头字典

        Returns
        -------
        str | None : API Key（未找到返回None）
        """
        # 不区分大小写查找
        for key, value in headers.items():
            if key.lower() == self.header_name.lower():
                return value
        return None

    def extract_from_query(self, query_params: Dict[str, Any]) -> Optional[str]:
        """
        从Query参数提取API Key

        Parameters
        ----------
        query_params : Dict
            Query参数字典

        Returns
        -------
        str | None : API Key（未找到返回None）
        """
        return query_params.get(self.query_param)

    def verify_request(self, headers: Dict[str, Any], query_params: Dict[str, Any]) -> bool:
        """
        验证请求（从Header和Query中提取API Key）

        Parameters
        ----------
        headers : Dict
            请求头
        query_params : Dict
            Query参数

        Returns
        -------
        bool : 是否验证通过
        """
        api_key = self.extract_from_headers(headers)
        if not api_key and self.allow_query_param:
            api_key = self.extract_from_query(query_params)
        return self.verify(api_key)

    @property
    def key_count(self) -> int:
        """已配置的API Key数量"""
        return len(self._api_keys)

    def add_key(self, api_key: str) -> None:
        """添加API Key"""
        self._api_keys.add(api_key)
        logger.info(f"[auth] 添加API Key: {self._key_fingerprint(api_key)}")

    def remove_key(self, api_key: str) -> None:
        """移除API Key"""
        self._api_keys.discard(api_key)
        logger.info(f"[auth] 移除API Key: {self._key_fingerprint(api_key)}")


# ═══════════════════════════════════════════════
#  便捷函数
# ═══════════════════════════════════════════════

def create_auth(
    api_keys: Optional[Set[str]] = None,
    enabled: bool = True,
) -> APIKeyAuth:
    """
    便捷函数：创建API Key认证实例

    Parameters
    ----------
    api_keys : Set[str] | None
        允许的API Key集合
    enabled : bool
        是否启用认证

    Returns
    -------
    APIKeyAuth : 认证实例
    """
    return APIKeyAuth(api_keys=api_keys, enabled=enabled)
