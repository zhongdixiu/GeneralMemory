from __future__ import annotations

from memory_cmic.api_schemas import TaskResponse
from memory_cmic.models import MemoryTask


def to_task_response(task: MemoryTask) -> TaskResponse:
    status = "pending" if task.status in {"pending", "processing"} else task.status
    if status == "pending":
        return TaskResponse(task_id=task.id, status="pending", results=None, error=None)
    results = task.result_json or []
    error = task.error_json if status in {"partial", "failed", "cancelled"} else None
    if status in {"partial", "failed", "cancelled"} and error is None:
        error = {"code": "TASK_FAILED", "message": task.last_error or "task did not complete"}
    return TaskResponse(
        task_id=task.id, status=status, results=results, error=error,
        processing_results=task.payload.get("execution", {}).get("processing_results"),
    )
