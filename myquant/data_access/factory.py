# 文件名：myquant/data_access/factory.py
"""
factory.py - 数据获取器工厂（统一入口 + 自动优先级注册）
============================================================
数据层的统一入口工厂，按最优优先级自动注册数据源，上层无需关心底层有多少个源。

★★★ 数据源优先级（从高到低）★★★
  1. 通达信本地源 TdxDataSource   —— 纯本地读取，毫秒级，不依赖网络，回测主力
  2. akshare网络源 AkshareDataSource —— 在线备用，内部多源降级（新浪优先→东方财富）

核心能力:
  1. create_default_fetcher()  —— 创建默认数据获取器（通达信优先，akshare备用）
  2. create_fetcher(mode)      —— 按指定模式创建（auto/tdx/akshare）
  3. detect_tdx_available()    —— 检测通达信是否安装且可用
  4. 自动降级 —— 通达信不可用时自动跳过，只注册可用的源

设计原则:
  - 本地优先，在线兜底 —— 有本地通达信数据绝不用在线源
  - 自动检测 —— 通达信没安装自动跳过，不报错
  - 对上层透明 —— 调用方只看到一个 DataFetcher，不知道背后有多少源
  - 防未来函数 —— 所有查询严格传递 end_date
  - 为实盘预留 —— 实时行情场景可通过 mode 参数指定专用源

依赖: myquant.data_access.fetcher, myquant.data_access.sources
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from .fetcher import DataFetcher
from .sources.base import DataSourceStatus

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# 通达信安装路径（与 tdx_source.py 保持一致）
# ─────────────────────────────────────────────

DEFAULT_TDX_PATHS = [
    r"D:\TDX",
    r"C:\new_tdx",
    r"C:\new_tdx_2",
    r"D:\new_tdx",
    r"D:\通达信",
    r"C:\通达信",
    r"C:\Program Files\new_tdx",
    r"C:\Program Files (x86)\new_tdx",
]


# ─────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────

def detect_tdx_available() -> tuple[bool, Optional[str]]:
    """
    检测通达信是否安装且可用

    Returns
    -------
    tuple[bool, str | None] : (是否可用, 安装路径)
    """
    for tdx_path in DEFAULT_TDX_PATHS:
        path = Path(tdx_path)
        # 检查目录是否存在，且有 vipdoc 子目录（通达信数据目录标志）
        if path.exists() and (path / "vipdoc").exists():
            # 进一步检查是否有上海或深圳的日线数据
            sh_dir = path / "vipdoc" / "sh" / "lday"
            sz_dir = path / "vipdoc" / "sz" / "lday"
            has_data = sh_dir.exists() or sz_dir.exists()
            if has_data:
                return True, str(path)
            # 目录存在但没数据，也算安装了（可能用户还没下载数据）
            return True, str(path)
    return False, None


def _try_add_tdx_source(fetcher: DataFetcher) -> bool:
    """
    尝试添加通达信本地数据源

    Returns
    -------
    bool : 是否成功添加
    """
    try:
        from .sources.tdx_source import TdxDataSource
        tdx = TdxDataSource()
        # 快速检测：不调用 health_check（可能慢），直接检查安装路径
        available, tdx_path = detect_tdx_available()
        if available:
            fetcher.add_source(tdx)
            logger.info(f"通达信本地数据源已注册（路径: {tdx_path}，优先级最高）")
            return True
        else:
            logger.info("通达信未安装或无数据目录，跳过本地源（将使用akshare在线源）")
            return False
    except Exception as e:
        logger.warning(f"通达信数据源初始化失败: {type(e).__name__}: {e}，跳过本地源")
        return False


def _try_add_akshare_source(fetcher: DataFetcher) -> bool:
    """
    尝试添加akshare网络数据源

    Returns
    -------
    bool : 是否成功添加
    """
    try:
        from .sources.akshare_source import AkshareDataSource
        ak = AkshareDataSource()
        fetcher.add_source(ak)
        logger.info("akshare网络数据源已注册（内部多源降级：新浪优先→东方财富）")
        return True
    except Exception as e:
        logger.warning(f"akshare数据源初始化失败: {type(e).__name__}: {e}")
        return False


# ─────────────────────────────────────────────
# 工厂函数
# ─────────────────────────────────────────────

def create_default_fetcher(enable_cache: bool = True) -> DataFetcher:
    """
    创建默认数据获取器（通达信优先，akshare备用，自动降级）

    ★★★ 推荐使用此函数创建数据获取器 ★★★

    数据源优先级:
      1. 通达信本地源（自动检测，未安装则跳过）
      2. akshare网络源（内部多源降级：新浪优先→东方财富）

    Parameters
    ----------
    enable_cache : bool
        是否启用内存缓存，默认True

    Returns
    -------
    DataFetcher : 配置好的数据获取器

    Usage:
        from myquant.data_access import create_default_fetcher
        fetcher = create_default_fetcher()
        result = fetcher.get_market_data("000001.SZ", "2024-01-01", "2024-12-31")
    """
    fetcher = DataFetcher(enable_cache=enable_cache)

    # 1. 优先添加通达信本地源
    _try_add_tdx_source(fetcher)

    # 2. 添加akshare网络源（备用）
    _try_add_akshare_source(fetcher)

    # L5修复：删除永不触发的死分支（fetcher.source 恒为 CompositeDataSource）
    source_count = len(fetcher.source.sources) if hasattr(fetcher.source, 'sources') else 0
    if source_count == 0:
        logger.error("没有可用的数据源！请安装通达信或akshare")
    else:
        logger.info(f"数据获取器创建完成，共 {source_count} 个数据源（按优先级自动降级）")

    return fetcher


def create_fetcher(
    mode: str = "auto",
    enable_cache: bool = True,
) -> DataFetcher:
    """
    按指定模式创建数据获取器

    Parameters
    ----------
    mode : str
        数据源模式:
        - "auto"    : 自动模式（通达信优先，akshare备用，默认）
        - "tdx"     : 仅通达信本地源
        - "akshare" : 仅akshare网络源
    enable_cache : bool
        是否启用内存缓存

    Returns
    -------
    DataFetcher : 配置好的数据获取器

    Usage:
        from myquant.data_access import create_fetcher
        # 自动模式（推荐）
        fetcher = create_fetcher("auto")
        # 仅用通达信
        fetcher = create_fetcher("tdx")
        # 仅用akshare
        fetcher = create_fetcher("akshare")
    """
    fetcher = DataFetcher(enable_cache=enable_cache)

    if mode == "auto":
        # 自动模式：通达信优先，akshare备用
        _try_add_tdx_source(fetcher)
        _try_add_akshare_source(fetcher)
    elif mode == "tdx":
        # 仅通达信
        added = _try_add_tdx_source(fetcher)
        if not added:
            logger.error("指定仅用通达信，但通达信不可用！请检查安装路径")
    elif mode == "akshare":
        # 仅akshare
        _try_add_akshare_source(fetcher)
    else:
        raise ValueError(f"未知的数据源模式: {mode}，可选: auto/tdx/akshare")

    return fetcher


def get_available_sources() -> list[str]:
    """
    获取当前可用的数据源列表

    Returns
    -------
    list[str] : 可用数据源名称列表
    """
    available = []
    # 检测通达信
    tdx_ok, _ = detect_tdx_available()
    if tdx_ok:
        available.append("tdx")
    # 检测akshare
    try:
        import akshare  # noqa: F401
        available.append("akshare")
    except ImportError:
        pass
    return available
