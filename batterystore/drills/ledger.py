"""带哈希链的追加台账。

演练台账的每一类事件（要求、计划、尝试、证据、补救）都落到同一条 JSONL 流水，
按追加顺序串成 SHA-256 链：后一条记录的哈希覆盖前一条哈希。计划一旦追加即不
提供任何修改或删除入口；检查时 :meth:`HashLedger.verify` 逐条重算，断链或被
改写字节都会暴露。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from ..errors import StorageError
from ..store.journal import JsonlJournal

GENESIS_HASH = "0" * 64


def digest_payload(payload: dict) -> str:
    """对载荷做规范序列化后取 SHA-256。"""

    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ChainBrokenError(StorageError):
    """哈希链校验失败：记录被改写或缺失。"""


class HashLedger:
    """单主题追加流水 + 逐条哈希链。"""

    def __init__(self, journal: JsonlJournal) -> None:
        self._journal = journal

    def append(self, event_type: str, payload: dict) -> dict:
        """追加一条事件，自动补序号、时间戳与哈希链字段。"""

        existing = self._journal.count()
        body: dict[str, Any] = dict(payload)
        body["event_type"] = event_type
        body["seq"] = existing
        body["logged_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        body["prev_hash"] = self._tail_hash() if existing else GENESIS_HASH
        body["hash"] = digest_payload(body)
        self._journal.append(body)
        return body

    def records(self) -> list[dict]:
        """按追加顺序返回全部记录。"""

        return self._journal.read_all()

    def _tail_hash(self) -> str:
        records = self._journal.read_all()
        if not records:
            return GENESIS_HASH
        return str(records[-1]["hash"])

    def verify(self) -> dict:
        """重放整条链，返回校验结果与断链位置。"""

        records = self.records()
        previous = GENESIS_HASH
        for index, record in enumerate(records):
            stored = str(record.get("hash", ""))
            candidate = {key: value for key, value in record.items() if key != "hash"}
            expected = digest_payload(candidate)
            if record.get("prev_hash") != previous or stored != expected:
                raise ChainBrokenError(f"哈希链在第 {index} 条断裂（{record.get('seq')}）", component="drills")
            previous = stored
        return {"records": len(records), "tail_hash": previous, "ok": True}
