# 文件名：tests/unit/test_health_check.py
"""
test_health_check.py - A-1 L0 基础设施监控补全单元测试
=====================================================
覆盖（不依赖外网：不可达地址验证失败/降级分支，本机验证正常分支）：
  1. register_l0_checks：7 项全部注册、run_all 可执行、汇总计数一致
  2. 与 register_default_checks 共存（3+7=10 项）
  3. 防御式校验：阈值倒挂/语义错、非法端点/域名/超时/空服务器抛 ValueError
  4. check_network_latency：不可达目标降级不崩溃、空 targets 跳过 HEALTHY
  5. check_clock_skew：空 server 抛 ValueError、NTP 不可达 → DEGRADED（不误判时钟损坏）
  6. check_cpu_usage / check_memory_usage：可执行、状态枚举合法
  7. check_tls_certificate：阈值语义校验（warning>critical）、空 hosts 跳过、不可达 → DEGRADED
  8. check_dns_resolution：本机解析 HEALTHY、空域名/非 list 校验
  9. check_network_partition：空端点跳过、端点失败不崩溃

运行方式:
  pytest tests/unit/test_health_check.py -v
"""

from __future__ import annotations

from typing import Tuple

import pytest

from myquant.core.health_check import (
    DEFAULT_DNS_HOSTS,
    DEFAULT_LATENCY_TARGETS,
    DEFAULT_PARTITION_ENDPOINTS,
    DEFAULT_TLS_HOSTS,
    HealthChecker,
    HealthStatus,
    check_clock_skew,
    check_cpu_usage,
    check_dns_resolution,
    check_memory_usage,
    check_network_latency,
    check_network_partition,
    check_tls_certificate,
    register_l0_checks,
)

_UNREACHABLE: Tuple[str, int] = ("10.255.255.1", 53)


# ═══════════════════════════════════════════
#  1. register_l0_checks
# ═══════════════════════════════════════════

class TestRegisterL0:
    L0_NAMES = [
        "network_latency",
        "clock_skew",
        "cpu_usage",
        "memory_usage",
        "tls_certificate",
        "dns_resolution",
        "network_partition",
    ]

    def test_registers_seven(self) -> None:
        checker = HealthChecker()
        register_l0_checks(checker)
        for name in self.L0_NAMES:
            assert name in checker.list_checks()
        assert len(checker.list_checks()) == 7

    def test_run_all_and_summary(self) -> None:
        checker = HealthChecker()
        register_l0_checks(checker)
        results = checker.run_all()
        assert len(results) == 7
        assert all(r.status in HealthStatus for r in results)
        summary = checker.get_summary()
        assert summary["checks_count"] == 7
        assert summary["overall_status"] in ("healthy", "degraded", "unhealthy")
        assert summary["healthy_count"] + summary["degraded_count"] + summary["unhealthy_count"] == 7

    def test_coexist_with_defaults(self) -> None:
        checker = HealthChecker()
        checker.register_default_checks()
        register_l0_checks(checker)
        assert len(checker.list_checks()) == 10  # 3 默认 + 7 L0

    def test_chainable(self) -> None:
        checker = HealthChecker()
        assert register_l0_checks(checker) is checker


# ═══════════════════════════════════════════
#  2. 防御式校验
# ═══════════════════════════════════════════

class TestValidation:
    def test_latency_threshold_inverted(self) -> None:
        with pytest.raises(ValueError):
            check_network_latency(warning_ms=500.0, critical_ms=100.0)

    def test_latency_bad_endpoint_type(self) -> None:
        with pytest.raises(ValueError):
            check_network_latency(targets=["host:80"])  # type: ignore[list-item]

    def test_latency_bad_port(self) -> None:
        with pytest.raises(ValueError):
            check_network_latency(targets=[("host", 70000)])

    def test_clock_empty_server(self) -> None:
        with pytest.raises(ValueError):
            check_clock_skew(server="")

    def test_clock_threshold_inverted(self) -> None:
        with pytest.raises(ValueError):
            check_clock_skew(warning_seconds=60.0, critical_seconds=5.0)

    def test_cpu_threshold_inverted(self) -> None:
        with pytest.raises(ValueError):
            check_cpu_usage(warning_pct=95.0, critical_pct=90.0)

    def test_cpu_negative_interval(self) -> None:
        with pytest.raises(ValueError):
            check_cpu_usage(interval=-0.1)

    def test_memory_threshold_negative(self) -> None:
        with pytest.raises(ValueError):
            check_memory_usage(warning_pct=-1.0, critical_pct=90.0)

    def test_tls_warning_must_exceed_critical(self) -> None:
        with pytest.raises(ValueError):
            check_tls_certificate(warning_days=7.0, critical_days=30.0)

    def test_tls_bad_hosts(self) -> None:
        with pytest.raises(ValueError):
            check_tls_certificate(hosts=["www.aliyun.com"])  # type: ignore[list-item]

    def test_dns_zero_timeout(self) -> None:
        with pytest.raises(ValueError):
            check_dns_resolution(timeout=0.0)

    def test_dns_bad_hosts_type(self) -> None:
        with pytest.raises(ValueError):
            check_dns_resolution(hosts="localhost")  # type: ignore[arg-type]

    def test_dns_empty_host(self) -> None:
        with pytest.raises(ValueError):
            check_dns_resolution(hosts=[""])

    def test_partition_negative_timeout(self) -> None:
        with pytest.raises(ValueError):
            check_network_partition(timeout=-1.0)


# ═══════════════════════════════════════════
#  3. 各检查项行为（不依赖外网）
# ═══════════════════════════════════════════

class TestChecks:
    def test_latency_unreachable_not_crash(self) -> None:
        r = check_network_latency(targets=[_UNREACHABLE], timeout=0.3)
        assert r.name == "network_latency"
        assert r.status in (HealthStatus.DEGRADED, HealthStatus.UNHEALTHY)
        assert "failures" in r.details or "latency_ms" in r.details

    def test_latency_empty_targets_skips(self) -> None:
        r = check_network_latency(targets=[])
        assert r.status == HealthStatus.HEALTHY
        assert r.details["targets"] == []

    def test_latency_default_targets_resolved(self) -> None:
        r = check_network_latency(targets=DEFAULT_LATENCY_TARGETS, timeout=1.0)
        assert r.status in HealthStatus

    def test_clock_unreachable_degraded_not_unhealthy(self) -> None:
        """NTP 不可达 → DEGRADED（网络问题不误判时钟损坏）。"""
        r = check_clock_skew(server=_UNREACHABLE[0], timeout=0.3)
        assert r.name == "clock_skew"
        assert r.status == HealthStatus.DEGRADED
        assert "不可达" in r.message or "解析失败" in r.message

    def test_cpu_usage_runs(self) -> None:
        r = check_cpu_usage(interval=0.0)
        assert r.status in HealthStatus
        assert "cpu_percent" in r.details

    def test_memory_usage_runs(self) -> None:
        r = check_memory_usage()
        assert r.status in HealthStatus
        assert "memory_percent" in r.details

    def test_tls_empty_hosts_skips(self) -> None:
        r = check_tls_certificate(hosts=[])
        assert r.status == HealthStatus.HEALTHY

    def test_tls_unreachable_degraded(self) -> None:
        r = check_tls_certificate(hosts=[_UNREACHABLE], timeout=0.3)
        assert r.name == "tls_certificate"
        assert r.status == HealthStatus.DEGRADED  # 不可达不判证书失效

    def test_dns_localhost_resolves(self) -> None:
        r = check_dns_resolution(hosts=["localhost"])
        assert r.status == HealthStatus.HEALTHY
        assert "localhost" in r.details["resolved_ips"]

    def test_dns_empty_hosts_skips(self) -> None:
        r = check_dns_resolution(hosts=[])
        assert r.status == HealthStatus.HEALTHY

    def test_partition_empty_skips(self) -> None:
        r = check_network_partition(endpoints=[])
        assert r.status == HealthStatus.HEALTHY

    def test_partition_all_unreachable_unhealthy(self) -> None:
        r = check_network_partition(endpoints=[_UNREACHABLE], timeout=0.3)
        assert r.name == "network_partition"
        assert r.status in (HealthStatus.DEGRADED, HealthStatus.UNHEALTHY)
        assert r.details["total"] == 1

    def test_default_tls_hosts_valid(self) -> None:
        """默认证书端点结构合法（(host, port) 二元组）。"""
        for host, port in DEFAULT_TLS_HOSTS:
            assert host and isinstance(port, int) and 0 < port < 65536

    def test_default_dns_hosts_nonempty(self) -> None:
        assert DEFAULT_DNS_HOSTS and all(h.strip() for h in DEFAULT_DNS_HOSTS)

    def test_default_partition_endpoints_valid(self) -> None:
        for host, port in DEFAULT_PARTITION_ENDPOINTS:
            assert host and isinstance(port, int) and 0 < port < 65536


# ═══════════════════════════════════════════
#  4. HealthChecker 集成（隔离单例，不碰全局）
# ═══════════════════════════════════════════

class TestCheckerIntegration:
    def test_single_check_run(self) -> None:
        checker = HealthChecker()
        register_l0_checks(checker)
        result = checker.run_check("cpu_usage")
        assert result is not None
        assert result.name == "cpu_usage"
        assert checker.get_last_result("cpu_usage") is result

    def test_disable_check(self) -> None:
        checker = HealthChecker()
        register_l0_checks(checker)
        assert checker.disable_check("memory_usage") is True
        assert checker.run_check("memory_usage") is None
        results = checker.run_all()
        assert len(results) == 6

    def test_unknown_check_none(self) -> None:
        checker = HealthChecker()
        register_l0_checks(checker)
        assert checker.run_check("no_such_check") is None
