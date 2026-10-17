"""Exercise upload -> Auditor auto-persistence -> reload in SQLite."""
import pytest
from sqlalchemy import select, func
from app.models import AuditRunModel, PendingActionModel
from tests.test_documents import db_session, client, seeded_project


@pytest.mark.asyncio
async def test_audit_auto_persistence_reload_and_failed_retry(client, db_session, seeded_project, monkeypatch):
    import app.agents as agents
    import app.semantic_service as semantic
    await client.get(f"/api/project/{seeded_project}")
    upload = await client.post(
        f"/api/project/{seeded_project}/documents/upload",
        files={"file": ("policy.md", b"# Policy\nApproval is required.", "text/markdown")},
    )
    assert upload.status_code == 201, upload.text
    document_id = upload.json()["id"]
    ref = {"document_id": document_id, "document_name": "policy.md",
           "section": "Policy", "excerpt": "Approval is required."}
    async def intent(*args, **kwargs):
        return {"intent": "GENERAL_CHAT", "confidence": 1}
    async def save(**kwargs):
        return {}
    async def inference(*args, **kwargs):
        assert "Approval is required." in kwargs["variables"]["structured_requirements"]
        assert "BANK-CORPORATE-001" in kwargs["variables"]["audit_checklist"]
        return {
            "is_valid": False, "findings": [{
                "rule_id": "BANK-CORPORATE-001",
                "finding_type": "missing", "category": "Approval",
                "description": "Approval owner is missing.", "source_references": [ref],
                "applicability": "applicable_required", "impact": "blocking",
                "evidence_status": "supported", "confidence": 0.9,
                "recommendation": {
                    "summary": "Name the approval owner.",
                    "proposed_requirement_text": "The designated approver shall approve the instruction.",
                    "proposed_acceptance_criteria": [],
                },
            }],
            "clarification_questions": [{
                "question_text": "Who approves?", "checklist_category": "Approval",
                "source_references": [ref],
            }],
        }
    monkeypatch.setattr(semantic, "detect_requirement_intent", intent)
    monkeypatch.setattr(agents.ConversationMessageRepository, "save_message", save)
    monkeypatch.setattr(agents, "invoke_llm_structured", inference)
    payload = {"project_id": seeded_project, "raw_input": "", "target_agent": "auditor"}
    response = await client.post("/api/process-requirements", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["pending_merge"] is False
    assert response.json()["pending_action_id"] is None
    assert response.json()["audit_auto_persisted"] is True
    assert response.json()["audit_result"]["clarification_questions"][0]["id"]
    saved = (await client.get(f"/api/project/{seeded_project}")).json()
    assert saved["audit_verdict"] == "fail"
    assert saved["audit_checklist_id"] == "banking-core"
    assert saved["audit_checklist_version"] == "1.0.0"
    assert saved["audit_findings"][0]["rule_id"] == "BANK-CORPORATE-001"
    assert saved["audit_findings"][0]["recommendation"]["summary"] == "Name the approval owner."
    assert saved["audit_findings"][0]["source_references"] == [ref]
    assert saved["clarification_questions"][0]["source_references"] == [ref]
    assert await db_session.scalar(select(func.count()).select_from(AuditRunModel)) == 1
    count_before = await db_session.scalar(select(func.count()).select_from(PendingActionModel))
    async def broken(*args, **kwargs):
        raise RuntimeError("private model exception")
    monkeypatch.setattr(agents, "invoke_llm_structured", broken)
    failed = await client.post("/api/process-requirements", json=payload)
    assert failed.status_code == 200
    assert "private model exception" not in failed.text
    assert failed.json()["pending_merge"] is False
    assert failed.json()["pending_action_id"] is None
    assert set(failed.json()["audit_result"]["failed_checks"]) == {"AUDIT_INCOMPLETE", "AUDIT_PARSE_ERROR"}
    assert await db_session.scalar(select(func.count()).select_from(PendingActionModel)) == count_before
    after = (await client.get(f"/api/project/{seeded_project}")).json()
    assert after["audit_findings"] == saved["audit_findings"]
    async def passing(*args, **kwargs):
        return {"is_valid": True, "findings": [], "clarification_questions": [],
                "passed_checks": ["Approval"]}
    monkeypatch.setattr(agents, "invoke_llm_structured", passing)
    rerun = await client.post("/api/process-requirements", json=payload)
    assert rerun.status_code == 200, rerun.text
    assert rerun.json()["pending_merge"] is False
    assert rerun.json()["audit_auto_persisted"] is True
    final = (await client.get(f"/api/project/{seeded_project}")).json()
    assert final["audit_verdict"] == "needs_clarification"
    assert final["failed_checks"] == []
    assert final["audit_findings"] == []
    assert len(final["clarification_questions"]) == 1
    assert final["clarification_questions"][0]["question_text"] == "Who approves?"
    assert await db_session.scalar(select(func.count()).select_from(AuditRunModel)) == 2
