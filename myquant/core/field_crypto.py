# 文件名：myquant/monitoring/field_crypto.py
"""
field_crypto.py - 监控关键字段加密（BASE-9 监控数据加密与访问审计）
====================================================================
依据《量化系统监控模块施工总清单_V1.2》BASE-9（新增·R-06）：
  - 含资金/持仓/密钥派生信息的监控库表落盘加密（静态加密，个人系统
    降级版：OS 级加密卷 + 库文件权限收紧 + 关键字段加密）
  - 对监控库的读取/导出纳入访问审计（与 BASE-7 只追加形成
    "防篡改 + 防泄露"双控）
  - 验收：监控库文件权限收紧；关键字段密文落盘；读取/导出有审计记录

本模块职责（关键字段加密）：
  1. FieldCrypto：Fernet 对称加密（cryptography），密钥从环境变量
     QUANT_MONITOR_KEY（base64 32 字节）或本地密钥文件读取；
     无密钥时自动生成密钥文件（首次）并记 warning 提示妥善保管
  2. SENSITIVE_NAME_PATTERNS：敏感指标名前缀/关键字集合
     （equity/balance/position/capital 等，可配置覆盖）
  3. is_sensitive_name / mask：敏感判定 + 脱敏展示

安全说明（个人系统降级版）：
  - 密钥文件仅本机可读（权限 600）；配合 OS 级加密卷 + 库文件权限
    收紧（connection.py harden_db_permissions）构成"防泄露"防线
  - 明文绝不落盘：命中敏感名 → 落库值=占位 0.0 + value_encrypted=密文；
    查询时解密回读（解密失败记 error 返回脱敏值）

防未来铁律：加/解密只作用于已发生的数据落盘/回读，不产生任何时间序列
预测；密钥读取失败记 error 不静默（宁可拒绝密文写入也不明文落盘）。

依赖: os, base64, logging, pathlib, cryptography.fernet
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Any, Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("myquant.core.field_crypto")


# ═══════════════════════════════════════════════
#  常量
# ═══════════════════════════════════════════════

_KEY_ENV_NAME: str = "QUANT_MONITOR_KEY"       # 环境变量密钥（base64 32B）
_DEFAULT_KEY_FILE: str = "config/monitoring/.monitor_field_key"  # 默认密钥文件（项目相对）
_SENSITIVE_NAME_PATTERNS: tuple = (
    "equity",       # 资金曲线/权益（equity_curve_total 等）
    "balance",      # 账户余额
    "position",     # 持仓相关（position_value/position_ratio 等）
    "capital",      # 资金/本金
    "asset",        # 资产总额
    "pnl",          # 盈亏额（累计盈亏绝对值）
)


def is_sensitive_name(name: str, patterns: tuple = _SENSITIVE_NAME_PATTERNS) -> bool:
    """
    敏感指标名判定（命中即关键字段，落盘加密）。

    Parameters
    ----------
    name : str
        指标名（P0-5 命名规范）
    patterns : tuple
        敏感关键字集合（可配置覆盖默认）

    Returns
    -------
    bool : True=含资金/持仓/密钥派生信息，须加密落盘
    """
    n = (name or "").strip().lower()
    if not n:
        return False
    return any(p in n for p in patterns)


def mask_value(value: Any) -> str:
    """
    脱敏展示（解密失败/未解密时的兜底显示）。

    Returns
    -------
    str : "****"（不泄露明文）
    """
    return "****"


class FieldCrypto:
    """
    关键字段加密器（Fernet 对称加密，静态加密落盘）。

    Usage:
        crypto = FieldCrypto(key_env=..., key_file=...)   # 自动探测密钥
        token = crypto.encrypt("123456.78")
        value = crypto.decrypt(token)                     # "123456.78"

    密钥来源优先级（探测顺序）：
      1. 环境变量 QUANT_MONITOR_KEY（base64 32 字节）
      2. 密钥文件 config/monitoring/.monitor_field_key（首次自动生成）
    """

    def __init__(
        self,
        key_env: Optional[str] = None,
        key_file: Optional[str] = None,
    ):
        key = self._load_or_create_key(key_env, key_file)
        self._fernet = Fernet(key)

    # ── 密钥管理 ──

    @staticmethod
    def _generate_key() -> bytes:
        return Fernet.generate_key()

    def _load_or_create_key(
        self, key_env: Optional[str], key_file: Optional[str]
    ) -> bytes:
        """探测密钥：环境变量优先，其次密钥文件（缺失则生成并落盘）。"""
        env_key = key_env if key_env is not None else os.environ.get(_KEY_ENV_NAME)
        if env_key:
            try:
                return env_key.encode("utf-8") if isinstance(env_key, str) else bytes(env_key)
            except (TypeError, ValueError):
                raise ValueError("QUANT_MONITOR_KEY 非法（须为 base64 32 字节字符串）")
        # 密钥文件（默认相对项目根）
        path = Path(key_file or _DEFAULT_KEY_FILE)
        if path.exists():
            return path.read_bytes().strip()
        # 首次运行：生成密钥并落盘（权限 600；Windows 提示 OS 级加密卷兜底）
        key = self._generate_key()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(key)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        logger.warning(
            f"[crypto] 已生成新监控密钥文件: {path}（请妥善保管，丢失则历史密文不可解）"
        )
        return key

    # ── 加/解密 ──

    def encrypt(self, value: Any) -> str:
        """
        加密字段值（密文落盘）。

        Parameters
        ----------
        value : Any
            明文（str/float 等，序列化为 str 后加密）

        Returns
        -------
        str : Fernet token（utf-8 密文）

        Raises
        ------
        ValueError: 加密失败时抛出（宁可拒绝也不明文落盘）
        """
        try:
            plain = str(value)
            token = self._fernet.encrypt(plain.encode("utf-8"))
            return token.decode("utf-8")
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"字段加密失败（拒绝明文落盘）: {e}")

    def decrypt(self, token: str) -> str:
        """
        解密字段值（回读还原）。

        Parameters
        ----------
        token : str
            Fernet 密文

        Returns
        -------
        str : 明文

        Raises
        ------
        ValueError: 密文非法/密钥不符（历史密钥丢失）时抛出
        """
        try:
            return self._fernet.decrypt(token.encode("utf-8")).decode("utf-8")
        except InvalidToken:
            raise ValueError("字段解密失败: 密文非法或密钥不符（历史密钥可能已变更）")
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"字段解密失败: {e}")

    def try_decrypt(self, token: str) -> Any:
        """
        安全解密（查询回读用）：失败返回脱敏值并记 error，不抛异常。

        Returns
        -------
        str | None : 明文；失败返回 None（调用方显示脱敏值）
        """
        try:
            return self.decrypt(token)
        except ValueError as e:
            logger.error(f"敏感字段回读解密失败（脱敏显示）: {e}")
            return None


# ═══════════════════════════════════════════════
#  自测（python -m myquant.core.field_crypto）
# ═══════════════════════════════════════════════

def _run_self_test() -> int:
    """独立自测：临时密钥跑通生成/加密/解密/敏感判定/脱敏，0=通过。"""
    import tempfile

    logger.info("FieldCrypto 自测开始")
    passed = 0
    failed = 0

    def check(label: str, cond: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if cond:
            passed += 1
            logger.info("  ✓ %s", label)
        else:
            failed += 1
            logger.error("  ✗ %s %s", label, detail)

    with tempfile.TemporaryDirectory() as tmp:
        key_file = str(Path(tmp) / ".test_key")
        crypto = FieldCrypto(key_env=None, key_file=key_file)

        # 密钥文件生成 + 可复用
        check("首次生成密钥文件", Path(key_file).exists())
        crypto2 = FieldCrypto(key_env=None, key_file=key_file)
        check("密钥文件复用（同库可解）", crypto2.decrypt(crypto.encrypt("1.5")) == "1.5")

        # 加密/解密往返
        plain = "123456.78"
        token = crypto.encrypt(plain)
        check("加密密文非明文", token != plain and len(token) > 10)
        check("解密还原", crypto.decrypt(token) == plain)

        # 敏感判定与脱敏
        check("equity 敏感", is_sensitive_name("equity_curve_total"))
        check("position 敏感", is_sensitive_name("position_value_a"))
        check("普通指标不敏感", not is_sensitive_name("patrol_status"))
        check("脱敏值不泄露", mask_value("123456.78") == "****")

        # 环境变量密钥
        env_key = FieldCrypto._generate_key().decode()
        os.environ[_KEY_ENV_NAME] = env_key
        try:
            crypto_env = FieldCrypto()
            check("环境变量密钥加载", crypto_env.decrypt(crypto_env.encrypt("9.9")) == "9.9")
        finally:
            del os.environ[_KEY_ENV_NAME]

        # 密钥不符 → 解密失败（try_decrypt 返回 None 不抛）
        other = FieldCrypto(key_env=FieldCrypto._generate_key().decode())
        check("错误密钥解密失败（拒绝明文）", other.try_decrypt(token) is None)

    logger.info("自测结果: %d 通过, %d 失败", passed, failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)-5s %(message)s")
    raise SystemExit(_run_self_test())
