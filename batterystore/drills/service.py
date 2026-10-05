"""台账装配。"""

from __future__ import annotations

import os

from ..store.jsonfile import JsonStore
from ..store.journal import JsonlJournal
from .ledger import HashLedger
from .register import DrillRegister


def build_register(storage_root: str) -> DrillRegister:
    """在存储目录下建立 ``drill-ledger`` 流水并装配台账。"""

    root = os.path.join(storage_root, "drills")
    store = JsonStore(root)
    journal = JsonlJournal(root, "drill-ledger", segment_limit=512)
    return DrillRegister(store, HashLedger(journal))
