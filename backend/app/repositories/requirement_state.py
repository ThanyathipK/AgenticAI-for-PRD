"""
RequirementState repository operations.

Dedicated persistence repository isolating all Supabase/PostgreSQL database
operations from the multi-agent system.
"""
import logging
import uuid
from typing import Any, Dict, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AcceptanceCriteriaModel,
    AuditResultModel,
    ClarificationQuestionModel,
    EpicModel,
    ProjectModel,
    RequirementModel,
    RequirementStateModel,
    UserStoryModel,
)
from app.repositories.acceptance_criteria import AcceptanceCriteriaRepository
from app.repositories.audit_result import AuditResultRepository
from app.repositories.base import as_uuid
from app.repositories.clarification_question import ClarificationQuestionRepository
from app.repositories.epic import EpicRepository
from app.repositories.prd import PRDDocumentRepository, PRDVersionRepository
from app.repositories.user_story import UserStoryRepository
from app.requirement_codes import UNASSIGNED_REQUIREMENT_CODE, default_requirement_code

logger = logging.getLogger(__name__)


# PROJECT state persistence map — the single read/write entry point for a project's
# aggregated requirement board. Steps:
#   2.3.2 / 2.3.3.2 — get_by_project_id / save_or_update ← GET  /api/project/{id}
#                     (PROJECT 2.3.2 read, PROJECT 2.3.3.2 first-open bookkeeping)
#   8.2.2           — save_or_update                      ← PUT  /api/project/{id}
#   (reused)        — save_or_update                      ← CONFIRMATION flow
#                     (confirm_action is the AI write path; see §7.8 of the analysis)
# DB: requirement_states + epics / requirements / user_stories / acceptance_criteria
#     / audit_results / clarification_questions / prd_documents.
class RequirementStateRepository:
    """Dedicated persistence repository for the project RequirementState."""

    # PROJECT 2.3.2 — READ: SELECT requirement_states WHERE project_id (None when
    # the project has no state yet) and rebuild the flattened board from the
    # normalized child tables. Read-only: no locks, no writes; the session is
    # supplied by the request (get_db) for transactional consistency.
    @staticmethod
    async def get_by_project_id(project_id: str, session: AsyncSession) -> Optional[Dict[str, Any]]:
        """
        Loads the latest RequirementState for a given project_id.
        Requires an active session to maintain transactional consistency.
        """
        pid = as_uuid(project_id)
        return await RequirementStateRepository._get_by_project_id_impl(pid, session)

    @staticmethod
    async def _get_by_project_id_impl(project_id: uuid.UUID, session: AsyncSession) -> Optional[Dict[str, Any]]:
        # ProjectModel.name is the only persisted project-name source.  Joining
        # it here means every consumer (chat, diagrams and PRD generation) sees
        # a rename immediately, even for state created before the rename.
        stmt = (
            select(RequirementStateModel, ProjectModel.name, ProjectModel.status)
            .join(ProjectModel, ProjectModel.id == RequirementStateModel.project_id)
            .where(RequirementStateModel.project_id == project_id)
        )
        result = await session.execute(stmt)
        row = result.one_or_none()
        if not row:
            return None
        model, project_name, project_status = row

        # Load active epic directly from epics table
        active_epic = await EpicRepository.get_active_by_project(str(project_id), session)
        epic_name = active_epic["epic_name"] if active_epic else ""

        # Load ALL active requirements for this project (multi-requirement support)
        stmt_req = select(RequirementModel).where(
            RequirementModel.project_id == project_id,
            func.lower(RequirementModel.status) == "active"
        ).order_by(RequirementModel.created_at.asc())
        res_req = await session.execute(stmt_req)
        reqs = res_req.scalars().all()
        req_ids = [r.id for r in reqs]

        version_num = model.version_number

        # If no project-level active epic was found, resolve the epic name in a
        # single batched IN query over the distinct requirement epics instead of
        # querying per-requirement (previous behavior did N separate lookups).
        if not epic_name:
            epic_ids = {r.epic_id for r in reqs if r.epic_id}
            if epic_ids:
                stmt_epics = select(EpicModel).where(EpicModel.id.in_(epic_ids))
                res_epics = await session.execute(stmt_epics)
                epics_by_id = {e.id: e for e in res_epics.scalars().all()}
                for req in reqs:
                    if req.epic_id in epics_by_id:
                        epic_name = epics_by_id[req.epic_id].epic_name
                        break

        # Batch-load ALL user stories + acceptance criteria for every active
        # requirement in one round-trip each. This removes the previous N+1
        # cascade (per requirement it issued a story + criteria query).
        stories_by_req = await UserStoryRepository.get_by_requirement_ids(req_ids, session)
        acceptance_by_req = await AcceptanceCriteriaRepository.get_by_requirement_ids(req_ids, str(project_id), session)

        # Build the requirements list with their user stories
        all_user_stories = []
        all_acceptance_criteria = []
        requirements_list = []

        for req in reqs:
            req_id = req.id

            stories = stories_by_req.get(req_id, [])
            ac_list = acceptance_by_req.get(req_id, [])

            active_stories = [s for s in stories if s.get("status", "active") == "active"]
            active_ac_list = [ac for ac in ac_list if ac.get("status", "active") == "active"]

            flat_ac = [ac["criteria_text"] for ac in active_ac_list]

            # Map acceptance criteria to stories
            for story in active_stories:
                story_ac = [ac["criteria_text"] for ac in active_ac_list if ac["ticket_code"] == story["ticket_code"]]
                story["acceptance_criteria"] = story_ac

            # Build requirement entry
            req_entry = {
                "id": str(req.id),
                "requirement_code": req.requirement_code,
                "title": req.title,
                "description": req.description or "",
                "user_stories": active_stories,
                "is_locked": bool(req.is_locked) if req.is_locked is not None else False,
                "locked_by": req.locked_by,
                "locked_at": req.locked_at.isoformat() if req.locked_at else None,
            }
            requirements_list.append(req_entry)
            all_user_stories.extend(active_stories)
            all_acceptance_criteria.extend(flat_ac)

        # Fallback if no epic found in epics table but present in model.requirements
        if not epic_name and model.requirements:
            if isinstance(model.requirements, dict):
                epic_name = model.requirements.get("epic_name", "")
            elif isinstance(model.requirements, list) and len(model.requirements) > 0:
                epic_name = model.requirements[0].get("epic_name", "")
            if epic_name:
                saved_epic = await EpicRepository.save_or_update_epic(str(project_id), epic_name, session, version=version_num)
                epic_name = saved_epic["epic_name"]

        if not requirements_list:
            # Fallback: use the old flat model.user_stories if no requirements found
            all_user_stories = model.user_stories or []
            all_acceptance_criteria = model.acceptance_criteria or []
            if model.requirements and isinstance(model.requirements, dict) and "epic_name" in model.requirements:
                epic_name = model.requirements["epic_name"]

        # CLARIFY 4.1 — Read path of the clarification questions the UI renders: the
        #             project-state load (PROJECT 2.3.2) pulls them through the
        #             dedicated repository (4.2) and flattens them into
        #             requirement_state.clarification_questions, which is what the
        #             CLARIFY 1.1/1.2 forms list and CLARIFY 3.4 later indexes.
        cqs = await ClarificationQuestionRepository.get_by_project(str(project_id), session)
        audits = await AuditResultRepository.get_by_project(str(project_id), session)
        latest_audit = audits[0] if audits else {}

        # The preview must ALWAYS surface the latest PRD version, so resolve the
        # document from the NEWEST stored row (at or before the current version)
        # instead of exact-matching the version number: version counters advance
        # independently of document writes (requirement merges and confirmed AI
        # generations pin the ledger ahead of the document rows), so an exact
        # match can find no row at all — which used to make this payload return
        # an empty PRD and the preview fall back to the template skeleton even
        # though a perfectly good document was stored.
        latest_prd = await PRDDocumentRepository.get_latest_for_project(
            str(project_id), session, max_version=version_num
        )
        latest_ledger = await PRDVersionRepository.get_latest(str(project_id), session)

        # Pick the newest NON-EMPTY document.  Requirement merges can create an
        # empty prd_documents row at an older version while a later Architect /
        # regeneration stores the real document in the immutable ledger and on
        # requirement_states.  Treating mere row existence as authoritative made
        # both the PRD and Mermaid preview disappear after reload.
        document_prd = (latest_prd or {}).get("prd_markdown") or ""
        ledger_prd = (latest_ledger or {}).get("generated_prd") or ""
        model_prd = model.generated_prd or ""
        document_version = int((latest_prd or {}).get("version") or 0)
        ledger_version = int((latest_ledger or {}).get("version_number") or 0)
        if ledger_prd and ledger_version >= document_version:
            generated_prd = ledger_prd
        else:
            generated_prd = document_prd or model_prd or ledger_prd

        # The version ledger currently stores only PRD text.  Prefer the newest
        # document diagram when present, then the materialized state written by
        # Architect/regeneration.  Never replace a real diagram with an older
        # empty document field.
        generated_diagrams = (
            (latest_prd or {}).get("mermaid_diagram")
            or model.generated_diagrams
            or ""
        )

        return {
            "project_id": str(model.project_id),
            "project_name": project_name,
            "project_status": project_status,
            "requirements": requirements_list if requirements_list else [],
            "business_goals": model.business_goals,
            "actors": model.actors,
            "user_stories": all_user_stories if all_user_stories else [],
            "acceptance_criteria": all_acceptance_criteria if all_acceptance_criteria else [],
            "clarification_questions": cqs if cqs else [],
            "validation_status": model.validation_status,
            "passed_checks": latest_audit.get("passed_checks", []),
            "failed_checks": latest_audit.get("failed_checks", []),
            "audit_findings": latest_audit.get("findings", []),
            "audit_source_references": latest_audit.get("source_references", []),
            "audit_verdict": latest_audit.get("verdict", "needs_clarification"),
            "audit_project_context": latest_audit.get("project_context", {}),
            "audit_checklist_id": latest_audit.get("checklist_id", "banking-core"),
            "audit_checklist_version": latest_audit.get("checklist_version", "1.0.0"),
            "generated_prd": generated_prd if generated_prd else "",
            "generated_diagrams": generated_diagrams if generated_diagrams else "",
            "current_workflow_state": model.current_workflow_state,
            "requirements_revision": model.requirements_revision,
            "version_number": version_num,
            "updated_at": model.__dict__.get("updated_at").isoformat() if model.__dict__.get("updated_at") else None
        }

    # PROJECT 8.2.2 (and 2.3.3.2, plus the CONFIRMATION flow's confirm_action) —
    # WRITE: create-if-missing, else partial update. Never clears a JSON blob whose
    # list was not supplied, so an unrelated partial update cannot wipe the board.
    # The caller owns the transaction; the request's get_db commits at the end.
    @staticmethod
    async def save_or_update(project_id: str, updates: Dict[str, Any], session: AsyncSession) -> Dict[str, Any]:
        """
        Saves or partially updates the RequirementState for a given project_id.
        Does not overwrite unchanged fields. Uses partial updates whenever possible.
        Requires an active session to maintain transactional consistency.
        """
        pid = as_uuid(project_id)
        res = await RequirementStateRepository._save_or_update_impl(pid, updates, session)
        return res

    @staticmethod
    async def _save_or_update_impl(project_id: uuid.UUID, updates: Dict[str, Any], session: AsyncSession) -> Dict[str, Any]:
        project_name = await session.scalar(
            select(ProjectModel.name).where(ProjectModel.id == project_id)
        )
        if project_name is None:
            raise ValueError(f"Project {project_id} does not exist")

        stmt = select(RequirementStateModel).where(RequirementStateModel.project_id == project_id)
        result = await session.execute(stmt)
        model = result.scalar_one_or_none()

        # PROJECT 8.2.2.1 — Create branch: first write for this project (INSERT
        # requirement_states with empty collections). The route decides whether any
        # requirements travel with it — the PROJECT 2.3.3.2 init sends none on
        # purpose, so the board stays empty and numbering starts at REQ-001.
        if not model:
            model = RequirementStateModel(
                project_id=project_id,
                requirements={},
                business_goals=updates.get("business_goals", []),
                actors=updates.get("actors", []),
                user_stories=[],
                acceptance_criteria=[],
                clarification_questions=[],
                validation_status=updates.get("validation_status", "pending"),
                generated_prd="",
                generated_diagrams="",
                current_workflow_state=updates.get("current_workflow_state", "gatherer_node"),
                requirements_revision=updates.get("requirements_revision", 1),
                version_number=updates.get("version_number", 1)
            )
            session.add(model)
            await session.flush()
        # PROJECT 8.2.2.2 — Existing row: partial update — only the top-level fields
        # present in `updates` are assigned, and each JSON blob that is about to be
        # re-derived is cleared just before the child tables are rebuilt below (this
        # is what stops deleted requirements/stories from surviving a re-save).
        else:
            if "validation_status" in updates and updates["validation_status"] is not None:
                model.validation_status = updates["validation_status"]
            if "current_workflow_state" in updates and updates["current_workflow_state"] is not None:
                model.current_workflow_state = updates["current_workflow_state"]
            if "requirements_revision" in updates and updates["requirements_revision"] is not None:
                model.requirements_revision = updates["requirements_revision"]
            if "version_number" in updates and updates["version_number"] is not None:
                model.version_number = updates["version_number"]
            if "business_goals" in updates and updates["business_goals"] is not None:
                model.business_goals = updates["business_goals"]
            if "actors" in updates and updates["actors"] is not None:
                model.actors = updates["actors"]

            # Only clear JSON blob fields that are being re-derived from relation tables.
            # This avoids wasteful clearing and prevents data loss if re-derivation fails mid-way.
            if "requirements" in updates and updates["requirements"] is not None:
                model.requirements = {}
            if "user_stories" in updates and updates["user_stories"] is not None:
                model.user_stories = []
            if "acceptance_criteria" in updates and updates["acceptance_criteria"] is not None:
                model.acceptance_criteria = []
            if "clarification_questions" in updates and updates["clarification_questions"] is not None:
                model.clarification_questions = []
            # generated_prd / generated_diagrams are persisted verbatim — the
            # caller passes the FINAL merged document (e.g. the manual-edit
            # merge of "previous version + this edit", or the AI regeneration).
            # Clearing them here wiped the preview after every manual save
            # (the frontend reloads project state and read generated_prd).
            if "generated_prd" in updates and updates["generated_prd"] is not None:
                model.generated_prd = updates["generated_prd"]
            if "generated_diagrams" in updates and updates["generated_diagrams"] is not None:
                model.generated_diagrams = updates["generated_diagrams"]

        version_num = model.version_number

        # ===============================================
        # MULTI-REQUIREMENT SAVE LOGIC
        # Parse updates.requirements or updates.user_stories to handle multiple requirements
        # ===============================================

        # Extract the list of requirements to persist
        # Only touch the requirement / user-story / criteria tables when the
        # caller actually passes requirement payload. Bookkeeping-only saves
        # (e.g. the PRD version ledger's version_number-only sync) must NEVER
        # re-derive the requirements: the old default-requirement fallback
        # renamed REQ-001 to "Default Requirement" and ARCHIVED every other
        # requirement on every version-only save.
        has_requirement_payload = (
            ("requirements" in updates and updates["requirements"] is not None)
            or ("user_stories" in updates and updates["user_stories"] is not None)
        )
        # Touched-ID tracking lives outside the guard so the post-block
        # propagation section can safely iterate an empty set when the caller
        # sent no requirement payload (version-only / audit-only saves).
        touched_req_ids = set()
        if has_requirement_payload:
            req_items = []
            if "requirements" in updates and updates["requirements"] is not None:
                reqs_data = updates["requirements"]
                if isinstance(reqs_data, list):
                    # New multi-requirement format: [{"requirement_code": "REQ-001", "title": "...", "user_stories": [...]}, ...]
                    req_items = reqs_data
                elif isinstance(reqs_data, dict):
                    # Legacy flat format: {"epic_name": "...", "user_stories": [...]}
                    # Convert real legacy content to one requirement. An empty
                    # legacy object represents an empty board and must not
                    # fabricate REQ-001.
                    legacy_epic = reqs_data.get("epic_name", "")
                    legacy_stories = reqs_data.get("user_stories", []) if "user_stories" in reqs_data else updates.get("user_stories", [])
                    if legacy_epic or legacy_stories:
                        req_items = [{
                            "requirement_code": default_requirement_code(0),
                            "title": legacy_epic or project_name,
                            "description": "",
                            "user_stories": legacy_stories if isinstance(legacy_stories, list) else []
                        }]
            elif "user_stories" in updates and updates["user_stories"] is not None:
                # Legacy flat stories need a parent requirement because of the
                # relational schema. Empty stories, however, mean an empty board.
                legacy_stories = updates["user_stories"] if isinstance(updates["user_stories"], list) else []
                if legacy_stories:
                    req_items = [{
                        "requirement_code": default_requirement_code(0),
                        "title": project_name,
                        "description": "",
                        "user_stories": legacy_stories,
                    }]

            # Determine epic name from first requirement or updates
            epic_name_val = ""
            if "requirements" in updates and isinstance(updates["requirements"], dict):
                epic_name_val = updates["requirements"].get("epic_name", "")
            if not epic_name_val and req_items:
                epic_name_val = req_items[0].get("title", "")
            if not epic_name_val:
                epic_name_val = project_name

            # Save or update active Epic in epics table
            active_epic = await EpicRepository.get_active_by_project(str(project_id), session)
            # AI/document merges preserve a locked epic instead of failing the
            # whole otherwise-valid merge. Direct repository update/delete
            # operations still reject with ArtifactLockError.
            if active_epic and active_epic.get("is_locked"):
                epic_rec = active_epic
                logger.warning("[LOCK ENFORCEMENT] Preserving locked epic %s", epic_rec["id"])
            elif epic_name_val:
                epic_rec = await EpicRepository.save_or_update_epic(str(project_id), epic_name_val, session, version=version_num)
            else:
                epic_rec = active_epic or await EpicRepository.save_or_update_epic(str(project_id), "Untitled Epic", session, version=version_num)

            epic_id_uuid = uuid.UUID(epic_rec["id"])

            # Load ALL existing active requirements for this project (by requirement_code map)
            stmt_all_reqs = select(RequirementModel).where(
                RequirementModel.project_id == project_id,
                func.lower(RequirementModel.status) == "active"
            )
            res_all_reqs = await session.execute(stmt_all_reqs)
            all_db_reqs = res_all_reqs.scalars().all()

            db_reqs_by_code = {}
            db_reqs_by_id = {}
            for r in all_db_reqs:
                if r.requirement_code:
                    db_reqs_by_code[r.requirement_code] = r
                db_reqs_by_id[str(r.id)] = r

            # Also load archived requirements (we might need to reactivate)
            stmt_archived_reqs = select(RequirementModel).where(
                RequirementModel.project_id == project_id,
                func.lower(RequirementModel.status) == "archived"
            )
            res_archived_reqs = await session.execute(stmt_archived_reqs)
            for r in res_archived_reqs.scalars().all():
                if r.requirement_code and r.requirement_code not in db_reqs_by_code:
                    db_reqs_by_code[r.requirement_code] = r

            # Track which requirement IDs are touched to archive others
            touched_req_ids = set()

            for req_item in req_items:
                req_code = req_item.get("requirement_code", UNASSIGNED_REQUIREMENT_CODE)
                req_title = req_item.get("title", "Untitled Requirement")
                req_desc = req_item.get("description", "")
                stories = req_item.get("user_stories", [])

                # Find or create the RequirementModel for this requirement_code
                db_req = db_reqs_by_code.get(req_code)
                if db_req:
                    # LOCK ENFORCEMENT: Skip updating locked requirements (AI cannot modify locked requirements)
                    if db_req.is_locked:
                        logger.warning(
                            f"[LOCK ENFORCEMENT] Skipping update of locked requirement {req_code} "
                            f"(locked_by={db_req.locked_by or 'unknown'}). AI modification blocked."
                        )
                        touched_req_ids.add(str(db_req.id))
                        continue
                    # Update existing requirement
                    if db_req.status != "active":
                        db_req.status = "active"
                    db_req.title = req_title
                    db_req.description = req_desc
                    db_req.epic_id = epic_id_uuid
                    db_req.version = version_num
                    await session.flush()
                    created_req_id = db_req.id
                else:
                    # Create new requirement
                    new_req = RequirementModel(
                        project_id=project_id,
                        epic_id=epic_id_uuid,
                        requirement_code=req_code,
                        title=req_title,
                        description=req_desc,
                        version=version_num,
                        status="active"
                    )
                    session.add(new_req)
                    await session.flush()
                    created_req_id = new_req.id

                touched_req_ids.add(str(created_req_id))

                # ---- Process User Stories for this requirement ----
                if not stories:
                    continue

                # Load all existing stories for this specific requirement
                stmt_stories = select(UserStoryModel).where(UserStoryModel.requirement_id == created_req_id)
                res_stories = await session.execute(stmt_stories)
                db_stories = res_stories.scalars().all()

                stories_by_id = {s.id: s for s in db_stories}
                stories_by_code = {s.ticket_code: s for s in db_stories if s.ticket_code}
                stories_by_title = {s.story_title: s for s in db_stories if s.story_title}

                touched_story_ids = []
                semantic_recs = updates.get("semantic_recommendations") or []

                for story in stories:
                    ticket_code = story.get("ticket_code")
                    story_title = story.get("story_title", "Untitled Story")

                    # Check match
                    db_story = None
                    if ticket_code and ticket_code != "US-000":
                        db_story = stories_by_code.get(ticket_code)
                    if not db_story:
                        db_story = stories_by_title.get(story_title)

                    rec_action = None
                    if semantic_recs:
                        matched_rec = next((r for r in semantic_recs if r.get("target_requirement_id") == ticket_code), None)
                        if not matched_rec and db_story:
                            matched_rec = next((r for r in semantic_recs if r.get("target_requirement_id") == db_story.ticket_code), None)
                        if matched_rec:
                            rec_action = matched_rec.get("recommended_action")

                    if db_story:
                        title_changed = db_story.story_title != story_title
                        as_a_changed = db_story.as_a != story.get("as_a", "")
                        i_want_to_changed = db_story.i_want_to != story.get("i_want_to", "")
                        so_that_changed = db_story.so_that != story.get("so_that", "")
                        was_archived = db_story.status != "active"

                        # LOCK ENFORCEMENT: Skip modification of locked user stories
                        if db_story.is_locked:
                            logger.warning(
                                f"[LOCK ENFORCEMENT] Skipping update of locked user story "
                                f"{db_story.ticket_code} (locked_by={db_story.locked_by or 'unknown'}). "
                                f"AI modification blocked."
                            )
                            touched_story_ids.append(db_story.id)
                            continue

                        if rec_action == "UPDATE":
                            story_modified = True
                        elif rec_action == "NO_CHANGE":
                            story_modified = False
                        elif rec_action == "ARCHIVE":
                            db_story.status = "archived"
                            db_story.change_type = "archived"
                            db_story.version = db_story.version + 1
                            db_story.last_modified_by = "automated_agent"
                            db_story.project_id = project_id
                            await session.flush()
                            continue
                        else:
                            story_modified = title_changed or as_a_changed or i_want_to_changed or so_that_changed

                        if story_modified or was_archived:
                            db_story.story_title = story_title
                            db_story.as_a = story.get("as_a", "")
                            db_story.i_want_to = story.get("i_want_to", "")
                            db_story.so_that = story.get("so_that", "")
                            db_story.status = "active"
                            db_story.version = db_story.version + 1
                            db_story.change_type = "updated"
                        else:
                            db_story.change_type = "unchanged"
                            db_story.status = "active"

                        db_story.last_modified_by = "automated_agent"
                        db_story.project_id = project_id
                        await session.flush()
                    else:
                        if not ticket_code or ticket_code == "US-000" or ticket_code in stories_by_code:
                            ticket_code = await UserStoryRepository.generate_unique_ticket_code(project_id, session)

                        db_story = UserStoryModel(
                            project_id=project_id,
                            requirement_id=created_req_id,
                            ticket_code=ticket_code,
                            story_title=story_title,
                            as_a=story.get("as_a", ""),
                            i_want_to=story.get("i_want_to", ""),
                            so_that=story.get("so_that", ""),
                            status="active",
                            version=1,
                            last_modified_by="automated_agent",
                            change_type="created"
                        )
                        session.add(db_story)
                        await session.flush()

                    touched_story_ids.append(db_story.id)

                    # Sync acceptance criteria for this story
                    stmt_ac = select(AcceptanceCriteriaModel).where(AcceptanceCriteriaModel.user_story_id == db_story.id)
                    res_ac = await session.execute(stmt_ac)
                    db_ac_list = res_ac.scalars().all()

                    incoming_ac_texts = story.get("acceptance_criteria", [])
                    matched_db_ac = set()
                    matched_incoming_indices = set()

                    for i, text_val in enumerate(incoming_ac_texts):
                        for db_ac in db_ac_list:
                            if db_ac.id not in matched_db_ac and db_ac.status == "active" and db_ac.criteria_text == text_val:
                                if not db_ac.is_locked:
                                    db_ac.change_type = "unchanged"
                                matched_db_ac.add(db_ac.id)
                                matched_incoming_indices.add(i)
                                break

                    for i, text_val in enumerate(incoming_ac_texts):
                        if i in matched_incoming_indices:
                            continue
                        for db_ac in db_ac_list:
                            if db_ac.is_locked:
                                continue
                            if db_ac.id not in matched_db_ac and db_ac.status == "archived" and db_ac.criteria_text == text_val:
                                db_ac.status = "active"
                                db_ac.change_type = "updated"
                                db_ac.version = db_ac.version + 1
                                matched_db_ac.add(db_ac.id)
                                matched_incoming_indices.add(i)
                                break

                    # Locked criteria may be read/matched but are never selected
                    # as overwrite targets for a newly extracted/generated item.
                    unmatched_active_db_ac = [
                        ac for ac in db_ac_list
                        if ac.id not in matched_db_ac and ac.status == "active" and not ac.is_locked
                    ]
                    unmatched_incoming_indices_list = [i for i in range(len(incoming_ac_texts)) if i not in matched_incoming_indices]

                    for idx, i in enumerate(unmatched_incoming_indices_list):
                        text_val = incoming_ac_texts[i]
                        if idx < len(unmatched_active_db_ac):
                            db_ac = unmatched_active_db_ac[idx]
                            db_ac.criteria_text = text_val
                            db_ac.status = "active"
                            db_ac.version = db_ac.version + 1
                            db_ac.change_type = "updated"
                            db_ac.last_modified_by = "automated_agent"
                            matched_db_ac.add(db_ac.id)
                        else:
                            new_ac = AcceptanceCriteriaModel(
                                user_story_id=db_story.id,
                                criteria_text=text_val,
                                status="active",
                                version=1,
                                last_modified_by="automated_agent",
                                change_type="created"
                            )
                            session.add(new_ac)

                    for db_ac in db_ac_list:
                        if db_ac.id not in matched_db_ac and db_ac.status == "active":
                            if db_ac.is_locked:
                                logger.warning(
                                    "[LOCK ENFORCEMENT] Preserving locked acceptance criteria %s",
                                    db_ac.id,
                                )
                                continue
                            db_ac.status = "archived"
                            db_ac.change_type = "archived"
                            db_ac.version = db_ac.version + 1

                    await session.flush()

                # Archive any user stories in this requirement that are NOT in the current payload
                for s_id, s_model in stories_by_id.items():
                    if s_id not in touched_story_ids and s_model.status == "active":
                        # LOCK ENFORCEMENT: Never archive a locked user story
                        if s_model.is_locked:
                            logger.warning(
                                f"[LOCK ENFORCEMENT] Skipping archive of locked user story "
                                f"{s_model.ticket_code} (locked_by={s_model.locked_by or 'unknown'}). "
                                f"AI removal blocked."
                            )
                            continue
                        s_model.status = "archived"
                        s_model.change_type = "archived"
                        s_model.version = s_model.version + 1

                        stmt_story_ac = select(AcceptanceCriteriaModel).where(
                            AcceptanceCriteriaModel.user_story_id == s_id,
                            AcceptanceCriteriaModel.status == "active"
                        )
                        res_story_ac = await session.execute(stmt_story_ac)
                        story_ac_list = res_story_ac.scalars().all()
                        for db_ac in story_ac_list:
                            if db_ac.is_locked:
                                logger.warning(
                                    "[LOCK ENFORCEMENT] Preserving locked acceptance criteria %s",
                                    db_ac.id,
                                )
                                continue
                            db_ac.status = "archived"
                            db_ac.change_type = "archived"
                            db_ac.version = db_ac.version + 1

                await session.flush()

            # Archive any active requirements NOT in the incoming payload (requirement removal)
            for r_code, r_model in db_reqs_by_code.items():
                r_id_str = str(r_model.id)
                if r_id_str not in touched_req_ids and r_model.status == "active":
                    # LOCK ENFORCEMENT: Cannot archive/delete a locked requirement
                    if r_model.is_locked:
                        logger.warning(
                            f"[LOCK ENFORCEMENT] Skipping archive of locked requirement {r_code} "
                            f"(locked_by={r_model.locked_by or 'unknown'}). AI removal blocked."
                        )
                        continue
                    r_model.status = "archived"
                    # Also archive all its user stories
                    stmt_orphan_stories = select(UserStoryModel).where(UserStoryModel.requirement_id == r_model.id)
                    res_orphan_stories = await session.execute(stmt_orphan_stories)
                    for orphan_story in res_orphan_stories.scalars().all():
                        if orphan_story.status == "active":
                            # LOCK ENFORCEMENT: Never archive a locked user story
                            if orphan_story.is_locked:
                                logger.warning(
                                    f"[LOCK ENFORCEMENT] Skipping archive of locked user story "
                                    f"{orphan_story.ticket_code} (locked_by={orphan_story.locked_by or 'unknown'}). "
                                    f"AI removal blocked."
                                )
                                continue
                            orphan_story.status = "archived"
                            orphan_story.change_type = "archived"
                            orphan_story.version = orphan_story.version + 1
                    await session.flush()

        # 3. Propagation for Clarification Questions (associate with first active requirement)
        first_active_req_id = None
        for r_id_str in touched_req_ids:
            first_active_req_id = uuid.UUID(r_id_str)
            break
        if first_active_req_id is None:
            # Audit-only saves deliberately omit requirement payload so they
            # cannot rewrite the board. Resolve an existing requirement solely
            # as the persistence anchor for audit questions/results.
            first_active_req_id = await session.scalar(
                select(RequirementModel.id).where(
                    RequirementModel.project_id == project_id,
                    func.lower(RequirementModel.status) == "active",
                ).order_by(RequirementModel.created_at.asc()).limit(1)
            )

        if "clarification_questions" in updates and updates["clarification_questions"] is not None:
            if first_active_req_id:
                stmt_ar_ids = select(AuditResultModel.id).where(AuditResultModel.requirement_id == first_active_req_id)
                res_ar_ids = await session.execute(stmt_ar_ids)
                ar_ids = res_ar_ids.scalars().all()
                if ar_ids:
                    stmt_del_cq = delete(ClarificationQuestionModel).where(ClarificationQuestionModel.audit_result_id.in_(ar_ids))
                    await session.execute(stmt_del_cq)
            else:
                # Document-only projects have no visible requirement. Remove
                # questions through the project-scoped internal audit anchor.
                stmt_ar_ids = (
                    select(AuditResultModel.id)
                    .join(RequirementModel)
                    .where(RequirementModel.project_id == project_id)
                )
                res_ar_ids = await session.execute(stmt_ar_ids)
                ar_ids = res_ar_ids.scalars().all()
                if ar_ids:
                    await session.execute(
                        delete(ClarificationQuestionModel).where(
                            ClarificationQuestionModel.audit_result_id.in_(ar_ids)
                        )
                    )

            for cq in updates["clarification_questions"]:
                await ClarificationQuestionRepository.create(str(project_id), {
                    "checklist_category": cq.get("checklist_category", "General"),
                    "target_user_story_id": cq.get("target_user_story_id"),
                    "question_text": cq.get("question_text", ""),
                    "user_answer": cq.get("user_answer"),
                    "is_resolved": cq.get("is_resolved", False),
                    "source_references": cq.get("source_references", []),
                }, session, requirement_id=first_active_req_id)

        # 4. Propagation for PRD & Diagrams
        if ("generated_prd" in updates and updates["generated_prd"] is not None) or ("generated_diagrams" in updates and updates["generated_diagrams"] is not None):
            prd_list = await PRDDocumentRepository.get_by_project(str(project_id), session, version=version_num)
            # Carry-over source: the row for THIS version when one exists, else
            # the project's NEWEST stored row (its version may lag the counter —
            # e.g. a manual part edit bumps the version before any document row
            # exists for it). Carrying over keeps every new version row
            # complete: a document-only or diagram-only save must never blank
            # the other field just because the version advanced.
            carry_over = prd_list[0] if prd_list else (
                await PRDDocumentRepository.get_latest_for_project(str(project_id), session)
            )
            prd_markdown = updates.get("generated_prd") or ((carry_over or {}).get("prd_markdown") or "")
            mermaid_diagram = updates.get("generated_diagrams") or ((carry_over or {}).get("mermaid_diagram") or "")

            if not prd_list:
                await PRDDocumentRepository.create(str(project_id), {
                    "version": version_num,
                    "prd_markdown": prd_markdown,
                    "mermaid_diagram": mermaid_diagram
                }, session)
            else:
                await PRDDocumentRepository.update(prd_list[0]["id"], str(project_id), {
                    "version": version_num,
                    "prd_markdown": prd_markdown,
                    "mermaid_diagram": mermaid_diagram
                }, session)

        # 5. Propagation for Audit Results
        if (
            "validation_status" in updates
            and updates["validation_status"] is not None
            and any(key in updates for key in (
                "passed_checks", "failed_checks", "audit_findings", "audit_source_references",
                "audit_verdict", "audit_project_context",
                "audit_checklist_id", "audit_checklist_version",
            ))
        ):
            passed = updates.get("passed_checks", [])
            failed = updates.get("failed_checks", [])
            findings = updates.get("audit_findings", [])
            source_references = updates.get("audit_source_references", [])
            verdict = updates.get("audit_verdict", "needs_clarification")
            project_context = updates.get("audit_project_context", {})
            checklist_id = updates.get("audit_checklist_id", "banking-core")
            checklist_version = updates.get("audit_checklist_version", "1.0.0")
            is_valid = updates["validation_status"] == "valid"

            ar_list = await AuditResultRepository.get_by_project(str(project_id), session, requirement_id=first_active_req_id)
            if not ar_list:
                current_audit = await AuditResultRepository.create(str(project_id), {
                    "is_valid": is_valid,
                    "audit_version_reviewed": version_num,
                    "passed_checks": passed,
                    "failed_checks": failed,
                    "findings": findings,
                    "source_references": source_references,
                    "verdict": verdict,
                    "project_context": project_context,
                    "checklist_id": checklist_id,
                    "checklist_version": checklist_version,
                }, session, requirement_id=first_active_req_id)
            else:
                current_audit = await AuditResultRepository.update(ar_list[0]["id"], str(project_id), {
                    "is_valid": is_valid,
                    "audit_version_reviewed": version_num,
                    "passed_checks": updates.get("passed_checks", ar_list[0]["passed_checks"]),
                    "failed_checks": updates.get("failed_checks", ar_list[0]["failed_checks"]),
                    "findings": findings,
                    "source_references": source_references,
                    "verdict": verdict,
                    "project_context": project_context,
                    "checklist_id": checklist_id,
                    "checklist_version": checklist_version,
                }, session)

            if updates.get("_record_audit_run") and current_audit:
                from app.audit_history_service import record_audit_run
                await record_audit_run(str(project_id), current_audit, session)

        await session.flush()
        await session.refresh(model)

        return await RequirementStateRepository._get_by_project_id_impl(project_id, session)
