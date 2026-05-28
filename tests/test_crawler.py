"""
测试 crawler.py 的核心功能
"""
import pytest
import sys
import os
import json
from io import StringIO
from unittest.mock import patch

# 确保可以导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import crawler


class TestCrawlerCrawl:
    """测试 crawl 函数"""

    def test_crawl_empty_list(self):
        results = crawler.crawl([])
        assert results == []

    def test_crawl_single_code(self):
        """测试单个代码（会调用 watchlist，mock 掉网络）"""
        with patch.object(crawler.watchlist, 'classify_code', return_value='stock_a'):
            with patch.object(crawler.watchlist, 'strip_prefix', return_value='000333'):
                with patch.object(crawler.watchlist, 'fetch_stock_a_data', return_value=None):
                    with patch.object(crawler.watchlist, 'query_stock_a'):
                        results = crawler.crawl(["sz000333"])

        # 由于数据是 mock 的，结果应该显示 matched=False
        assert len(results) == 1
        assert results[0]["code"] == "sz000333"
        assert results[0]["matched"] is False


class TestCrawlerMain:
    """测试 crawler CLI 入口"""

    def test_main_with_codes_argument(self):
        """测试 --codes 参数"""
        with patch('sys.argv', ['crawler.py', '--codes', '["sz000333"]']):
            with patch('sys.stdout', new=StringIO()) as mock_stdout:
                crawler.main()
                output = mock_stdout.getvalue()
                # 应该是 JSON 输出
                results = json.loads(output)
                assert isinstance(results, list)

    def test_main_with_empty_stdin(self):
        """测试空 stdin 输入"""
        with patch('sys.argv', ['crawler.py']):
            with patch('sys.stdin', new=StringIO('')):
                with patch('sys.stdout', new=StringIO()) as mock_stdout:
                    crawler.main()
                    output = mock_stdout.getvalue()
                    assert output.strip() == '[]'

    def test_main_with_invalid_json(self):
        """测试无效 JSON 输入"""
        with patch('sys.argv', ['crawler.py']):
            with patch('sys.stdin', new=StringIO('not valid json')):
                with patch('sys.stderr', new=StringIO()) as mock_stderr:
                    with pytest.raises(SystemExit) as exc_info:
                        crawler.main()
                    assert exc_info.value.code == 1
