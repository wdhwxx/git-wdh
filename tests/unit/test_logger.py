# 文件名：tests/unit/test_logger.py
"""
test_logger.py - 统一日志配置与 data_quality 专用通道单元测试
================================================================
覆盖范围:
  1. data_quality 子目录自动创建
  2. 命名空间路由: data_clean.* / live.* / data_quality 进入专用通道
  3. 级别阈值: WARNING 及以上落盘, INFO 不落盘
  4. source 字段自动标注: offline(离线清洗) / live(实盘) / api
  5. symbol 字段: 显式 extra 优先, 否则从消息正则提取
  6. 双写机制: 告警同时进入全量日志与 data_quality 文件
  7. 无关命名空间不进入专用通道
  8. 统一告警 API: log_data_quality_alert / get_data_quality_logger
"""

from __future__ import annotations

import logging

import pytest

import myquant.core.logger as logger_mod
from myquant.core.logger import (
    DATA_QUALITY_FMT,
    DataQualityFilter,
    DataQualityFormatter,
    get_data_quality_logger,
    log_data_quality_alert,
    setup_logging,
)


@pytest.fixture(autouse=True)
def _reset_logger_state():
    """
    每个用例前后重置全局日志状态

    logger.setup_logging 有"仅初始化一次"保护，测试需要反复初始化，
    因此每个用例独立重置标记并清理 handler，避免用例间互相污染。
    """
    logger_mod._logging_initialized = False
    root = logging.getLogger()
    for h in list(root.handlers):
        h.close()
    root.handlers.clear()
    yield
    logger_mod._logging_initialized = False
    for h in list(root.handlers):
        h.close()
    root.handlers.clear()


def _read_dq_log(tmp_path):
    """读取 data_quality 目录中唯一的日志文件内容"""
    files = sorted((tmp_path / "data_quality").glob("*.log"))
    assert len(files) == 1, f"期望 1 个 data_quality 日志文件，实际 {files}"
    return files[0].read_text(encoding="utf-8")


class TestDataQualityChannelSetup:
    """data_quality 通道初始化"""

    def test_creates_data_quality_subdir(self, tmp_path):
        """setup_logging 应自动创建 logs/data_quality/ 子目录"""
        setup_logging(
            log_dir=tmp_path, console=False, file_output=True,
            trade_log=False, data_quality_log=True,
        )
        assert (tmp_path / "data_quality").is_dir()

    def test_disabled_creates_no_subdir(self, tmp_path):
        """data_quality_log=False 时不应创建子目录与专用 handler"""
        setup_logging(
            log_dir=tmp_path, console=False, file_output=True,
            trade_log=False, data_quality_log=False,
        )
        assert not (tmp_path / "data_quality").exists()

    def test_handler_registered_on_root(self, tmp_path):
        """启用后 root logger 应挂载带 DataQualityFilter 的 handler"""
        setup_logging(
            log_dir=tmp_path, console=False, file_output=True,
            trade_log=False, data_quality_log=True,
        )
        dq_handlers = [
            h for h in logging.getLogger().handlers
            if any(isinstance(f, DataQualityFilter) for f in h.filters)
        ]
        assert len(dq_handlers) == 1


class TestDataQualityRouting:
    """命名空间路由与级别阈值"""

    def test_offline_warning_routed(self, tmp_path):
        """data_clean.* 的 WARNING 应进入专用通道, source=offline"""
        setup_logging(
            log_dir=tmp_path, console=False, file_output=True,
            trade_log=False, data_quality_log=True,
        )
        logging.getLogger("data_clean.bar").warning("价格跳变 22.5%: 600519.SH")
        content = _read_dq_log(tmp_path)
        assert "offline" in content
        assert "600519.SH" in content
        assert "价格跳变" in content

    def test_live_warning_routed(self, tmp_path):
        """live.* 的 WARNING 应进入专用通道, source=live"""
        setup_logging(
            log_dir=tmp_path, console=False, file_output=True,
            trade_log=False, data_quality_log=True,
        )
        logging.getLogger("live.data_feed").warning(
            "[data_feed] 脏数据告警: 000001.SZ errors=['OHLC非法']",
            extra={"source": "live", "symbol": "000001.SZ"},
        )
        content = _read_dq_log(tmp_path)
        assert "live" in content
        assert "000001.SZ" in content

    def test_info_not_routed(self, tmp_path):
        """INFO 级别不应进入专用通道（阈值=WARNING）"""
        setup_logging(
            log_dir=tmp_path, console=False, file_output=True,
            trade_log=False, data_quality_log=True,
        )
        logging.getLogger("data_clean.bar").info("清洗完成: 有效 100 条")
        content = _read_dq_log(tmp_path)
        assert content == ""

    def test_unrelated_namespace_not_routed(self, tmp_path):
        """无关命名空间（如 myquant.backtest）的 WARNING 不应进入专用通道"""
        setup_logging(
            log_dir=tmp_path, console=False, file_output=True,
            trade_log=False, data_quality_log=True,
        )
        logging.getLogger("myquant.backtest").warning("回测回撤超阈值")
        content = _read_dq_log(tmp_path)
        assert content == ""

    def test_error_routed(self, tmp_path):
        """ERROR 级别（高于阈值）应进入专用通道"""
        setup_logging(
            log_dir=tmp_path, console=False, file_output=True,
            trade_log=False, data_quality_log=True,
        )
        logging.getLogger("data_clean.tick").error("Tick清洗严重异常: 000858.SZ")
        content = _read_dq_log(tmp_path)
        assert "ERROR" in content
        assert "000858.SZ" in content


class TestStructuredFields:
    """source / symbol 结构化字段"""

    def test_symbol_auto_extracted(self, tmp_path):
        """未显式指定 symbol 时，从消息文本自动提取 6 位标的代码"""
        setup_logging(
            log_dir=tmp_path, console=False, file_output=True,
            trade_log=False, data_quality_log=True,
        )
        logging.getLogger("data_clean.bar").warning(
            "成交量异常放大 120.0x (curr=200, prev=1): 601398"
        )
        content = _read_dq_log(tmp_path)
        lines = [ln for ln in content.splitlines() if ln.strip()]
        assert lines, "应有告警记录"
        # DATA_QUALITY_FMT: 时间|级别|source|symbol|消息
        assert "| 601398 |" in lines[0]

    def test_symbol_dash_when_absent(self, tmp_path):
        """消息中无标的代码时，symbol 字段填 '-'"""
        setup_logging(
            log_dir=tmp_path, console=False, file_output=True,
            trade_log=False, data_quality_log=True,
        )
        logging.getLogger("data_clean.pipeline").warning("清洗配置加载失败")
        content = _read_dq_log(tmp_path)
        assert "| - |" in content

    def test_explicit_extra_symbol_priority(self, tmp_path):
        """显式 extra 的 symbol 优先于消息正则提取"""
        setup_logging(
            log_dir=tmp_path, console=False, file_output=True,
            trade_log=False, data_quality_log=True,
        )
        logging.getLogger("live.data_feed").warning(
            "聚合窗口异常",
            extra={"source": "live", "symbol": "002594.SZ"},
        )
        content = _read_dq_log(tmp_path)
        assert "| 002594.SZ |" in content


class TestUnifiedAlertApi:
    """统一告警 API（实时行情/实盘无缝对接入口）"""

    def test_log_data_quality_alert(self, tmp_path):
        """log_data_quality_alert 应写入专用通道"""
        setup_logging(
            log_dir=tmp_path, console=False, file_output=True,
            trade_log=False, data_quality_log=True,
        )
        log_data_quality_alert(
            "实时行情时间戳超前被拒绝", source="live", symbol="601398.SH"
        )
        content = _read_dq_log(tmp_path)
        assert "live" in content
        assert "601398.SH" in content
        assert "时间戳超前" in content

    def test_alert_error_level(self, tmp_path):
        """统一 API 支持 ERROR 级别上报"""
        setup_logging(
            log_dir=tmp_path, console=False, file_output=True,
            trade_log=False, data_quality_log=True,
        )
        log_data_quality_alert(
            "行情源断连检出缺失bar", source="live",
            symbol="600036.SH", level=logging.ERROR,
        )
        content = _read_dq_log(tmp_path)
        assert "ERROR" in content

    def test_get_data_quality_logger_writes(self, tmp_path):
        """get_data_quality_logger 返回的 logger 应可写专用通道"""
        setup_logging(
            log_dir=tmp_path, console=False, file_output=True,
            trade_log=False, data_quality_log=True,
        )
        get_data_quality_logger().warning(
            "手动上报", extra={"source": "api", "symbol": "000002.SZ"}
        )
        content = _read_dq_log(tmp_path)
        assert "000002.SZ" in content


class TestDoubleWrite:
    """双写机制：专用通道不影响既有日志输出"""

    def test_alert_also_in_all_log(self, tmp_path):
        """清洗告警应同时出现在全量日志与 data_quality 文件中"""
        setup_logging(
            log_dir=tmp_path, console=False, file_output=True,
            trade_log=False, data_quality_log=True,
        )
        logging.getLogger("data_clean.bar").warning("OHLC非法: 600519")
        all_logs = sorted(tmp_path.glob("myquant_*.log"))
        all_content = "".join(
            f.read_text(encoding="utf-8") for f in all_logs
            if "error" not in f.name
        )
        assert "OHLC非法" in all_content, "全量日志应包含该告警"
        assert "OHLC非法" in _read_dq_log(tmp_path), "专用通道应包含该告警"

    def test_dq_channel_only_warnings(self, tmp_path):
        """全量日志收 INFO，但专用通道只收 WARNING 及以上"""
        setup_logging(
            log_dir=tmp_path, console=False, file_output=True,
            trade_log=False, data_quality_log=True,
        )
        logging.getLogger("data_clean.bar").info("开始清洗 600519")
        logging.getLogger("data_clean.bar").warning("脏数据: 600519")
        all_logs = [
            f for f in tmp_path.glob("myquant_*.log")
            if "error" not in f.name
        ]
        all_content = "".join(
            f.read_text(encoding="utf-8") for f in all_logs
        )
        assert "开始清洗" in all_content, "全量日志应包含 INFO"
        dq = _read_dq_log(tmp_path)
        assert "开始清洗" not in dq, "专用通道不应包含 INFO"
        assert "脏数据" in dq


class TestFormatterRobustness:
    """DataQualityFormatter 健壮性"""

    def test_format_without_fields(self):
        """缺少 source/symbol 字段时，格式化器应填 '-' 而不抛异常"""
        formatter = DataQualityFormatter(DATA_QUALITY_FMT)
        record = logging.LogRecord(
            name="data_quality", level=logging.WARNING,
            pathname=__file__, lineno=1, msg="无字段告警",
            args=None, exc_info=None,
        )
        output = formatter.format(record)
        assert "| - | - |" in output
        assert "无字段告警" in output

    def test_filter_rejects_unrelated(self):
        """DataQualityFilter 应拒绝无关命名空间"""
        f = DataQualityFilter()
        record = logging.LogRecord(
            name="myquant.strategy", level=logging.WARNING,
            pathname=__file__, lineno=1, msg="普通告警",
            args=None, exc_info=None,
        )
        assert f.filter(record) is False
