"""
飞书 Base 共享基础设施 - 供 feishu_sync.py 使用
"""
import argparse
import json
import platform
import shutil
import signal
import subprocess
import sys
import time
from typing import Optional

_lark_cli = "lark-cli.cmd" if platform.system() == "Windows" else "lark-cli"
if shutil.which(_lark_cli) is None and platform.system() == "Windows":
    _lark_cli = "lark-cli.cmd"

# 全局中断标志
_interrupted = False


def setup_signal_handlers():
    """设置信号处理器，支持优雅退出"""
    def handler(signum, frame):
        global _interrupted
        print("\n收到中断信号，正在优雅退出...", file=sys.stderr)
        _interrupted = True
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


class LarkClient:
    """
    lark-cli 调用封装，支持 rate-limit 自动重试和编码自动回退。
    """

    def __init__(self, base_token: str, upsert_delay: float = 0.8):
        self.base_token = base_token
        self.upsert_delay = upsert_delay

    def _decode_output(self, data: bytes) -> str:
        """尝试用 UTF-8 解码，回退到 GBK"""
        for enc in ("utf-8", "gbk", "gb2312", "utf-16"):
            try:
                return data.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return data.decode("utf-8", errors="replace")

    def _run_lark(self, args: list[str], rate_limit: float = None) -> dict:
        """
        调用 lark-cli 并返回解析后的 JSON dict。
        遇到限流时会自动重试一次。
        """
        if rate_limit is None:
            rate_limit = self.upsert_delay
        cmd = [_lark_cli] + args

        for attempt in range(2):
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=False,
                    timeout=60,
                )
            except subprocess.TimeoutExpired:
                raise RuntimeError(f"lark-cli 超时: {' '.join(cmd)}")
            except FileNotFoundError:
                raise RuntimeError(
                    f"未找到 {_lark_cli}，请确保已安装: npm install -g @lark-opdev/lark-cli"
                )

            stdout = self._decode_output(proc.stdout)
            stderr = self._decode_output(proc.stderr)

            if proc.returncode != 0:
                err = stderr.strip() or "(no stderr)"
                is_rate_limit = (proc.returncode == 429 or "rate limit" in err.lower())
                if is_rate_limit and attempt == 0:
                    time.sleep(rate_limit * 2)
                    continue
                raise RuntimeError(f"lark-cli 失败 (exit={proc.returncode}): {err}")

            try:
                return json.loads(stdout)
            except json.JSONDecodeError as e:
                raise RuntimeError(f"lark-cli 输出解析失败: {e}\n输出: {stdout[:500]}")

    def get_records(self, table_id: str, field_ids: dict, page_size: int = 200) -> list[dict]:
        """
        泛化记录读取，支持任意 table/field_ids。

        参数:
            table_id:   飞书多维表格 table ID
            field_ids:  字段名到 field_id 的映射，格式 {"字段名": "field_id", ...}
            page_size:  每页记录数（默认 200）

        返回:
            [{name: value, ..., _record_id: str}, ...]
        """
        records = []
        offset = 0

        while True:
            args = [
                "base", "+record-list",
                "--base-token", self.base_token,
                "--table-id", table_id,
                "--as", "user",
                "--format", "json",
                "--limit", str(page_size),
            ]
            if offset > 0:
                args += ["--offset", str(offset)]

            result = self._run_lark(args)
            data = result.get("data", {})

            record_ids = data.get("record_id_list", [])
            field_id_list = data.get("field_id_list", [])
            rows = data.get("data", [])

            field_index = {fid: idx for idx, fid in enumerate(field_id_list)}
            idx_map = {name: field_index.get(fid) for name, fid in field_ids.items()}

            for i, record_id in enumerate(record_ids):
                row = rows[i] if i < len(rows) else []
                rec = {
                    name: (row[idx] if idx is not None and idx < len(row) else None)
                    for name, idx in idx_map.items()
                }
                rec["_record_id"] = record_id
                records.append(rec)

            if not data.get("has_more", False):
                break
            offset += page_size

        return records

    def upsert_record(
        self,
        table_id: str,
        record_id: str,
        fields: dict,
        dry_run: bool = False,
        verbose: bool = True,
    ) -> bool:
        """
        泛化单条 upsert。

        参数:
            table_id:   目标 table ID
            record_id:  记录 ID
            fields:     要更新的字段，格式 {field_id: value}
            dry_run:    是否仅预览
            verbose:    是否打印日志

        返回:
            True 表示成功（或 dry-run），False 表示失败。
        """
        if not fields:
            if verbose:
                print(f"  [SKIP] {record_id}: 无数据可更新")
            return True

        cell_json = json.dumps(fields, ensure_ascii=False)

        if dry_run:
            if verbose:
                print(f"  [DRY-RUN] 更新 {record_id}: {cell_json}")
            return True

        args = [
            "base", "+record-upsert",
            "--base-token", self.base_token,
            "--table-id", table_id,
            "--record-id", record_id,
            "--json", cell_json,
            "--as", "user",
        ]

        try:
            self._run_lark(args)
            return True
        except RuntimeError as e:
            if verbose:
                print(f"  [FAIL] 更新 {record_id} 失败: {e}", file=sys.stderr)
            return False


def add_common_args(parser: argparse.ArgumentParser, upsert_delay_default: float = 0.8):
    """为 argparse 添加 feishu_sync 共享参数"""
    parser.add_argument(
        "--dry-run", action="store_true",
        help="预览模式（不实际写入）"
    )
    parser.add_argument(
        "--force", action="store_true",
        help="强制更新所有记录（忽略日期检查）"
    )
    parser.add_argument(
        "--rate-limit", type=float, default=upsert_delay_default,
        help=f"写入间隔秒数（默认 {upsert_delay_default}s）"
    )
    parser.add_argument(
        "--on-error", choices=["skip", "abort"], default="skip",
        help="遇错处理：skip（跳过继续）或 abort（终止）（默认 skip）"
    )
    parser.add_argument(
        "--quiet", "-q", action="store_true",
        help="静默模式"
    )
