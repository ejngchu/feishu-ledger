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
        self._cleanup_files: list[str] = []

    def _cleanup_temp(self):
        import os as _os
        for f in self._cleanup_files:
            try:
                _os.remove(f)
            except OSError:
                pass
        self._cleanup_files.clear()

    def _decode_output(self, data: bytes) -> str:
        """尝试用 UTF-8 解码，回退到 GBK"""
        for enc in ("utf-8", "gbk", "gb2312", "utf-16"):
            try:
                return data.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return data.decode("utf-8", errors="replace")

    def _write_json_temp(self, content: str) -> str:
        """将 JSON 内容写入 CWD 临时文件，返回 @filename 引用"""
        import os as _os
        import tempfile
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False,
            dir=_os.getcwd(), encoding="utf-8"
        )
        tmp.write(content)
        tmp.close()
        fname = _os.path.basename(tmp.name)
        self._cleanup_files.append(fname)
        return f"@{fname}"

    def _run_lark(self, args: list[str], rate_limit: float = None) -> dict:
        """
        调用 lark-cli 并返回解析后的 JSON dict。
        遇到限流时会自动重试一次。
        Windows 上不使用 shell=True，避免 JSON {} 被 shell 解析破坏。
        """
        if rate_limit is None:
            rate_limit = self.upsert_delay

        # lark-cli --json 直接接受 JSON 字符串（无需临时文件）
        safe_args = []
        for a in args:
            if a is None:
                continue
            safe_args.append(a)

        try:
            for attempt in range(2):
                try:
                    proc = subprocess.run(
                        [_lark_cli] + safe_args,
                        capture_output=True,
                        text=False,
                        timeout=60,
                    )
                except subprocess.TimeoutExpired:
                    raise RuntimeError(f"lark-cli 超时: {' '.join(safe_args)}")
                except FileNotFoundError:
                    raise RuntimeError(
                        f"未找到 {_lark_cli}，请确保已安装: npm install -g @lark-opdev/lark-cli"
                    )

                stdout = self._decode_output(proc.stdout)
                stderr = self._decode_output(proc.stderr)

                if proc.returncode != 0:
                    err = stderr.strip() or "(no stderr)"
                    is_rate_limit = (proc.returncode == 429 or "rate limit" in err.lower() or "太频繁" in err)
                    if is_rate_limit and attempt == 0:
                        time.sleep(rate_limit * 2)
                        continue
                    raise RuntimeError(f"lark-cli 失败 (exit={proc.returncode}): {err}")

                try:
                    return json.loads(stdout)
                except json.JSONDecodeError as e:
                    raise RuntimeError(f"lark-cli 输出解析失败: {e}\n输出: {stdout[:500]}")
        finally:
            self._cleanup_temp()

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

    def upsert_batch(
        self,
        table_id: str,
        records: list[dict],
        dry_run: bool = False,
        verbose: bool = True,
    ) -> tuple[int, int]:
        """
        逐条 upsert 记录（使用 lark-cli +record-upsert）。
        `+record-batch-update` 是同值批量（同一 patch 应用到多条记录），不适合我们的异值场景。

        参数:
            table_id:  目标 table ID
            records:   [{"record_id": str, "fields": {field_id: value}}, ...]
            dry_run:   是否仅预览
            verbose:   是否打印日志

        返回:
            (成功数, 失败数)
        """
        if not records:
            return 0, 0

        if dry_run:
            if verbose:
                for r in records:
                    print(f"  [DRY-RUN] 更新 {r['record_id']}: {json.dumps(r['fields'], ensure_ascii=False)}")
            return len(records), 0

        ok_count, fail_count = 0, 0
        for i, rec in enumerate(records):
            if _interrupted:
                if verbose:
                    print("  [INTERRUPT] 收到中断信号，停止写入", file=sys.stderr)
                break
            fields = rec.get("fields", {})
            if not fields:
                ok_count += 1
                continue
            cell_json = json.dumps(fields, ensure_ascii=False)
            args = [
                "base", "+record-upsert",
                "--base-token", self.base_token,
                "--table-id", table_id,
                "--record-id", rec["record_id"],
                "--json", cell_json,
                "--as", "user",
            ]
            try:
                self._run_lark(args)
                ok_count += 1
                if i < len(records) - 1:
                    time.sleep(self.upsert_delay)
            except RuntimeError as e:
                if verbose:
                    print(f"  [FAIL] {rec['record_id']}: {e}", file=sys.stderr)
                fail_count += 1

        return ok_count, fail_count

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
        ]
        # 新建记录（record_id=None）不传 --record-id flag
        if record_id is not None:
            args += ["--record-id", record_id]
        args += [
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

    def verify_fields(self, table_id: str, expected_fields: dict) -> bool:
        """
        校验飞书表格的字段 ID 是否与代码中的预期配置一致。

        参数:
            table_id:       目标 table ID
            expected_fields: 预期字段映射，格式 {"字段名": "field_id", ...}

        返回:
            True 表示校验通过（全部匹配），False 表示有 mismatch。
        """
        args = [
            "base", "+field-list",
            "--base-token", self.base_token,
            "--table-id", table_id,
            "--as", "user",
        ]
        try:
            result = self._run_lark(args)
        except RuntimeError as e:
            print(f"  [FAIL] 获取字段列表失败: {e}", file=sys.stderr)
            return False

        actual_fields = result.get("data", {}).get("fields", [])
        actual_by_id = {f["id"]: f["name"] for f in actual_fields}

        ok = True
        for name, fid in expected_fields.items():
            if fid in actual_by_id:
                print(f"  [OK]  {name} → {fid}")
            else:
                actual_name = actual_by_id.get(fid, "(不存在)")
                print(f"  [MISSING]  {name} → 预期 {fid}，实际字段名: {actual_name}")
                ok = False

        # 检查代码中未配置的字段（warning）
        configured_fids = set(expected_fields.values())
        for f in actual_fields:
            if f["id"] not in configured_fids:
                print(f"  [EXTRA]  字段 \"{f['name']}\" (id={f['id']}) 未在代码中配置")

        return ok


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
