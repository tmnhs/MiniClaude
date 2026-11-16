from __future__ import annotations

from datetime import UTC, datetime

# 返回当前 UTC 时间的 ISO 8601 字符串
def now_iso() -> str:
    return datetime.now(UTC).isoformat()
