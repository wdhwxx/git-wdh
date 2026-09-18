# 文件名：tests/unit/test_pickle_checker.py
"""
test_pickle_checker.py - P3-2 pickle 序列化预检回归单元测试
===========================================================
覆盖 myquant/utils/pickle_checker.py：
  - check_picklable   两阶段预检（静态扫描 + 真实往返）
  - is_picklable      布尔版判断
  - assert_picklable  断言（失败抛 PickleCheckError）
  - pickle_guard      上下文管理器（批量预检）
  - precheck_task / assert_task_picklable  进程池任务专项预检

测试维度:
  1. 可 pickle 对象：dict/list/int/str/tuple/模块级函数 → 通过
  2. 不可 pickle：lambda / 局部函数 / threading.Lock → 拒绝并定位
  3. strict=False：仅静态扫描；参数非法防御
  4. 断言与守卫：assert_picklable / pickle_guard（含空对象 ValueError）
  5. 任务预检：precheck_task / assert_task_picklable / 类型防御
  6. 大对象跳过：超过 max_test_bytes 明确 skipped 而非误判

运行方式:
  pytest tests/unit/test_pickle_checker.py -v
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from myquant.utils.pickle_checker import (
    PickleCheckError,
    _demo_top_level_task,
    assert_picklable,
    assert_task_picklable,
    check_picklable,
    is_picklable,
    pickle_guard,
    precheck_task,
)

# ═════════════════════════════════════════════
#  1. 可 pickle 对象
# ═════════════════════════════════════════════

class TestPicklableObjects:
    """可序列化对象应通过预检。"""

    @pytest.mark.parametrize(
        "obj",
        [
            {"a": 1, "b": [1, 2, 3]},
            [1, 2, 3, 4],
            42,
            "hello",
            (1, "x", 3.14),
            {"nested": {"deep": [{"list": [1, 2]}]}},
            _demo_top_level_task,  # 模块级顶层函数可按名引用
        ],
        ids=["dict", "list", "int", "str", "tuple", "nested", "module_func"],
    )
    def test_picklable_passes(self, obj: Any) -> None:
        """常见可序列化对象应 ok=True 且真实往返通过。"""
        result = check_picklable(obj)
        assert result.ok is True
        assert result.roundtrip_ok is True
        assert result.items == []

    def test_is_picklable_true(self) -> None:
        """is_picklable 对可序列化对象返回 True。"""
        assert is_picklable({"a": [1, 2, 3]}) is True


# ═════════════════════════════════════════════
#  2. 不可 pickle 对象
# ═════════════════════════════════════════════

class TestUnpicklableObjects:
    """不可序列化对象应被拒绝并给出定位。"""

    def test_lambda_rejected(self) -> None:
        """lambda 应被静态扫描拒绝。"""
        result = check_picklable(lambda x: x + 1)  # type: ignore[arg-type]
        assert result.ok is False
        assert result.items  # 至少一个问题项
        assert "lambda" in result.message.lower() or result.message != ""

    def test_local_function_rejected(self) -> None:
        """局部函数应被静态扫描拒绝。"""

        def _inner() -> int:
            return 1

        result = check_picklable(_inner)
        assert result.ok is False
        assert result.items

    def test_threading_lock_rejected(self) -> None:
        """threading.Lock 应被静态扫描拒绝。"""
        result = check_picklable(threading.Lock())
        assert result.ok is False
        assert result.items

    def test_nested_unpicklable_detected(self) -> None:
        """嵌套在容器内的 lambda 应被定位到路径。"""
        obj = {"safe": 1, "bad": lambda: None}  # type: ignore[dict-item]
        result = check_picklable(obj)
        assert result.ok is False
        assert any("bad" in item.path for item in result.items)

    def test_is_picklable_false(self) -> None:
        """is_picklable 对 lambda 返回 False。"""
        assert is_picklable(lambda: None) is False  # type: ignore[arg-type]

    def test_result_to_dict(self) -> None:
        """PickleCheckResult.to_dict 应可 JSON 序列化。"""
        import json

        result = check_picklable(lambda: None)  # type: ignore[arg-type]
        d = result.to_dict()
        assert d["ok"] is False
        json.dumps(d, ensure_ascii=False)


# ═════════════════════════════════════════════
#  3. strict / 参数防御
# ═════════════════════════════════════════════

class TestStrictAndValidation:
    """strict 模式与参数防御测试。"""

    def test_strict_false_skips_roundtrip(self) -> None:
        """strict=False 时仅静态扫描，roundtrip_skipped=True。"""
        result = check_picklable({"a": 1}, strict=False)
        assert result.ok is True
        assert result.roundtrip_skipped is True
        assert result.roundtrip_ok is None

    def test_strict_false_still_rejects_lambda(self) -> None:
        """strict=False 仍应静态拒绝 lambda。"""
        result = check_picklable(lambda: None, strict=False)  # type: ignore[arg-type]
        assert result.ok is False

    def test_empty_name_raises(self) -> None:
        """空 name 应抛出 ValueError。"""
        with pytest.raises(ValueError):
            check_picklable({"a": 1}, name="  ")

    def test_invalid_bounds_raise(self) -> None:
        """非法 max_test_bytes / max_depth / max_items 应抛出 ValueError。"""
        with pytest.raises(ValueError):
            check_picklable({"a": 1}, max_test_bytes=0)
        with pytest.raises(ValueError):
            check_picklable({"a": 1}, max_depth=0)
        with pytest.raises(ValueError):
            check_picklable({"a": 1}, max_items=0)

    def test_large_object_skipped(self) -> None:
        """超出 max_test_bytes 的可序列化对象应明确 skipped 而非误判失败。"""
        big = list(range(100_000))
        result = check_picklable(big, max_test_bytes=1)
        assert result.ok is True
        assert result.roundtrip_skipped is True
        assert result.roundtrip_ok is None


# ═════════════════════════════════════════════
#  4. 断言与守卫
# ═════════════════════════════════════════════

class TestAssertAndGuard:
    """断言与上下文守卫测试。"""

    def test_assert_picklable_passes(self) -> None:
        """可序列化对象不应抛异常。"""
        assert_picklable({"a": [1, 2]}, name="配置")

    def test_assert_picklable_raises(self) -> None:
        """不可序列化对象应抛出 PickleCheckError。"""
        with pytest.raises(PickleCheckError) as exc_info:
            assert_picklable(lambda: None, name="任务参数")  # type: ignore[arg-type]
        assert isinstance(exc_info.value.result, object)

    def test_pickle_guard_passes(self) -> None:
        """pickle_guard 应放行可序列化对象并产出原对象元组。"""
        with pickle_guard({"a": 1}, [1, 2, 3], name="扫描任务") as objs:
            assert objs[0] == {"a": 1}
            assert objs[1] == [1, 2, 3]

    def test_pickle_guard_raises_on_bad(self) -> None:
        """pickle_guard 遇不可序列化对象应抛出 PickleCheckError。"""
        with pytest.raises(PickleCheckError), pickle_guard(  # type: ignore[arg-type]
            {"ok": 1}, lambda: None, name="任务"
        ):
            pass  # pragma: no cover

    def test_pickle_guard_empty_raises(self) -> None:
        """pickle_guard 未传任何对象应抛出 ValueError。"""
        with pytest.raises(ValueError), pickle_guard():
            pass  # pragma: no cover


# ═════════════════════════════════════════════
#  5. 进程池任务专项预检
# ═════════════════════════════════════════════

class TestTaskPrecheck:
    """进程池任务 pickle 预检测试。"""

    def test_top_level_task_passes(self) -> None:
        """模块级顶层函数 + 可序列化参数应通过。"""
        result = precheck_task(_demo_top_level_task, args=(1, 2), kwargs={"b": 3})
        assert result.ok is True

    def test_lambda_task_fails(self) -> None:
        """lambda 任务函数应被拒绝。"""
        result = precheck_task(lambda a, b: a + b, args=(1, 2))  # type: ignore[arg-type]
        assert result.ok is False

    def test_unpicklable_arg_fails(self) -> None:
        """参数含不可序列化对象应被拒绝。"""
        result = precheck_task(_demo_top_level_task, args=(1, threading.Lock()))
        assert result.ok is False

    def test_non_callable_fn_raises(self) -> None:
        """fn 不可调用应抛出 TypeError。"""
        with pytest.raises(TypeError):
            precheck_task("not_callable", args=(1,))  # type: ignore[arg-type]

    def test_bad_args_type_raises(self) -> None:
        """args 非 list/tuple 应抛出 TypeError。"""
        with pytest.raises(TypeError):
            precheck_task(_demo_top_level_task, args=42)  # type: ignore[arg-type]

    def test_bad_kwargs_type_raises(self) -> None:
        """kwargs 非 dict/None 应抛出 TypeError。"""
        with pytest.raises(TypeError):
            precheck_task(_demo_top_level_task, kwargs=[1, 2])  # type: ignore[arg-type]

    def test_assert_task_picklable(self) -> None:
        """assert_task_picklable 通过/失败。"""
        assert_task_picklable(_demo_top_level_task, args=(1, 2))
        with pytest.raises(PickleCheckError):
            assert_task_picklable(lambda: None)  # type: ignore[arg-type]
