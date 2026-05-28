"""
测试 watchlist.py 的核心功能
"""
import pytest
import sys
import os

# 确保可以导入 watchlist
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import watchlist


class TestClassifyCode:
    """测试代码分类函数"""

    def test_a_stock_sz(self):
        assert watchlist.classify_code("sz000333") == "stock_a"

    def test_a_stock_sh(self):
        assert watchlist.classify_code("sh600887") == "stock_a"

    def test_a_stock_300xxx(self):
        assert watchlist.classify_code("sz300760") == "stock_a"

    def test_a_stock_688xxx(self):
        assert watchlist.classify_code("sh688001") == "stock_a"

    def test_hk_stock(self):
        assert watchlist.classify_code("hk00700") == "hk_stock"

    def test_etf_sz(self):
        assert watchlist.classify_code("sz159201") == "etf"

    def test_etf_sh(self):
        assert watchlist.classify_code("sh512040") == "etf"

    def test_fund_no_prefix(self):
        assert watchlist.classify_code("015600") == "fund"

    def test_fund_various(self):
        assert watchlist.classify_code("519915") == "fund"
        assert watchlist.classify_code("050025") == "fund"


class TestStripPrefix:
    """测试前缀去除函数"""

    def test_strip_sz(self):
        assert watchlist.strip_prefix("sz000333") == "000333"

    def test_strip_sh(self):
        assert watchlist.strip_prefix("sh600887") == "600887"

    def test_strip_hk(self):
        assert watchlist.strip_prefix("hk00700") == "00700"

    def test_strip_no_prefix(self):
        assert watchlist.strip_prefix("015600") == "015600"


class TestToFloat:
    """测试安全浮点数转换"""

    def test_none(self):
        assert watchlist.to_float(None) is None

    def test_int(self):
        assert watchlist.to_float(42) == 42.0

    def test_float(self):
        assert watchlist.to_float(3.14) == 3.14

    def test_string_float(self):
        assert watchlist.to_float("3.14") == 3.14

    def test_string_with_percent(self):
        assert watchlist.to_float("+2.5%") == 2.5

    def test_string_with_space(self):
        assert watchlist.to_float(" 3.14 ") == 3.14

    def test_invalid_string(self):
        assert watchlist.to_float("abc") is None

    def test_nan(self):
        import math
        assert watchlist.to_float(math.nan) is None


class TestHoldingItem:
    """测试持仓单品 dataclass"""

    def test_create_holding_item(self):
        item = watchlist.HoldingItem(
            raw_code="sz000333",
            name="美的集团",
            category="stock_a",
            stripped="000333",
        )
        assert item.raw_code == "sz000333"
        assert item.matched is False
        assert item.price is None

    def test_holding_item_defaults(self):
        item = watchlist.HoldingItem(
            raw_code="sz000333",
            name="美的集团",
            category="stock_a",
            stripped="000333",
        )
        assert item.matched is False
        assert item.price is None
        assert item.change_pct is None
        assert item.date is None
        assert item.extra == {}


class TestRetryDecorator:
    """测试重试装饰器"""

    def test_retry_success_first_try(self):
        call_count = 0

        @watchlist.retry(max_attempts=3, delay=0.1)
        def succeed_once():
            nonlocal call_count
            call_count += 1
            return "success"

        result = succeed_once()
        assert result == "success"
        assert call_count == 1

    def test_retry_success_after_failures(self):
        call_count = 0

        @watchlist.retry(max_attempts=3, delay=0.1)
        def fail_twice_then_succeed():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("simulated failure")
            return "success"

        result = fail_twice_then_succeed()
        assert result == "success"
        assert call_count == 3

    def test_retry_all_fail(self):
        call_count = 0

        @watchlist.retry(max_attempts=3, delay=0.1)
        def always_fail():
            nonlocal call_count
            call_count += 1
            raise ConnectionError("always fails")

        with pytest.raises(ConnectionError):
            always_fail()
        assert call_count == 3


class TestTimeoutDecorator:
    """测试超时装饰器"""

    def test_timeout_success(self):
        @watchlist.with_timeout(2)
        def quick_function():
            return "done"

        result = quick_function()
        assert result == "done"

    def test_timeout_expires(self):
        import time

        @watchlist.with_timeout(1)
        def slow_function():
            time.sleep(3)
            return "done"

        with pytest.raises(watchlist.TimeoutError) as exc_info:
            slow_function()
        assert "timed out" in str(exc_info.value)
