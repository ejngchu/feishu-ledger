"""
测试 feishu_sync.py 的核心功能

测试 feishu_base.LarkClient 和合并后的 feishu_sync.sync
"""
import pytest
import sys
import os
import json
from io import StringIO
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import feishu_base
import feishu_sync


class TestLarkClientDecodeOutput:
    """测试 LarkClient._decode_output"""

    def test_decode_utf8(self):
        client = feishu_base.LarkClient("token")
        data = "你好".encode("utf-8")
        assert client._decode_output(data) == "你好"

    def test_decode_gbk(self):
        client = feishu_base.LarkClient("token")
        data = "你好".encode("gbk")
        assert client._decode_output(data) == "你好"

    def test_decode_fallback(self):
        client = feishu_base.LarkClient("token")
        data = b"\xff\xfe\xfd\xfc"
        result = client._decode_output(data)
        assert isinstance(result, str)


class TestLarkClientRunLark:
    """测试 LarkClient._run_lark"""

    def test_file_not_found(self):
        client = feishu_base.LarkClient("token")
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            with pytest.raises(RuntimeError) as exc_info:
                client._run_lark(["base", "+record-list"])
            assert "未找到" in str(exc_info.value)

    def test_timeout(self):
        import subprocess
        client = feishu_base.LarkClient("token")
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 60)):
            with pytest.raises(RuntimeError) as exc_info:
                client._run_lark(["base", "+record-list"])
            assert "超时" in str(exc_info.value)

    def test_success(self):
        client = feishu_base.LarkClient("token")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = json.dumps({"data": {"record_id_list": []}}).encode("utf-8")
        mock_proc.stderr = b""
        with patch("subprocess.run", return_value=mock_proc):
            result = client._run_lark(["base", "+record-list"])
            assert result == {"data": {"record_id_list": []}}


class TestLarkClientGetRecords:
    """测试 LarkClient.get_records"""

    def test_empty(self):
        client = feishu_base.LarkClient("token")
        mock_result = {
            "data": {
                "record_id_list": [],
                "field_id_list": [],
                "data": [],
                "has_more": False,
            }
        }
        with patch.object(client, "_run_lark", return_value=mock_result):
            records = client.get_records("tblXXX", {"代码": "fld1", "名称": "fld2"})
            assert records == []

    def test_single_record(self):
        client = feishu_base.LarkClient("token")
        mock_result = {
            "data": {
                "record_id_list": ["rec_001"],
                "field_id_list": ["fld1", "fld2"],
                "data": [["sz000333", "美的集团"]],
                "has_more": False,
            }
        }
        with patch.object(client, "_run_lark", return_value=mock_result):
            records = client.get_records("tblXXX", {"代码": "fld1", "名称": "fld2"})
            assert len(records) == 1
            assert records[0]["代码"] == "sz000333"
            assert records[0]["名称"] == "美的集团"
            assert records[0]["_record_id"] == "rec_001"


class TestLarkClientUpsertRecord:
    """测试 LarkClient.upsert_record"""

    def test_no_data(self):
        client = feishu_base.LarkClient("token")
        with patch("sys.stdout", new=StringIO()) as mock_out:
            result = client.upsert_record("tblXXX", "rec_001", {}, dry_run=True, verbose=True)
            assert result is True
            output = mock_out.getvalue()
            assert "SKIP" in output

    def test_dry_run(self):
        client = feishu_base.LarkClient("token")
        with patch("sys.stdout", new=StringIO()) as mock_out:
            result = client.upsert_record(
                "tblXXX", "rec_001",
                {"fld1": 100.0},
                dry_run=True, verbose=True
            )
            assert result is True
            assert "DRY-RUN" in mock_out.getvalue()


class TestSignalHandlers:
    """测试信号处理器"""

    def test_setup_signal_handlers(self):
        feishu_base.setup_signal_handlers()


class TestSyncZixuan:
    """测试自选表同步逻辑 (sync_watchlist)"""

    @pytest.fixture
    def mock_crawler(self):
        """Mock crawler.crawl 返回固定数据"""
        return [
            {"code": "sz000333", "matched": True, "price": 80.0,
             "change_pct": "+1.00%", "date": "2026-05-28"},
            {"code": "hk00700", "matched": True, "price": 400.0,
             "change_pct": "-0.50%", "date": "2026-05-28"},
        ]

    def test_sync_watchlist_empty_records(self):
        """自选表为空时直接返回"""
        client = feishu_base.LarkClient("token")
        with patch.object(client, "get_records", return_value=[]):
            codes, price_map = feishu_sync.sync_watchlist(
                client, dry_run=True, force=False,
                rate_limit=0, verbose=False, on_error="skip",
                today="2026-05-28"
            )
            assert codes == []
            assert price_map == {}

    def test_sync_watchlist_dry_run(self, mock_crawler):
        """dry-run 模式不实际调用 lark-cli"""
        mock_records = [
            {"_record_id": "r1", "代码": "sz000333", "更新日期": "2026-05-27"},
            {"_record_id": "r2", "代码": "hk00700", "更新日期": None},
        ]
        client = feishu_base.LarkClient("token")
        with patch.object(client, "get_records", return_value=mock_records):
            with patch("crawler.crawl", return_value=mock_crawler):
                with patch.object(client, "upsert_record", return_value=True) as mock_upsert:
                    codes, price_map = feishu_sync.sync_watchlist(
                        client, dry_run=True, force=False,
                        rate_limit=0, verbose=False, on_error="skip",
                        today="2026-05-28"
                    )
                    assert "sz000333" in codes
                    assert "hk00700" in codes
                    assert price_map["sz000333"] == 80.0
                    # dry_run=True 时 upsert_record 仍被调用（内部提前返回，不调用 lark-cli）
                    assert mock_upsert.call_count == 2
                    for call in mock_upsert.call_args_list:
                        assert call.kwargs["dry_run"] is True


class TestSyncChicang:
    """测试持仓表同步逻辑 (sync_holdings)"""

    def test_sync_holdings_empty_records(self):
        """持仓表为空时直接返回"""
        client = feishu_base.LarkClient("token")
        with patch.object(client, "get_records", return_value=[]):
            feishu_sync.sync_holdings(
                client, code_to_price={"sz000333": 80.0},
                dry_run=True, force=False,
                rate_limit=0, verbose=False, on_error="skip",
                today="2026-05-28"
            )
            # 无异常即通过

    def test_sync_holdings_calculates_profit(self):
        """验证市值和收益计算正确"""
        from feishu_constants import HOLDINGS_FIELD_IDS
        mock_chicang = [
            {
                "_record_id": "cr1",
                "代码": "sz000333",
                "名称": "美的集团",
                "总份额": 100.0,
                "总成本": 7000.0,
                "市值": None,
                "持有收益": None,
                "持有收益率": None,
            }
        ]
        client = feishu_base.LarkClient("token")
        upsert_calls = []

        def mock_upsert(table_id, record_id, fields, dry_run=False, verbose=True):
            upsert_calls.append(fields)
            return True

        with patch.object(client, "get_records", return_value=mock_chicang):
            with patch.object(client, "upsert_record", side_effect=mock_upsert):
                feishu_sync.sync_holdings(
                    client,
                    code_to_price={"sz000333": 80.0},
                    dry_run=False, force=True,
                    rate_limit=0, verbose=False, on_error="skip",
                    today="2026-05-28"
                )

        # 市值 = 80.0 * 100 = 8000, 收益 = 8000 - 7000 = 1000, 收益率 = 1000/7000 * 100 = 14.29%
        # fields 使用 field IDs 作为 key（来自 HOLDINGS_FIELD_IDS）
        assert len(upsert_calls) == 1
        assert upsert_calls[0][HOLDINGS_FIELD_IDS["市值"]] == 8000.0
        assert upsert_calls[0][HOLDINGS_FIELD_IDS["持有收益"]] == 1000.0
        assert upsert_calls[0][HOLDINGS_FIELD_IDS["持有收益率"]] == "14.29%"

    def test_sync_holdings_no_price(self):
        """自选表中无价格时跳过"""
        mock_chicang = [
            {
                "_record_id": "cr1",
                "代码": "sz000333",
                "名称": "美的集团",
                "总份额": 100.0,
                "总成本": 7000.0,
            }
        ]
        client = feishu_base.LarkClient("token")
        with patch.object(client, "get_records", return_value=mock_chicang):
            with patch.object(client, "upsert_record") as mock_upsert:
                feishu_sync.sync_holdings(
                    client,
                    code_to_price={},  # 无价格
                    dry_run=False, force=True,
                    rate_limit=0, verbose=False, on_error="skip",
                    today="2026-05-28"
                )
                mock_upsert.assert_not_called()


class TestSyncIntegration:
    """测试完整 sync 流程"""

    def test_sync_full_flow_dry_run(self):
        """完整流程 dry-run 测试（mock 所有外部依赖）"""
        mock_zixuan_records = [
            {"_record_id": "zr1", "代码": "sz000333", "更新日期": "2026-05-27"},
        ]
        mock_holdings_records = [
            {
                "_record_id": "cr1",
                "代码": "sz000333",
                "名称": "美的集团",
                "总份额": 100.0,
                "总成本": 7000.0,
                "市值": 7800.0,
                "持有收益": 800.0,
                "持有收益率": "11.43%",
            }
        ]
        mock_crawl_results = [
            {"code": "sz000333", "matched": True, "price": 80.0,
             "change_pct": "+1.00%", "date": "2026-05-28"},
        ]

        client = feishu_base.LarkClient("token")

        def mock_get_records(table_id, field_ids):
            if table_id == "tblIP0LuVvZFMjZD":
                return mock_zixuan_records
            else:
                return mock_holdings_records

        with patch.object(client, "get_records", side_effect=mock_get_records):
            with patch("crawler.crawl", return_value=mock_crawl_results):
                with patch.object(client, "upsert_record", return_value=True) as mock_upsert:
                    feishu_sync.sync(dry_run=True, force=False, verbose=False)

                    # dry_run 不调用 upsert
                    mock_upsert.assert_not_called()
