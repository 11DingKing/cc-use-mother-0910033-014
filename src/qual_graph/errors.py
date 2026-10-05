"""领域错误类型。"""
from __future__ import annotations


class DomainError(Exception):
    """所有领域错误的基类。"""

    status = 400
    code = "domain_error"


class ValidationError(DomainError):
    """输入或图谱内容不满足约束。"""

    status = 422
    code = "validation_error"


class PublishBlockedError(DomainError):
    """图谱草稿存在阻断性问题，不能发布。"""

    status = 422
    code = "publish_blocked"

    def __init__(self, issues: list[dict]):
        self.issues = issues
        super().__init__(f"图谱存在 {sum(1 for i in issues if i['level'] == 'error')} 个阻断问题")


class NotFoundError(DomainError):
    """引用的对象不存在。"""

    status = 404
    code = "not_found"


class ConflictError(DomainError):
    """操作与当前状态冲突（如状态机非法迁移、资格不达标）。"""

    status = 409
    code = "conflict"
