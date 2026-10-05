"""演练证据柜。

证据只有"追加"一种写法：每次演练编排产生的联动报告（探测读数、阀门行程、
确认时刻、PCS 跳闸、违约项）直接固化成一份 JSON 证据；现场补传的照片、签到
表按字节落盘。每份证据同时记录 SHA256 指纹与字节数，检查时用
:meth:`EvidenceVault.verify` 重新算指纹，文件被改过或删掉都能发现。证据目录
按演练批次分文件，不提供覆盖和删除入口。
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from ..errors import StorageError

EVIDENCE_KIND_REPORT = "linkage_report"
EVIDENCE_KIND_FIELD = "field_attachment"


class EvidenceVault:
    """按目录存放、只追加的演练证据柜。"""

    def __init__(self, root: str) -> None:
        self._root = os.path.abspath(root)
        os.makedirs(self._root, exist_ok=True)

    @property
    def root(self) -> str:
        return self._root

    def path_of(self, evidence_id: str) -> str:
        """证据标识只允许数字序号，避免路径穿越。"""

        if not evidence_id.isdigit():
            raise StorageError(f"证据标识非法: {evidence_id}", component="drill")
        prefix = os.path.join(self._root, f"evidence-{int(evidence_id):06d}")
        matches = [path for path in (f"{prefix}.json", f"{prefix}.bin") if os.path.isfile(path)]
        if not matches:
            raise StorageError(f"证据不存在: {evidence_id}", component="drill")
        return matches[0]

    def _next_id(self) -> int:
        used = [
            int(name.split(".")[0].split("-")[1])
            for name in os.listdir(self._root)
            if name.startswith("evidence-")
        ]
        return (max(used) + 1) if used else 1

    def _write_bytes(self, evidence_id: int, data: bytes, suffix: str) -> str:
        path = os.path.join(self._root, f"evidence-{evidence_id:06d}.{suffix}")
        if os.path.exists(path):
            raise StorageError(f"证据文件已存在，禁止覆盖: {path}", component="drill")
        with open(path, "xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        return path

    def attach_report(self, run_id: str, report: dict[str, Any]) -> dict:
        """把一次联动编排报告固化成 JSON 证据。"""

        evidence_id = self._next_id()
        payload = {"run_id": run_id, "kind": EVIDENCE_KIND_REPORT, "report": report}
        data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        path = self._write_bytes(evidence_id, data, "json")
        return self._descriptor(evidence_id, run_id, EVIDENCE_KIND_REPORT, path, data, filename="linkage-report.json")

    def attach_bytes(
        self, run_id: str, data: bytes, *, filename: str, content_type: str, note: str = ""
    ) -> dict:
        """落盘一份现场补传的证据字节（照片、签到表扫描件等）。"""

        safe = os.path.basename(filename).strip()
        if not safe or safe in (".", ".."):
            raise StorageError("证据文件名非法", component="drill")
        evidence_id = self._next_id()
        path = self._write_bytes(evidence_id, bytes(data), "bin")
        return self._descriptor(
            evidence_id,
            run_id,
            EVIDENCE_KIND_FIELD,
            path,
            bytes(data),
            filename=safe,
            content_type=content_type,
            note=note,
        )

    def _descriptor(
        self,
        evidence_id: int,
        run_id: str,
        kind: str,
        path: str,
        data: bytes,
        *,
        filename: str,
        content_type: str = "application/json",
        note: str = "",
    ) -> dict:
        return {
            "evidence_id": f"{evidence_id:06d}",
            "run_id": run_id,
            "kind": kind,
            "filename": filename,
            "content_type": content_type,
            "note": note,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "path": path,
        }

    def verify(self, descriptor: dict) -> dict:
        """按描述符重新核对文件是否存在、字节数与指纹是否一致。"""

        evidence_id = str(descriptor["evidence_id"])
        path = self.path_of(evidence_id)
        if not os.path.isfile(path):
            return {"evidence_id": evidence_id, "ok": False, "reason": "missing"}
        with open(path, "rb") as handle:
            data = handle.read()
        digest = hashlib.sha256(data).hexdigest()
        if len(data) != int(descriptor["bytes"]):
            return {"evidence_id": evidence_id, "ok": False, "reason": "size_mismatch"}
        if digest != descriptor["sha256"]:
            return {"evidence_id": evidence_id, "ok": False, "reason": "hash_mismatch"}
        return {"evidence_id": evidence_id, "ok": True, "sha256": digest}
