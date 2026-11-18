from __future__ import annotations


class PermissionDeniedError(Exception):
    """当工具调用被权限管理器拒绝时抛出。"""
