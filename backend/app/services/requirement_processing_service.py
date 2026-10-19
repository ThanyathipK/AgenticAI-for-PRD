"""Application boundary for authenticated requirement workflow execution."""

import asyncio
import logging
from typing import Any, Awaitable, Callable

from fastapi import status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthenticatedUser
from app.schemas import ProcessRequirementsRequest, ProcessRequirementsResponse
from app.workflow_cancellation import workflow_cancellations

logger = logging.getLogger("app.services.requirement_processing")

Pipeline = Callable[
    [ProcessRequirementsRequest, AsyncSession, AuthenticatedUser],
    Awaitable[ProcessRequirementsResponse],
]
AccessVerifier = Callable[[str, AuthenticatedUser, AsyncSession], Awaitable[Any]]


class RequirementProcessingService:
    """Authorize, register, execute, and clean up one workflow request."""

    def __init__(self, *, pipeline: Pipeline, verify_access: AccessVerifier) -> None:
        self._pipeline = pipeline
        self._verify_access = verify_access

    async def process(
        self,
        request: ProcessRequirementsRequest,
        session: AsyncSession,
        current_user: AuthenticatedUser,
    ) -> ProcessRequirementsResponse:
        project_id = str(request.project_id)
        await self._verify_access(project_id, current_user, session)

        current_task = asyncio.current_task()
        if current_task is not None:
            workflow_cancellations.register(project_id, current_task)
        try:
            return await self._pipeline(request, session, current_user)
        except asyncio.CancelledError:
            logger.warning(
                "[CANCELLED] Generation for project %s was stopped; skipping result persistence.",
                project_id,
            )
            await session.rollback()
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "status": "cancelled",
                    "project_id": project_id,
                    "message": (
                        "Generation was stopped by the user before completion. "
                        "No results were saved."
                    ),
                },
            )
        finally:
            if current_task is not None:
                workflow_cancellations.unregister(project_id, current_task)
