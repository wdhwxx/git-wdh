# 文件名：myquant/monitoring/security_monitor.py
"""
security_monitor.py - 安全监控（A-13，回写#217；依据 P0-7 密钥零明文/脱敏规则）
==========================================================================
本模块承载《量化系统监控模块施工总清单_V1.2》A-13：
  - API 密钥/券商凭证变更与泄漏检测（敏感凭证出现在日志/告警/未授权上下文）
  - 未授权 API 访问（401/403 计数 / 事件）
  - 异常登录、账户非本人操作
  - 异常 → P0/P1 告警（密钥泄漏/未授权访问 P1，凭证变更 P0）

硬约束（P0-7）:
  - 密钥零明文落盘/日志/告警：本模块识别"疑似密钥模式"，但**绝不**把密钥原文写入
    日志 / 告警 details / 落盘；告警只携带脱敏摘要（sha256 指纹 + 掩码后 4 位）。
  - 复用 field_crypto.mask_value 的脱敏思想（本模块自建指纹助手，便于关联同一密钥）。

触发模式（R-02）：事件驱动为主（可注入钩子，供 API 层/外部调用），
  get_status() 供周期巡检暴露计数。

依赖: re, hashlib, myquant.core.{constants,logger}
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional

from myquant.core.constants import SeverityLevel
from myquant.core.logger import get_logger

logger = get_logger("monitoring.security")

# ─────────────────────────────────────────────
# 常量（非业务阈值；事件类型枚举化）
# ─────────────────────────────────────────────
#: 安全事件类型（security_events_total 的 event_type 标签取值）
EVT_LOGIN_FAILED: str = "login_failed"            # 异常登录（登录失败/非本人操作嫌疑）
EVT_UNAUTHORIZED_ACCESS: str = "unauthorized_access"  # 未授权 API 访问（401/403）
EVT_CREDENTIAL_CHANGE: str = "credential_change"   # 凭证变更
EVT_SECRET_EXPOSURE: str = "secret_exposure"       # 疑似密钥泄漏（进日志/异常上下文）

#: 疑似密钥模式（识别"看起来像密钥"的串，仅用于触发检测，原文绝不外泄）
#: 命中后立即 sha256 指纹化，原文不出本函数作用域。
_SUSPECTED_SECRET_PATTERNS: tuple = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),                       # OpenAI 风格 sk-xxx
    re.compile(r"(?i)api[_-]?key\s*[=:]\s*[\"']?([A-Za-z0-9_\-]{12,})"),   # api_key=...
    re.compile(r"(?i)secret\s*[=:]\s*[\"']?([A-Za-z0-9_\-]{12,})"),        # secret=...
    re.compile(r"(?i)token\s*[=:]\s*[\"']?([A-Za-z0-9_\-\.]{16,})"),       # token=...
    re.compile(r"(?i)password\s*[=:]\s*[\"']?(\S{8,})"),                   # password=...
    re.compile(r"\b[A-Za-z0-9+/]{32,}={0,2}\b"),                # 裸 base64 长串
)


def sha256_fingerprint(secret: str) -> str:
    """
    计算密钥的 sha256 短指纹（用于在告警中标识"是哪一把"密钥，不泄露原文）。

    Parameters
    ----------
    secret : str
        疑似密钥原文（仅本函数内用于哈希，调用方不得留存/打印）。

    Returns
    -------
    str : 16 位十六进制指纹（前 16 位 sha256）。空串返回 "".
    """
    if not secret:
        return ""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


def mask_secret(secret: str) -> str:
    """
    脱敏展示：前 2 位 + **** + 后 4 位（P0-7 脱敏规则；绝不返回完整原文）。

    Returns
    -------
    str : 形如 "ab****wxyz"；长度 <=6 时一律 "****"。
    """
    if not secret:
        return ""
    if len(secret) <= 6:
        return "****"
    return f"{secret[:2]}****{secret[-4:]}"


class SecurityMonitor:
    """
    安全监控器（事件钩子可注入；密钥零明文落盘）。

    供 API 层 / 外部调用的钩子：
      - observe_login(actor, ok)               登录结果（ok=False → 异常登录 P1）
      - observe_unauthorized_access(actor, endpoint, status_code)  401/403 P1
      - observe_credential_change(actor, credential_fingerprint)  凭证变更 P0
      - observe_secret_exposure(context, text)  扫描上下文文本，疑似密钥泄漏 P1
      - scan_text(text) -> List[str]            纯检测：返回命中的指纹列表（不含原文）

    密钥零明文（P0-7）:
      - 任何告警 details / 日志只携带 sha256 指纹与 mask_secret 掩码摘要；
      - 疑似密钥原文仅在内存中用于哈希，绝不写入告警/日志/落盘。

    防未来：只依据"已发生"的登录/访问/变更事件做判定，不预测未来攻击。
    """

    def __init__(
        self,
        alert_manager: Any = None,
        metrics_collector: Any = None,
    ) -> None:
        """
        Parameters
        ----------
        alert_manager : AlertManager | None
            告警管理器；None 延迟取 get_alert_manager()。
        metrics_collector : MetricsCollector | None
            指标采集器；None 延迟取 get_metrics_collector()（security_events_total）。
        """
        self._alert_manager = alert_manager
        self._metrics_collector = metrics_collector
        # 已发生安全事件计数（按 event_type；供 get_status 周期暴露）
        self._counts: Dict[str, int] = {}

    # ── 指标 ────────────────────────────────────────────────
    def _inc_event(self, event_type: str) -> None:
        """累计安全事件计数（内存 + security_events_total 进 /api/metrics）。"""
        self._counts[event_type] = self._counts.get(event_type, 0) + 1
        col = self._metrics_collector
        if col is None:
            try:
                from myquant.monitoring.metrics_collector import get_metrics_collector
                col = get_metrics_collector()
                self._metrics_collector = col
            except Exception as e:  # noqa: BLE001
                logger.warning(f"获取 metrics_collector 失败（security_events_total 未计数）: {e}")
                return
        try:
            col.inc_counter("security_events_total", 1.0, labels={"event_type": event_type})
        except Exception as e:  # noqa: BLE001
            logger.warning(f"security_events_total 计数失败（不阻断告警）: {e}")

    # ── 告警（统一入口；details 已脱敏）─────────────────────
    def _alert(
        self, level: SeverityLevel, message: str, details: Dict[str, Any], key: str
    ) -> None:
        mgr = self._alert_manager
        if mgr is None:
            from myquant.monitoring.alerting import get_alert_manager
            mgr = get_alert_manager()
        mgr.alert(
            level=level,
            message=f"[security] {message}",
            source="monitoring.security",
            category="security",
            details=details,
            deduplication_key=key,
        )

    @staticmethod
    def _nonempty(value: str, name: str) -> str:
        """防御式：actor/endpoint 等必填字符串非空校验。"""
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} 必须为非空字符串，实际 {value!r}")
        return value.strip()

    # ── 钩子 1：登录 ────────────────────────────────────────
    def observe_login(self, actor: str, ok: bool) -> None:
        """
        登录事件钩子（API 层/外部调用）。

        Parameters
        ----------
        actor : str
            登录主体（用户名/账号标识，非密钥）。
        ok : bool
            登录是否成功。False → 异常登录 P1 告警（账户非本人操作嫌疑）。
        """
        actor = self._nonempty(actor, "actor")
        if not isinstance(ok, bool):
            raise ValueError(f"ok 必须为 bool，实际 {ok!r}")
        self._inc_event(EVT_LOGIN_FAILED if not ok else "login_ok")
        if ok:
            return
        # 异常登录 → P1/ERROR
        self._alert(
            SeverityLevel.ERROR,
            f"异常登录：主体 {actor} 登录失败（疑似账户非本人操作/凭证失效）",
            details={"event_type": EVT_LOGIN_FAILED, "actor": actor},
            key=f"security:login_failed:{actor}",
        )

    # ── 钩子 2：未授权 API 访问 ─────────────────────────────
    def observe_unauthorized_access(
        self, actor: str, endpoint: str, status_code: Optional[int] = None
    ) -> None:
        """
        未授权 API 访问钩子（401/403，P1）。

        Parameters
        ----------
        actor : str
            访问主体（用户名/客户端标识/匿名）。
        endpoint : str
            被访问的端点路径。
        status_code : int | None
            HTTP 状态码（401/403）。
        """
        actor = self._nonempty(actor, "actor")
        endpoint = self._nonempty(endpoint, "endpoint")
        self._inc_event(EVT_UNAUTHORIZED_ACCESS)
        self._alert(
            SeverityLevel.ERROR,
            f"未授权 API 访问：主体 {actor} 访问 {endpoint}"
            + (f"（HTTP {status_code}）" if status_code else ""),
            details={
                "event_type": EVT_UNAUTHORIZED_ACCESS,
                "actor": actor,
                "endpoint": endpoint,
                "status_code": status_code,
            },
            key=f"security:unauthorized:{actor}:{endpoint}",
        )

    # ── 钩子 3：凭证变更 ────────────────────────────────────
    def observe_credential_change(self, actor: str, credential_fingerprint: str) -> None:
        """
        凭证变更钩子（P0/CRITICAL）。

        Parameters
        ----------
        actor : str
            执行变更的主体。
        credential_fingerprint : str
            凭证的**指纹/标识**（调用方须传非秘密标识，如 sha256 短指纹；
            本模块不再二次指纹化，原样记录，禁止传密钥原文）。
        """
        actor = self._nonempty(actor, "actor")
        credential_fingerprint = self._nonempty(credential_fingerprint, "credential_fingerprint")
        self._inc_event(EVT_CREDENTIAL_CHANGE)
        self._alert(
            SeverityLevel.CRITICAL,
            f"券商/API 凭证变更：主体 {actor}（凭证指纹 {credential_fingerprint}）",
            details={
                "event_type": EVT_CREDENTIAL_CHANGE,
                "actor": actor,
                "credential_fingerprint": credential_fingerprint,
            },
            key=f"security:cred_change:{actor}:{credential_fingerprint}",
        )

    # ── 钩子 4：疑似密钥泄漏检测 ────────────────────────────
    def scan_text(self, text: Optional[str]) -> List[str]:
        """
        扫描文本，检测疑似密钥模式，返回命中的**指纹列表**（不含原文）。

        Parameters
        ----------
        text : str | None
            待扫描文本（日志片段/异常信息/告警上下文）。

        Returns
        -------
        list[str] : 命中密钥的 sha256 指纹列表（可能为空）。原文绝不外泄。
        """
        if not text or not isinstance(text, str):
            return []
        fingerprints: List[str] = []
        for pat in _SUSPECTED_SECRET_PATTERNS:
            for m in pat.finditer(text):
                # 命中组：优先取捕获组（api_key=xxx 的 xxx），否则取整段匹配
                secret = m.group(1) if m.lastindex else m.group(0)
                fp = sha256_fingerprint(secret)
                if fp:
                    fingerprints.append(fp)
        return fingerprints

    def observe_secret_exposure(self, context: str, text: str) -> Optional[str]:
        """
        疑似密钥泄漏钩子：扫描上下文文本，命中即 P1 告警（details 只带指纹/掩码）。

        Parameters
        ----------
        context : str
            泄漏发生的上下文位置（如 "log" / "alert" / "exception"，非秘密）。
        text : str
            疑似含密钥的文本片段。

        Returns
        -------
        str | None : 命中的首个指纹；无命中返回 None。
        """
        context = self._nonempty(context, "context")
        fps = self.scan_text(text)
        if not fps:
            return None
        fp = fps[0]
        self._inc_event(EVT_SECRET_EXPOSURE)
        # 密钥零明文：details 只放指纹 + 命中数量，绝不放原文
        self._alert(
            SeverityLevel.ERROR,
            f"疑似密钥泄漏：在 {context} 上下文检测到 {len(fps)} 处疑似凭证"
            f"（指纹 {fp}，原文已脱敏，P0-7）",
            details={
                "event_type": EVT_SECRET_EXPOSURE,
                "context": context,
                "fingerprint": fp,
                "match_count": len(fps),
            },
            key=f"security:secret_exposure:{fp}",
        )
        return fp

    # ── 状态查询（周期巡检暴露）──────────────────────────────
    def get_status(self) -> Dict[str, Any]:
        """返回已发生安全事件计数快照（供周期巡检/看板暴露；不含任何密钥原文）。"""
        return {"events": dict(self._counts)}


# ═══════════════════════════════════════════════
#  __main__ 自测
# ═══════════════════════════════════════════════
def _run_self_test() -> int:
    """模块自测：异常登录/未授权访问/凭证变更/密钥泄漏即告警，且 details 无明文。"""
    passed = 0
    failures: List[str] = []

    def check(name: str, cond: bool, info: str = "") -> None:
        nonlocal passed
        if cond:
            passed += 1
        else:
            failures.append(f"{name} {info}")

    class FakeAlert:
        def __init__(self):
            self.calls = []

        def alert(self, **kw):
            self.calls.append(kw)
            return 1

    class FakeMetrics:
        def __init__(self):
            self.counts = {}

        def inc_counter(self, name, amount=1.0, labels=None):
            self.counts.setdefault(name, {}).setdefault(
                (labels or {}).get("event_type", ""), 0
            )
            self.counts[name][(labels or {}).get("event_type", "")] += int(amount)

    SECRET = "sk-SUPERSECRETKEY-abcdef1234567890"  # 测试用假密钥

    # 1) 异常登录 → ERROR(P1) 告警
    am = FakeAlert()
    mon = SecurityMonitor(alert_manager=am, metrics_collector=FakeMetrics())
    mon.observe_login("trader01", ok=False)
    check("1) 异常登录发ERROR告警", any(c["level"].name == "ERROR" for c in am.calls), f"calls={am.calls}")
    # 2) 正常登录不告警
    am.calls.clear()
    mon.observe_login("trader01", ok=True)
    check("2) 正常登录不告警", am.calls == [], f"calls={am.calls}")
    # 3) 未授权访问 403 → ERROR
    am.calls.clear()
    mon.observe_unauthorized_access("anon", "/api/admin", status_code=403)
    u = [c for c in am.calls if c["details"]["event_type"] == "unauthorized_access"]
    check("3) 403未授权访问发ERROR", len(u) == 1 and u[0]["details"]["status_code"] == 403, f"calls={am.calls}")
    # 4) 凭证变更 → CRITICAL(P0)，details 只带指纹
    am.calls.clear()
    mon.observe_credential_change("ops", "fp_a1b2c3")
    c = [c for c in am.calls if c["details"]["event_type"] == "credential_change"]
    check("4) 凭证变更发CRITICAL告警", len(c) == 1 and c[0]["level"].name == "CRITICAL", f"calls={am.calls}")
    # 5) 密钥泄漏：扫描含假密钥文本 → P1，details 无明文密钥
    am.calls.clear()
    met = FakeMetrics()
    mon2 = SecurityMonitor(alert_manager=am, metrics_collector=met)
    fp = mon2.observe_secret_exposure("log", f"connect failed api_key={SECRET} timeout")
    s = [c for c in am.calls if c["details"]["event_type"] == "secret_exposure"]
    check("5) 命中疑似密钥发ERROR告警", len(s) == 1, f"calls={am.calls}")
    check("5) 告警details不含密钥原文", all(SECRET not in str(c["details"]) and SECRET not in c["message"] for c in am.calls), f"calls={am.calls}")
    check("5) 告警details带指纹而非原文", s and s[0]["details"]["fingerprint"] == fp and len(fp) == 16, f"fp={fp}")
    # 6) scan_text 只返回指纹列表
    fps = mon2.scan_text(f"api_key={SECRET}")
    check("6) scan_text返回指纹不含原文", fps and SECRET not in fps[0] and len(fps[0]) == 16, f"fps={fps}")
    # 7) 指标 security_events_total 按 event_type 计数
    check("7) security_events_total计数", met.counts.get("security_events_total", {}).get("secret_exposure") == 1, f"met={met.counts}")
    # 8) mask_secret / sha256_fingerprint 不泄露原文
    check("8) mask_secret不含完整原文", SECRET not in mask_secret(SECRET) and "****" in mask_secret(SECRET), f"{mask_secret(SECRET)}")
    check("8) 指纹与原文不同", sha256_fingerprint(SECRET) != SECRET and SECRET not in sha256_fingerprint(SECRET), "")
    # 9) 防御式：空 actor 抛错
    raised = False
    try:
        mon.observe_login("", ok=False)
    except ValueError:
        raised = True
    check("9) 空actor抛ValueError", raised, "")
    # 10) get_status 不含密钥原文
    st = mon2.get_status()
    check("10) get_status不含密钥原文", SECRET not in str(st), f"st={st}")

    print("=" * 60)
    print(f"security_monitor.py 自测: {passed} 项通过")
    if failures:
        for f in failures:
            print(f"  ✗ {f}")
        print(f"失败 {len(failures)} 项")
        return 1
    print("全部通过 ✅")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_self_test())
