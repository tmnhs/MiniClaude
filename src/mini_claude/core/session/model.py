from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

SessionStatus = Literal["active", "waiting_for_input", "closed"]
SessionMode = Literal["one_shot", "chat"]

# session_id 会被拼进 ~/.mc/sessions/<id>，限制字符集防止路径穿越
SESSION_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"


@dataclass
class Session:
    id: str
    mode: SessionMode
    status: SessionStatus
    title: str
    created_at: str
    updated_at: str
    run_ids: list[str] = field(default_factory=list)
    # 会话的工作目录（绝对路径）；工具/沙箱/审批以此为根，空串表示旧会话（回退 daemon cwd）
    workspace: str = ""

    # 将 Session 转为可写入 meta.json 的普通 dict
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "mode": self.mode,
            "status": self.status,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "run_ids": list(self.run_ids),
            "workspace": self.workspace,
        }

    # 从 meta.json 的 dict 还原 Session 对象
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        return cls(
            id=str(data["id"]),
            mode=data["mode"],
            status=data["status"],
            title=str(data.get("title", "")),
            created_at=str(data["created_at"]),
            updated_at=str(data["updated_at"]),
            run_ids=[str(x) for x in data.get("run_ids", [])],
            workspace=str(data.get("workspace", "")),
        )