"""定义 Orchestrator 计划、任务执行与复核共享的数据契约。"""

from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, Field, StringConstraints, model_validator

OrchestrationText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


class TaskType(StrEnum):
    """当前根图允许调度的任务类型。"""

    PLANNING = "planning"
    EXPLORE = "explore"
    RESEARCH = "research"
    HELPER = "helper"


class TaskSpec(BaseModel):
    """描述单个可由根图调度的任务。"""

    task_id: OrchestrationText
    task_type: TaskType
    instruction: OrchestrationText


class OrchestrationPlan(BaseModel):
    """保存单轮任务调度的目标与有限任务列表。"""

    goal: OrchestrationText
    tasks: list[TaskSpec] = Field(min_length=1, max_length=5)
    notes: str = ""


class TaskStatus(StrEnum):
    """表示模块任务的可消费执行结果。"""

    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class TaskResult(BaseModel):
    """记录单个任务的执行结果，供根图复核。"""

    task_id: OrchestrationText
    task_type: TaskType
    status: TaskStatus
    result: OrchestrationText


class ReviewAction(StrEnum):
    """限定复核节点在任务执行后的后续动作。"""

    CONTINUE = "continue"
    REPLACE_REMAINING = "replace_remaining"
    FINISH = "finish"


class PlanReviewDecision(BaseModel):
    """表示复核节点对当前任务计划作出的结构化决定。"""

    action: ReviewAction
    reason: OrchestrationText
    replacement_tasks: list[TaskSpec] = Field(default_factory=list)
    handoff_context: str = ""

    @model_validator(mode="after")
    def validate_replacement_tasks(self) -> Self:
        """确保替换任务只在替换动作中出现，且替换列表非空。"""
        if self.action is ReviewAction.REPLACE_REMAINING:
            if not 1 <= len(self.replacement_tasks) <= 5:
                raise ValueError("替换剩余计划时必须提供 1～5 个任务")
        elif self.replacement_tasks:
            raise ValueError("只有替换剩余计划时才能提供替换任务")
        if self.action is ReviewAction.FINISH:
            if self.handoff_context.strip():
                raise ValueError("结束任务计划时不能提供交接上下文")
        elif not self.handoff_context.strip():
            raise ValueError("继续执行任务时必须提供交接上下文")
        return self
