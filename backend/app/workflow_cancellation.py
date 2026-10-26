"""Server-side cancellation support for the multi-agent workflow (Stop button).

The long-running ``POST /api/process-requirements`` handler executes the whole
LangGraph pipeline (intent detection, gatherer/auditor/architect agents) inside
a single request. Pressing Stop in the UI must terminate that work *on the
server*, not merely make the browser stop waiting.

Mechanism
---------
1. At request start the handler registers its ``asyncio.Task`` here, keyed by
   ``project_id`` (see ``app.routes.requirements``).
2. ``POST /api/process-requirements/cancel?project_id=...`` looks the task up
   and calls ``task.cancel()``, which raises ``asyncio.CancelledError`` at the
   task's current await point — including mid-LLM-call, so the outbound LM
   Studio HTTP request is aborted too.
3. The handler catches ``CancelledError``, skips every persistence step
   (pending actions, assistant messages, SSE publishes) and returns a
   ``{"status": "cancelled"}`` response.

The registry is deliberately in-process: the app enforces a single uvicorn
worker (see ``app.event_manager.verify_single_worker_guarantee``), so a plain
dict keyed by project id is correct and lock-free on the single event loop.
"""
import asyncio
import logging
from typing import Dict

logger = logging.getLogger("app.workflow_cancellation")


class WorkflowCancellationRegistry:
    """Maps ``project_id`` to the asyncio task running its generation."""

    def __init__(self) -> None:
        self._tasks: Dict[str, asyncio.Task] = {}

    def register(self, project_id: str, task: asyncio.Task) -> None:
        """Record ``task`` as the in-flight generation for ``project_id``."""
        previous = self._tasks.get(project_id)
        if previous is not None and not previous.done():
            # Should be impossible via the UI (buttons are disabled while a run
            # is active), but stay correct if two clients race: the newer run
            # simply becomes the cancellable one.
            logger.warning(
                "[CANCEL REGISTRY] Overwriting an in-flight registration for project %s",
                project_id,
            )
        self._tasks[project_id] = task

    def unregister(self, project_id: str, task: asyncio.Task) -> None:
        """Drop the registration, but only if it still points at ``task``.

        The identity check prevents a finishing older run from clobbering the
        registration of a newer run that replaced it.
        """
        if self._tasks.get(project_id) is task:
            self._tasks.pop(project_id, None)

    def cancel(self, project_id: str) -> bool:
        """Terminate the in-flight generation for ``project_id``.

        Returns ``True`` when a live task was found and cancelled, ``False``
        when nothing was running (e.g. Stop pressed just after completion).
        Finished-but-unregistered leftovers are cleaned up lazily here.
        """
        task = self._tasks.get(project_id)
        if task is None or task.done():
            if project_id in self._tasks:
                self._tasks.pop(project_id, None)
            return False
        task.cancel()
        return True

    def is_running(self, project_id: str) -> bool:
        """Whether a live generation task is currently registered."""
        task = self._tasks.get(project_id)
        return task is not None and not task.done()


# Process-wide singleton consumed by the requirements route module.
workflow_cancellations = WorkflowCancellationRegistry()

# Document extraction is a separate long-running request from the LangGraph
# workflow, so it needs its own namespace. Keys include project + document IDs.
document_extraction_cancellations = WorkflowCancellationRegistry()
