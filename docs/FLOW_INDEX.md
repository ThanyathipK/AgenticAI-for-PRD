# Application Flow Index

This document is the source of truth for tracing the application's major business flows across the frontend, API boundary, backend orchestration, domain services, repositories, and persistence layer.

It documents the implementation as it exists today. Updating a flow comment must not imply a behavior change. Application behavior belongs in code and tests; this index explains how that behavior is connected.

## Comment standard

Use numbered flow comments on major boundaries only:

- UI entry points and user actions
- Frontend state or orchestration handlers
- API-client methods
- Backend routes
- Workflow and application services
- Important domain branches
- Repository or persistence boundaries
- Background jobs and emitted events

Small formatters, projections, mappers, and other local helpers should normally use a concise docstring or ordinary comment instead of receiving their own flow number.

### Backend template

```python
# <FLOW> <STEP> — <short responsibility>.
# Trigger: <caller, route, user action, or background event>.
# Input: <important arguments or state>.
# Calls: <next functions, services, or repositories>.
# Branches: <condition → destination>.
# Writes: <state, database, or external effects; use NONE when read-only>.
# Failure: <guard, fallback, or propagated error>.
# Next: <FLOW STEP or END>.
async def function_name(...):
```

### Frontend template

```typescript
// <FLOW> <STEP> — <short responsibility>.
// Trigger: <component event, effect, or state transition>.
// Input: <important arguments or state>.
// Calls: <hook, API client, or next handler>.
// Branches: <condition → destination>.
// Writes: <local/application state or NONE>.
// Failure: <guard, error display, or rollback>.
// Next: <FLOW STEP or END>.
function functionName(...) {
```

Not every field is required. Include only fields that make the path easier to understand.

## Numbering convention for new flows

New flows should use the following layer ranges consistently:

| Range | Layer |
| --- | --- |
| `1.x` | UI/component entry |
| `2.x` | Frontend hook or state handler |
| `3.x` | Frontend API client |
| `4.x` | Backend HTTP/SSE route |
| `5.x` | Application or workflow orchestration |
| `6.x` | Domain logic and transformations |
| `7.x` | Repository and database persistence |
| `8.x` | Events, jobs, and downstream effects |

Substeps use another decimal segment, such as `5.2.1`. A substep belongs to its parent operation and should not be used merely to number individual lines.

Existing flow numbers are retained below even when they predate this convention. Renumbering them should be a separate documentation task so cross-file references are not broken incrementally.

## Flow-comment vocabulary

Use these terms consistently:

- **Trigger**: the event or caller that starts this step.
- **Input**: material state consumed by the step.
- **Calls**: the next named boundary crossed by the step.
- **Branches**: decisions that change the destination or result.
- **Writes**: durable or in-memory side effects.
- **Failure**: whether the step fails loudly, fails softly, fails open, or fails closed.
- **Next**: the next numbered step or `END`.
- **Read-only**: performs no durable mutation.
- **Stage**: stores a proposed mutation for later confirmation.
- **Commit**: applies an approved mutation to authoritative state.

Arrows represent movement of control or data:

```text
caller → callee → result
condition → destination
```

## End-to-end application path

```text
Dashboard
→ useProjectState
→ focused frontend hook
→ typed API client
→ authenticated backend route
→ application/workflow service
→ domain service or agent node
→ repository/database
→ EventManager/SSE
→ useProjectSync
→ refreshed UI
```

The primary AI mutation path has an additional human approval boundary:

```text
ChatPanel
→ useChat
→ process-requirements route
→ router/matcher/agent
→ pending action (stage)
→ ConfirmationPanel
→ confirm-action route
→ RequirementStateRepository (commit)
→ SSE refresh
```

## Current flow registry

### AUTH — Authentication and authorization

Purpose: login, session restoration, registration, logout, password/account operations, JWT validation, and project ownership enforcement.

Primary path:

```text
AuthPanel/AuthModal
→ useAuth
→ api.auth*
→ routes/auth.py
→ app/auth.py
→ UserRepository
→ users table
```

Important locations:

- `src/components/AuthPanel.tsx`
- `src/components/AuthModal.tsx`
- `src/hooks/useAuth.ts`
- `src/api/client.ts`
- `backend/app/routes/auth.py`
- `backend/app/auth.py`
- `backend/app/repositories/user.py`

Current numbered groups:

- `AUTH 1.x`: login and authenticated UI transition
- `AUTH 2.x`: stored-session restoration
- `AUTH 3.x`: public authentication-capability/configuration probe
- `AUTH 4.x`: logout
- `AUTH 5.x`: password change and account operations
- `AUTH 6.x`: registration
- `AUTH 7.x`: JWT authentication and project authorization dependencies
- `AUTH 8.x`: permanent account deletion

Authorization path:

```text
AUTH 7.1 standard Bearer authentication
   or AUTH 7.2 flexible Bearer/query-token authentication
→ AUTH 7.3 safe AuthenticatedUser identity contract
→ protected route directly
   or AUTH 7.4 project ownership validation
      → AUTH 7.5 REST ownership dependency
      → AUTH 7.6 SSE ownership dependency
```

Account-deletion path:

```text
AUTH 8.1 DeleteAccountModal
→ AUTH 8.2 useAuth.deleteAccount
→ AUTH 8.3 api.authDeleteAccount
→ AUTH 8.4 DELETE /api/auth/account
   ├→ AUTH 8.4.1 confirmation guard
   ├→ AUTH 8.4.2 password verification
   └→ AUTH 8.4.3 protected-system-account guard
→ AUTH 8.5 UserRepository.delete_account
→ AUTH 8.6 commit and response
→ AUTH 8.7 clear browser session
```

`AUTH 1.7.2` is the successful-credential handoff between `authenticate_user`
(`AUTH 1.7.1`) and JWT creation (`AUTH 1.7.3`). It exists explicitly so the
login sequence does not contain an unexplained numbering gap.

### PROJECT — Project lifecycle and state loading

Purpose: list, search, create, open, rename, delete, pin, flag, and update project workflow status.

Primary path:

```text
Dashboard/sidebar
→ useProjects or useProjectSync
→ api project method
→ routes/projects.py
→ ProjectRepository or RequirementStateRepository
→ project-backed tables
```

Important locations:

- `src/hooks/useProjectState.ts`
- `src/hooks/useProjects.ts`
- `src/hooks/useProjectSync.ts`
- `src/api/client.ts`
- `backend/app/routes/projects.py`
- `backend/app/repositories/project.py`
- `backend/app/repositories/requirement_state.py`

Current numbered groups:

- `PROJECT 1.x`: project-list load and selection
- `PROJECT 2.x`: open project and hydrate full workspace state
- `PROJECT 3.x`: project search
- `PROJECT 4.x`: create project
- `PROJECT 5.x`: rename/update project
- `PROJECT 6.x`: delete project
- `PROJECT 7.x`: pin, flag, and workflow-status metadata
- `PROJECT 8.x`: direct requirement-state write path
- `PROJECT 9.x`: conversation timeline reads

### CHAT — Conversation entry, response generation, and memory

Purpose: accept chat input, select conversational or agent processing, recall project memory, generate a response, and persist the timeline.

Primary path:

```text
ChatPanel
→ useChat.handleSendMessage
→ api.processRequirements
→ routes/requirements.py
→ detect_requirement_intent
→ general-chat branch or LangGraph workflow
→ ConversationMessageRepository
```

Supporting paths:

```text
product general-chat branch
→ project-knowledge retrieval
→ generate_general_chat_response
→ LLM transport

standalone /api/chat branch
→ semantic-memory recall
→ prompt assembly
→ LLM transport
→ semantic-memory write
```

Current numbered groups:

- `CHAT 1.x`: composer, frontend handler, shared API call, and UI response
- `CHAT 2.x`: `/api/process-requirements` direct-conversation branch
- `CHAT 3.x`: semantic-memory recall and prompt rendering
- `CHAT 4.x`: semantic-memory fact extraction and persistence
- `CHAT 5.x`: standalone `/api/chat` endpoint used by API clients/tests
- `CHAT 6.x`: LM Studio inference transport and readiness
- `CHAT 7.x`: conversation transcript repository

Product endpoint sequence:

```text
CHAT 2.1 shared processing route
→ CHAT 2.2 intent branch
→ CHAT 2.3 direct conversation or LangGraph agent workflow
→ CHAT 2.4 direct-conversation response construction, when applicable
→ CHAT 2.5 fail-open semantic-memory write
→ CHAT 1.4 frontend response handling
```

The three conversation implementations are intentionally distinct:

```text
CHAT 2.3     Product composer's direct GENERAL_CHAT branch
ROUTING 2.2  Router-node chat fallback inside LangGraph
CHAT 5.1     Standalone memory-aware /api/chat endpoint (not called by the SPA)
```

Standalone endpoint sequence:

```text
CHAT 5.1 route/guards
→ CHAT 5.2 semantic-memory recall
→ CHAT 5.3 prompt assembly + newest user-turn persistence
→ CHAT 5.4 LM Studio transport
→ CHAT 5.5 assistant-turn persistence
→ CHAT 5.6 semantic-memory write
→ CHAT 5.7 SSE publication
```

Readiness sequence:

```text
CHAT 6.2 gateway probe
→ CHAT 6.2.1 health route
→ CHAT 6.3.1 API client
→ CHAT 6.3.2 frontend polling/state
```

Important locations:

- `src/components/ChatPanel.tsx`
- `src/hooks/useChat.ts`
- `src/hooks/chat/`
- `backend/app/routes/requirements.py`
- `backend/app/routes/chat.py`
- `backend/app/semantic_service.py`
- `backend/app/semantic_memory.py`
- `backend/app/project_knowledge_service.py`
- `backend/app/repositories/conversation.py`
- `backend/app/llm_client.py`

### ROUTING — Workflow classification and graph dispatch

Purpose: classify messages as `CHAT`, `QUESTION`, `COMMAND`, or `REQUIREMENT`, then dispatch to the correct single-pass graph node.

Graph topology:

```text
START
→ route_on_demand
   ├→ workflow_router_node
   ├→ requirement_matcher_node
   ├→ auditor_node
   └→ architect_node

workflow_router_node
→ route_from_router
   ├→ CHAT/QUESTION → END
   ├→ COMMAND → auditor_node | architect_node | END
   └→ REQUIREMENT → requirement_matcher_node

requirement_matcher_node
→ route_from_matcher
   ├→ resolved requirement → gatherer_node
   ├→ confident delete → delete_requirement_node
   └→ clarification required → END
```

Current numbered groups:

- `ROUTING 1.x`: graph topology, entry selection, and compilation
- `ROUTING 2.x`: router node and its CHAT/QUESTION/COMMAND/REQUIREMENT branches
- `ROUTING 3.x`: workflow-classification service and deterministic fallback
- `ROUTING 4.x`: post-router and post-matcher conditional edges
- `ROUTING 5.x`: authenticated debug endpoint and response schema

Important locations:

- `backend/app/agents.py`
- `backend/app/workflow_orchestration/graph.py`
- `backend/app/workflow_orchestration/routing.py`
- `backend/app/semantic_service.py`
- `backend/app/prompts/router.md`

### INTENT — Requirement-intent detection

Purpose: classify requirement input as create, update, delete, clarification, or general chat.

Primary path:

```text
process-requirements pipeline or matcher fallback
→ detect_requirement_intent
→ intent prompt + story context
→ invoke_llm_structured
→ normalized intent/confidence/reasoning
→ target-agent selection
```

Current numbered groups:

- `INTENT 1.x`: integration with the shared processing pipeline
- `INTENT 2.x`: canonical detector, structured LLM call, and deterministic fallback
- `INTENT 3.x`: defensive detection for direct LangGraph node invocation
- `INTENT 4.x`: authenticated API/debug detector integration
- `INTENT 5.x`: shared response schema

Pipeline sequence:

```text
INTENT 1.0 pipeline integration
→ INTENT 1.1 establish detection phase/context source
→ INTENT 1.2 local service import
→ INTENT 1.3 invoke canonical detector
→ CHAT 2.2 direct-conversation decision
   or INTENT 1.4 map requirement intent to target agent
```

Canonical detector sequence:

```text
INTENT 2.0 detector
→ INTENT 2.0.1 accepted vocabulary
→ INTENT 2.1 blank-input guard
→ INTENT 2.1.1 requirement-brief guard
→ INTENT 2.2 story-context rendering
→ INTENT 2.3 prompt assembly
→ INTENT 2.4 schema/format instructions
→ INTENT 2.5 vocabulary validation
→ INTENT 2.6 structured LLM call
→ INTENT 2.7 normalized result
   or INTENT 2.8/2.8.1 deterministic failure fallback
```

Direct API sequence:

```text
INTENT 4.1 detector endpoint
→ INTENT 4.1.0 input-budget guard
→ optional INTENT 4.1.2 ownership/context branch
   → INTENT 4.1.1 shared context loader
→ INTENT 4.1.3 canonical detector invocation
→ INTENT 5.0 schema boundary
→ INTENT 5.1 response validation

Direct matcher alternative:
requirement-matcher endpoint
→ supplied intent is reused
   or INTENT 4.2 invokes INTENT 2.0 when intent is missing
```

Important locations:

- `backend/app/semantic_service.py`
- `backend/app/prompts/intent.md`
- `backend/app/schemas.py`
- `backend/app/routes/requirements.py`

### MATCHING — Existing requirement/story target resolution

Purpose: determine which existing requirement, story, or acceptance criterion a modification refers to and stop before extraction or archive staging when the target is ambiguous.

Numbered groups:

- `MATCHING 1.x` — graph matcher input loading, deterministic resolution, semantic fallback, and clarification gate
- `MATCHING 2.x` — canonical semantic matcher prompt, structured call, and target validation
- `MATCHING 3.x` — matched-target consumers in prompt guidance, Gatherer reconciliation, and archive staging
- `MATCHING 4.x` — graph outcome dispatch and API-pipeline persistence/staging handoff
- `MATCHING 5.x` — authenticated direct API and response-schema boundary

Primary path:

```text
requirement_matcher_node
→ MATCHING 1.1 load backlog and normalized intent
→ MATCHING 1.2 build REQ/US/scope indexes
→ MATCHING 1.3 deterministic explicit-target resolution
   └→ unresolved text → MATCHING 1.4 → MATCHING 2.x semantic matcher
→ MATCHING 1.5 ambiguity gate or MATCHING 1.6 confident result
→ MATCHING 4.0 dispatch
   ├→ unresolved/invalid → END
   ├→ DELETE → MATCHING 3.3 archive-preview consumer
   └→ other requirement action → MATCHING 3.1/3.2 Gatherer consumers
→ MATCHING 4.1 API pipeline interprets the final graph result
   ├→ MATCHING 4.1.1 unresolved → persist clarification state; no pending action
   ├→ MATCHING 4.1.2 resolved DELETE with material change → stage ARCHIVE action
   └→ stale, locked, or unchanged → no matcher-driven pending action
→ Save/Cancel continues in the CONFIRMATION flow
```

Direct API path:

```text
POST /api/requirement-matcher (MATCHING 5.1)
→ request budget and project access checks
→ supplied intent or INTENT 4.2 detection
→ MATCHING 5.1.2 calls MATCHING 2.0
→ MATCHING 5.2 response contract
```

Important locations:

- `backend/app/agents.py`
- `backend/app/semantic_service.py`
- `backend/app/prompts/matcher.md`
- `backend/app/schemas.py`

### GATHERING — Requirement extraction and merge staging

Purpose: turn raw product-owner input into structured requirements, merge it with the current board, and stage material changes for approval.

Numbered groups:

- `GATHERING 1.x` — state loading, structured-draft intake, and safe early exits
- `GATHERING 2.x` — matcher consumption, semantic recommendations, confidence gate, and prompt context
- `GATHERING 3.x` — structured extraction, normalization, evidence filtering, and requirement-code repair
- `GATHERING 4.x` — one-pass merge, requirement regrouping, revision calculation, and preview return
- `GATHERING 5.x` — semantic-change LLM service and deterministic result validation
- `GATHERING 6.x` — shared story identity, field/criteria merge, protection, and projection helpers
- `GATHERING 7.x` — canonical `REQ-###` formatting, normalization, and sequencing
- `GATHERING 8.x` — backend pending-action staging and frontend confirmation presentation

Primary path:

```text
gatherer_node
→ GATHERING 1.1/1.2 load RequirementState and snapshot board/draft
→ GATHERING 1.3 intake branches
   ├→ 1.4 empty request → transcript only; graph END
   ├→ 1.5/1.5.1 draft-only projection → no LLM; graph END → 8.0 comparison
   └→ 1.6 GENERAL_CHAT → transcript only; graph END
→ GATHERING 2.0 pre-extraction intelligence
   ├→ 2.1 locked matched update → lock-blocked response; END
   ├→ 2.2 → GATHERING 5.x semantic detection/validation
   ├→ 2.3 merge matcher recommendation
   └→ 2.4 low confidence → request-local clarification + persistent transcript; graph END
→ GATHERING 2.5/2.6 backlog context and intent/target guidance
→ GATHERING 3.x structured extraction and normalized draft
→ GATHERING 4.1 → GATHERING 6.x one-pass story merge
→ GATHERING 4.2 regroup stories under stable requirement identities
→ GATHERING 4.3 update request-local preview and requirements_revision
→ GATHERING 4.4/4.5 transcript summary and preview return
→ GATHERING 8.0 material current preview → pending MERGE action
→ GATHERING 8.1 frontend confirmation presentation
→ Save/Cancel continues in the CONFIRMATION flow
```

Persistence rule: the node may write assistant transcript messages, but it does not commit final requirement/story rows. The API pipeline stores material proposed state in `pending_actions`; Save in the `CONFIRMATION` flow is the final writer. Gathering increments `requirements_revision` only for a material story merge and does not advance PRD `version_number` or SemVer.

Current semantic-clarification behavior: `GATHERING 2.4` returns clarification questions in the request result and persists the assistant transcript message. Because that branch changes no requirement source field, `GATHERING 8.0` creates no pending action and does not separately persist those questions into `requirement_states`.

Important locations:

- `backend/app/agents.py`
- `backend/app/semantic_service.py`
- `backend/app/merge_service.py`
- `backend/app/requirement_codes.py`
- `backend/app/prompts/gatherer.md`
- `backend/app/routes/requirements.py`

### AUDIT — Requirement validation and compliance review

Purpose: audit the active requirement board, record findings, and produce clarification questions when required.

Numbered groups:

- `AUDIT 1.x` — detached state/draft loading, full active scope, evidence sanitation, and empty-scope reuse
- `AUDIT 2.x` — prompt budgeting, bounded model passes, deterministic validation/recovery, result projection, and immediate persistence
- `AUDIT 3.x` — authenticated, lock-gated clarification-answer API
- `AUDIT 4.x` — clarification-question repository persistence and target resolution
- `AUDIT 5.x` — frontend trigger, response mapping, and verdict/question presentation

Primary path:

```text
Validate Requirements action
→ useChat.handleValidateRequirements
→ processRequirements(target_agent=auditor)
→ AUDIT 1.0 auditor_node
→ AUDIT 1.1/1.2 detached state, full active story scope, sanitized evidence
   └→ AUDIT 1.3 empty scope → reuse saved audit; no model call
→ AUDIT 2.1/2.2 prompt assembly and budgeted evidence contexts
→ AUDIT 2.3/2.4 bounded full-checklist pass
   ├→ primary failure → smaller checklist recovery passes
   ├→ no usable pass → deterministic structural fallback; operational error
   └→ partial recovery → incomplete read-only result
→ AUDIT 2.5 deterministic pass merge, citation/risk policy, and quality findings
→ AUDIT 2.6 preserve/deduplicate questions and project audit-only state
→ AUDIT 2.7 persist auditor transcript
→ AUDIT 2.8 explicit auditor request: persist successful current audit/questions/history immediately
   └→ stale or operational error → preserve last saved audit; no persistence
→ AUDIT 5.2–5.4 render verdict, findings, and clarification form
→ AUDIT 3.x answer a persisted question by stable row ID
```

Persistence rule: a successful, current request that explicitly uses `target_agent="auditor"` is not a proposed requirement edit and creates no pending MERGE. The API pipeline immediately persists audit metadata, `audit_results`, clarification questions, and audit-run history. A router-classified audit command can run the node but does not enter that explicit-target persistence branch. Operationally incomplete or stale results remain read-only and preserve the last good saved audit. On document-only projects, the persistence repositories may create one archived internal requirement anchor to satisfy audit foreign keys; it is excluded from the visible active board.

Important locations:

- `src/hooks/useChat.ts`
- `backend/app/agents.py`
- `backend/app/audit_checklist.py`
- `backend/app/audit_history_service.py`
- `backend/app/prompts/auditor.md`
- `backend/app/repositories/audit_result.py`
- `backend/app/repositories/clarification_question.py`

### ARCHITECT — PRD and architecture-diagram generation

Purpose: compile saved requirements, explicit narrative, and supported audit evidence into a reviewable PRD and Mermaid architecture-flow candidate while preserving locked or human-owned sections.

Numbered groups:

- `ARCHITECT 1.x` — frontend guards, version digest, background-job trigger, preview loading, and cancellation/error handling
- `ARCHITECT 2.x` — canonical-state loading, lock/scope preflight, draft policy, reuse, and empty-board guards
- `ARCHITECT 3.x` — deterministic official-template dataset assembly and fill
- `ARCHITECT 4.x` — optional Mermaid generation, validation, and fail-open behavior
- `ARCHITECT 5.x` — SemVer stamping and ownership-preserving section merge in preview or direct-write mode
- `ARCHITECT 6.x` — immutable version ledger for non-preview/direct-write callers
- `ARCHITECT 7.x` — user-facing candidate message (preview-oriented wording, also returned to internal callers)
- `ARCHITECT 8.x` — candidate return and durable `GENERATE_PRD` preview staging
- `ARCHITECT 9.x` — Save-time stale checks and authoritative artifact/version commit

Primary path:

```text
Generate PRD action
→ ARCHITECT 1.1 client eligibility guard
→ ARCHITECT 1.2/1.4 explicit background request with target_agent=architect
→ ARCHITECT 2.0 architect_node in stage_artifacts_only preview mode
→ ARCHITECT 2.1–2.7 canonical state, lock/scope, narrative, reuse, and empty-board guards
→ ARCHITECT 3.x deterministic official-template fill
→ ARCHITECT 4.x optional validated Mermaid generation
→ ARCHITECT 5.0.1 proposed SemVer stamp
→ ARCHITECT 5.0.2 in-memory merge preserving locked/human-owned stored sections
→ ARCHITECT 8.0 return candidate
→ ARCHITECT 8.1 replace/store durable GENERATE_PRD pending action
→ ARCHITECT 1.5 load preview panel; saved artifacts remain unchanged
→ Save PRD → ARCHITECT 9.0
   ├→ stale requirement revision or PRD version → 409; preview remains unapplied
   └→ current preview → sync sections → append immutable version → save document/diagram → delete action
```

Alternate direct-write mode: callers that invoke `architect_node` without `stage_artifacts_only` use `ARCHITECT 5.1/5.2` to synchronize sections and `ARCHITECT 6.0` to append the version ledger during the node run. The explicit frontend Generate PRD path does not use those write branches before approval.

Persistence rule: explicit Generate PRD writes only a pending preview. Canonical PRD sections, `generated_prd`, `generated_diagrams`, and the immutable version ledger change only after `ARCHITECT 9.0` Save. Discarding the action is a governed-artifact no-op.

Important locations:

- `src/hooks/useChat.ts`
- `backend/app/agents.py`
- `backend/app/prd_filler.py`
- `backend/app/prd_section_service.py`
- `backend/app/services/diagram_service.py`
- `backend/app/prompts/architect_diagram.md`
- `backend/app/routes/requirements.py`
- `backend/app/routes/lock.py`

### CONFIRM — Human approval and authoritative commit

Purpose: display a persisted pending-action draft, calculate its impact, apply approved
project changes through the correct commit branch, or discard the draft.

Numbering structure:

- `CONFIRM 1.x` — confirmation UIs, handlers, and pending-action refresh
- `CONFIRM 2.x` — frontend API boundary
- `CONFIRM 3.x` — backend list, impact, Save, and Discard routes
- `CONFIRM 4.x` — read-only impact calculation
- `CONFIRM 5.x` — pending-action repository lifecycle

Shared Save gate:

```text
ConfirmationPanel or PRDGenerationPreview
→ api.confirmAction
→ routes/lock.py::confirm_action
→ validate IDs
→ row-lock the project-scoped WAITING_CONFIRMATION action
→ delete + 410 for expiry; reject empty content with 400
→ recheck parent locks for ARCHIVE
→ select commit branch by action type
```

Non-GENERATE_PRD branch:

```text
MERGE / ARCHIVE / INSERT_CHUNKED_REQUIREMENTS / legacy UPDATE
→ reject a stale requirements revision when the draft carries a base revision
→ RequirementStateRepository.save_or_update
→ for ordinary requirement merges: invalidate changed dependents and delete stale PRD previews
→ for a legacy COMPLETED proposal: synchronize PRD sections without a ledger entry
→ mark an approved project revised when governed content changed
→ update document-extraction bookkeeping when applicable
→ delete consumed action
→ publish merge_confirmed
```

Generated-PRD branch:

```text
GENERATE_PRD
→ reject stale requirements or base PRD version
→ synchronize PRD sections
→ append immutable PRD version
→ save canonical PRD / diagram / narrative
→ delete consumed action
→ publish prd_confirmed
```

Other branches:

```text
impact preview → delete + 410 if expired; otherwise read-only impact calculation
cancel → optionally reset document extraction → delete pending action → action_cancelled
```

Persistence rule: the pending-action row and its proposal are already stored before
this flow appears. Save makes the proposal authoritative. Discard never applies it,
although discarding a document-extraction draft resets the source document status.
Expiry cleanup commits its deletion before returning 410. The empty-proposal branches
attempt deletion, but their 400 exception causes `get_db` to roll it back, so those rows
remain; this index documents that behavior and does not change it.

Important locations:

- `src/components/ConfirmationPanel.tsx`
- `src/components/PRDGenerationPreview.tsx`
- `src/api/client.ts`
- `backend/app/routes/lock.py`
- `backend/app/impact_service.py`
- `backend/app/repositories/pending_action.py`
- `backend/app/repositories/requirement_state.py`

Canonical name: use `CONFIRM` in identifiers. “Confirmation flow” may still be used as ordinary prose.

### CLARIFY — Clarification question and answer cycle

Purpose: collect stakeholder answers to persisted Matcher, Gatherer, or Auditor
questions, retain them as audit evidence, and prepare reviewable requirement changes
without treating the answers themselves as approved edits.

Numbering structure:

- `CLARIFY 1.x` — question rendering, local answers, submission, and UI reconciliation
- `CLARIFY 2.x` — frontend API boundary
- `CLARIFY 3.x` — authenticated answer route, evidence save, and draft staging
- `CLARIFY 4.x` — question projection, repository load, and target serialization
- `CLARIFY 5.x` — bounded Gatherer classification/proposal helper

Incoming question sources:

```text
AUDIT persisted clarification question ─┐
MATCHING persisted unresolved target ───┼→ project-state question projection → form
other question persisted with state ────┘

GATHERING request-local semantic clarification → response/transcript only
                                              → not a durable form row by itself
```

The combined answer endpoint operates only on persisted question rows. A stable database
question ID is therefore the normal contract; `q-<index>` exists for legacy/id-less
payload compatibility and is not the preferred identity.

Primary path:

```text
ValidationCard unresolved question
→ stable question-id answer in useChat local state
→ handleSubmitClarifications
→ api.submitClarifications
→ post_clarification_submit
→ validate project id and ownership
→ load persisted questions
→ resolve submitted questions and collapse exact duplicates
→ group answers by target and call Gatherer in batches of at most four
→ save answers as clarification evidence
→ set WAITING_CLARIFICATION or AUDIT_PENDING, always validation_status=invalid
```

Proposal branches:

```text
material Gatherer requirement change
→ remove older clarification-review drafts
→ stage one MERGE pending action
→ CONFIRM review

no material change (including a total Gatherer failure)
→ remove older clarification-review drafts
→ retain answer evidence in the request transaction
→ return reason/warning; no pending action
```

A partial Gatherer failure does not erase successful batches: if another batch produced
material requirement changes, the route may return both a warning and a pending draft.

After either branch, the route publishes `clarification_submitted` and returns the
saved state plus proposal metadata. It does not run the Auditor; the user must select
Validate Requirements for a fresh verdict. There is no artifact-lock gate on answer
submission because answers are evidence; any proposed requirement mutation remains
behind CONFIRM.

Transaction rule: answer evidence, duplicate deletion, workflow status, and an optional
pending action commit together at the end of a successful request. The event is published
before that database commit; an unhandled later failure rolls the database transaction
back. Gatherer formatting failures are handled, so they do not by themselves discard
answers.

Persistence note: project question loading can repair exact legacy duplicate rows by
copying an existing resolved answer to its unresolved duplicate. The repair becomes
durable when the surrounding request commits.

Important locations:

- `src/hooks/useChat.ts`
- `src/components/ChatPanel.tsx`
- `src/api/transforms.ts`
- `src/api/client.ts`
- `backend/app/routes/requirements.py`
- `backend/app/agents.py`
- `backend/app/repositories/requirement_state.py`
- `backend/app/repositories/clarification_question.py`

### DOC-UPLOAD — Project knowledge documents and extraction

Purpose: upload project knowledge, convert it to canonical Markdown, preview/delete it,
and optionally extract a reviewable requirement proposal.

Numbering structure:

- `DOC-UPLOAD 1.x` — paperclip/library UI, hook state, API calls, progress, and refresh
- `DOC-UPLOAD 2.x` — upload/list/detail/delete/process/cancel HTTP routes
- `DOC-UPLOAD 3.x` — upload validation, byte cap, signature checks, and conversion
- `DOC-UPLOAD 4.x` — uploaded-document repository and serialization
- `DOC-UPLOAD 5.x` — public response schemas
- `DOC-UPLOAD 6.x` — extraction sanitization, routing, chunking, Gatherer, and proposal

Upload path:

```text
ChatPanel paperclip
→ useDocuments
→ api.uploadDocument
→ routes/documents.py::upload_document
→ format/signature/size validation
→ canonical Markdown conversion
→ DocumentRepository.create
→ persist conversion transcript milestone and commit document
→ publish document_uploaded
```

Upload branches:

```text
processed text document → full canonical Markdown row, status=processed
OCR-only PDF → empty traceable row, status=failed, needs-OCR message
other validation/conversion failure → no document row, committed failure transcript
```

An OCR-only PDF is returned as HTTP 201 with the failed row; it does not propagate the
converter's internal 422. The final `document_uploaded` publish is after the row/message
commit and is not caught, so delivery failure can surface despite durable upload state.

The full canonical Markdown is not edited in place. Its row is not completely immutable:
extraction status changes and explicit deletion removes the row.

Extraction path:

```text
stored document
→ process_document validation/rate limit
→ commit extraction-request transcript
→ sanitize only the LLM copy (embedded image bytes removed)
→ set extraction_pending and publish started
→ full pass or bounded chunked Gatherer passes
→ accumulate/de-duplicate structured output
→ merge with current state and stamp base requirements revision/document reference
→ INSERT_CHUNKED_REQUIREMENTS pending action
→ commit pending action + tracking status + draft-ready transcript
→ CONFIRM flow
```

`document_extraction_started` is published while the pending status is only flushed, not
committed, so an immediate subscriber reload can still observe the earlier status. Progress
events are hints and do not persist domain state.

Terminal extraction branches:

```text
success → extraction_pending remains while draft awaits CONFIRM
cancel/failure → partial output discarded, status=not_extracted, terminal transcript/event
CONFIRM Save → proposed requirements applied, status=extraction_applied
CONFIRM Discard → proposal deleted, status=not_extracted
expired preview cleanup → pending action deleted, status currently remains extraction_pending
```

The cancel endpoint only signals a process-local running task; the process route performs
the durable cleanup. `cancelled=false` means no live task matched that project/document.
The process route does not supersede older extraction drafts from the same document, so
repeated extraction can create multiple pending actions. Cancelling any one through CONFIRM
resets the shared document status even if another draft remains; this is current behavior.
The response currently reports `excluded_anything=false` even though DOCX embedded image
data and alt text are intentionally removed from the LLM-only extraction copy. Canonical
stored Markdown remains complete; the flag should not be read as “binary image payloads
were sent to the model.”

Delete path:

```text
ConfirmModal → delete route → delete stored row → delete currently staged matching drafts
→ publish document_deleted → refresh library
```

Already confirmed requirements are retained. Deletion does not cancel an extraction that
is already running, so the current code has a race in which that run may finish after the
row/draft scan; this is documented behavior, not changed here.

Persistence rule: upload alone adds knowledge and transcript history, not requirements.
Extraction writes tracking/transcript/pending-action state, but proposed requirement fields
become authoritative only through CONFIRM.

Important locations:

- `src/components/ChatPanel.tsx`
- `src/components/DocumentLibrary.tsx`
- `src/hooks/useDocuments.ts`
- `src/api/client.ts`
- `backend/app/routes/documents.py`
- `backend/app/services/document_ingestion_service.py`
- `backend/app/document_processor.py`
- `backend/app/repositories/document.py`
- `backend/app/repositories/base.py`
- `backend/app/workflow_cancellation.py`

### VERSION — PRD snapshot history, diff, and restore

Purpose: append immutable PRD snapshots, page their metadata, calculate section/line comparisons, and copy an old snapshot's available sections into the current document.

Numbered structure:

```text
VERSION 1.x — frontend
1.0 Versions tab coordinator
1.1 paged metadata load; 1.1.1 forced head selection; 1.1.2 timeline mapping
1.2 automatically based diff request; 1.2.1 loaded-base choice; 1.3/1.4 rendering
1.5 confirmed restore → accept route document/version → reload history + locks

VERSION 2.x — authenticated project routes
2.0 export producer → 3.9 stamp prediction → direct 4.1 insert (bypasses 3.6)
2.1 list newest-first metadata; generated_prd is blanked
2.2 diff route → 3.8; missing/invalid comparison becomes 404
2.3 latest full snapshot; 2.4 numbered full snapshot
2.5 restore target-present, changed, unlocked sections → stitch → attempt 3.6

VERSION 3.x — domain service
3.1/3.2 split and classify sections using the supplied current lock set
3.3 summary; 3.4 parse SemVer; 3.5 derive next SemVer
3.6 always attempt ledger append → 4.1 → best-effort state-counter sync
3.7 bounded line diff; 3.8 historical document diff using current locks
3.9 fail-soft next-SemVer prediction for document stamping

VERSION 4.x — repository
4.1 insert max+1 (or optional pin); 4.2 predecessor; 4.3 paged project list
4.4 numbered row; 4.5 latest row shared by recording, state and generation reads

VERSION 5.x — public schemas
5.1 persisted change summary; 5.2 version row; 5.3/5.4 diff; 5.5 restore
```

Important behavior:

- `prd_versions` has no update/delete repository path, and identical content still creates a row when 3.6 succeeds.
- The current Versions-tab effect forces selection back to the loaded latest row whenever selection changes, so an older row cannot remain selected; this is documented behavior here, not fixed by the comment pass.
- Snapshot producers do not share one path or failure policy: generation/confirmation use 3.6 and can abort; export inserts directly through 4.1 and fails with 500; manual section edit/revert, legacy direct write, and restore catch ledger errors and may persist document state without a matching row.
- Diff change kinds are recomputed with current section locks; they are not historical proof of lock state.
- Restore is not an exact whole-document replacement: locked target sections and current-only sections remain. A caught ledger failure still permits the stitched document to persist and returns the source number with blank SemVer.

Important locations:

- `src/components/VersionHistory.tsx`
- `src/hooks/useRequirementStore.ts`
- `src/api/client.ts`
- `backend/app/routes/projects.py`
- `backend/app/version_service.py`
- `backend/app/repositories/prd.py`

### LOCK — Artifact ownership and mutation protection

Purpose: expose lock metadata for persisted requirements and PRD sections and enforce the relevant row lock at mutation boundaries.

Numbered structure:

```text
LOCK 2.x — frontend hook (no separate numbered UI component boundary)
2.0 coordinator; 2.1 UUID-bearing requirement list
2.2 optimistic lock + rollback; 2.3 optimistic unlock + lossy rollback
2.4 requirement-code → persisted UUID adapters

LOCK 3.x — frontend API client
3.1 generic lock/unlock transport

LOCK 4.x — backend routes
4.1/4.2 generic UUID lock/unlock; 4.3 generic status (no SPA caller)
4.4/4.5 legacy requirement-specific routes (no SPA caller)
4.6/4.7 PRD section-key lock/unlock

LOCK 5.x — application service
5.0 supports requirement + prd_section only; 5.1 lock; 5.2 unlock; 5.3 status

LOCK 7.x — legacy requirement repository writers
7.1 lock; 7.2 unlock
```

Important behavior:

- JWT authorization proves project ownership, but `locked_by`/`unlocked_by` is a client-supplied label rather than the authenticated user id.
- Generic same-label re-lock and already-unlocked release are idempotent. Different-label re-lock is 409; different-label unlock is 403; an ownerless generic lock can be cleared by any supplied label.
- Legacy requirement locking differs: any re-lock returns the existing row without owner conflict, while unlock requires exact label equality, including for a null owner.
- Generic service reads are not row-locked, so concurrent lock/unlock requests may race.
- Project/document artifact locks are not supported. PRD edits enforce their section lock; other repositories enforce their own requirement/section gates.
- Lock/unlock event-log writes are fail-soft, while SSE publication is the client refresh notification.
- Generic lock routes publish SSE before the request-scoped transaction commits, so the resulting refresh can race persistence.
- LOCK has no separate numbered UI component, domain-transform (`6.x`), or flow-owned event (`8.x`) boundary; UI buttons remain in their feature flows and publications continue through SYNC 8.x.

Important locations:

- `src/hooks/useArtifactLocks.ts`
- `backend/app/routes/lock.py`
- `backend/app/routes/requirements.py`
- `backend/app/routes/prd_sections.py`
- `backend/app/lock_service.py`

### JOB — Durable background generation and cancellation

Purpose: start, poll, stop, and recover long-running agent or regeneration work.

Numbered structure:

```text
JOB 2.x — frontend orchestration (buttons remain in AUDIT/ARCHITECT flows)
2.1 localStorage resume/backoff polling; 2.2 dual client/server Stop

JOB 3.x — frontend API client
3.1 create; 3.2 poll; 3.3 durable cancel; 3.4 direct-request cancel

JOB 4.x — backend routes
4.1 Architect/Auditor create; 4.2 regeneration create; 4.3 read
4.4 durable cancel; 4.5 direct-request cancel

JOB 5.x — application orchestration
5.1 one-active create; 5.2 typed dispatch; 5.3 lifecycle; 5.3.1 launch
5.4 durable cancel; 5.5/5.5.1/5.5.2 direct-request registry

JOB 7.x — persistence
7.1 generation_jobs row + partial unique active-project index

JOB 8.x — lifecycle effects
8.1 startup recovery; 8.2 shutdown interruption
```

Important behavior:

- Durable jobs survive browser disconnects. Startup requeues active rows unless cancel was requested; shutdown cancellation records interrupted work for recovery.
- One queued/running/cancelling job is allowed per project across all job types. Heavy jobs also share a process-wide inference semaphore and whole-job timeout.
- The browser resumes a saved job id; non-401 read failure discards it and attempts creation, while 401 preserves it for later sign-in.
- Stop sends server cancellation best-effort and immediately aborts local polling. A known durable id uses JOB 5.4; otherwise the separate JOB 5.5 registry is used.
- Regeneration execution currently requests generic lock type `project`, which LOCK 5.0 does not support; those jobs fail before regeneration. This is documented, not fixed.
- The frontend wire type omits runtime `auditor` from its job-type union; this discrepancy is documented, not fixed.

Important locations:

- `src/hooks/chat/backgroundJobs.ts`
- `src/hooks/chat/useAgentCancellation.ts`
- `backend/app/routes/generation_jobs.py`
- `backend/app/generation_job_service.py`
- `backend/app/workflow_cancellation.py`

### SYNC — SSE events and workspace refresh

Purpose: publish project-scoped mutation notifications and reconcile the workspace from authoritative HTTP state.

Numbered structure:

```text
SYNC 2.x — frontend hook (no separate numbered UI component boundary)
2.0 coordinator; 2.1 initial load + retries/initial ordering guard
2.2 held PRD/diagram catch-up; 2.3 EventSource subscription
2.4 debounced reconciliation pull; 2.5 authenticated connection
2.5.1 connected/progress/general-event branches

SYNC 4.x — backend route
4.1 owner-scoped SSE frames, replay id and heartbeat

SYNC 8.x — event/downstream effects
8.0 process-local manager; 8.1 subscribe/replay; 8.2 sequence/history/fan-out
8.3 single-worker guard; 8.4 startup invocation
```

Important behavior:

- SSE normally carries only a notification. General events debounce for 350 ms and trigger a full state pull plus pending-action, section-lock and latest-version reads. Extraction progress bypasses the full pull.
- Initial project loading aborts the previous request and rejects older `updated_at`; SSE reconciliation does not apply that ordering check, and forced catch-up may overlap another refresh.
- Replay is bounded and process-local; subscriber queues are unbounded. Multi-worker startup is rejected unless an explicit unsafe override is enabled.
- Event publication is not transaction-aware. Many mutation routes publish before request-dependency commit, so a debounced pull can still observe pre-commit state and there is no guaranteed follow-up frame.
- Restart loses replay history and resets sequence ids. The frontend ignores every `connected` handshake, so reconnect after restart does not itself force state catch-up.
- There is no periodic polling fallback; EventSource handles reconnects automatically.

Important locations:

- `src/hooks/useProjectSync.ts`
- `backend/app/event_manager.py`
- `backend/app/routes/events.py`

## Broad FLOW references awaiting normalization

The source currently contains broad labels such as `FLOW 10`, `FLOW 14`, and `FLOW 15`. They should not be assigned new meanings until every existing reference has been audited.

Known associations:

| Existing label | Current association |
| --- | --- |
| `FLOW 10` | Clarification flow |
| `FLOW 14` | Document upload/knowledge-base flow |
| `FLOW 15` | Document extraction/traceability marker path |

Prefer the named identifiers `CLARIFY` and `DOC-UPLOAD` for new comments. Existing broad labels can be removed only in a coordinated comment-normalization pass.

## Persistence boundaries

These distinctions must remain explicit in comments:

| Operation | Durable effect |
| --- | --- |
| Router, matcher, Gatherer, Auditor, Architect computation | Produces workflow state or draft output |
| `PendingActionRepository.create` | Stages proposed changes |
| Impact analysis | Read-only |
| Cancel pending action | Deletes the draft; document extraction also resets source status |
| `confirm_action` requirement branch → `RequirementStateRepository.save_or_update` | Commits approved authoritative requirements |
| `confirm_action` PRD branch → section sync + `record_prd_version` + state save | Commits an approved generated PRD |
| `record_prd_version` | Appends an immutable PRD ledger entry |
| Document upload | Adds project knowledge only |
| SSE publication | Notifies clients; does not itself persist domain state |

## Failure-language convention

Use the following language in comments:

- **Fail loudly**: propagate or translate the failure; do not return a plausible result.
- **Fail softly**: return an explicit fallback result while logging the failure.
- **Fail open**: an optional subsystem failure does not block the primary operation.
- **Fail closed**: deny the operation or disable capability when validation cannot be established.
- **No-op guard**: return without mutation when the input does not justify work.
- **Stale-result guard**: reject or ignore work based on an outdated revision.

Do not call a path fail-safe without stating whether it fails open or closed.

## Maintenance checklist

When adding or changing a major flow:

1. Trace the implementation from its actual trigger to its final result.
2. Reuse an existing named flow when the behavior belongs to it.
3. Add numbered comments only at meaningful boundaries.
4. Include reads/writes when persistence could otherwise be misunderstood.
5. Identify staging separately from committing.
6. Document important branches, guards, and failure behavior.
7. Update this registry when files or boundaries change.
8. Update or add tests for behavior changes; comments alone are not verification.
9. Search all references before renumbering an existing flow.
10. Keep comments synchronized with code during review.

## Known documentation gaps

The following areas have useful comments but do not yet follow one complete numbered structure:

- Traceability routes and derived-matrix generation
- Operational governance and review workflow
- PRD PDF/DOCX export and conversion
- PRD-section manual editing and reversion
- Generation-job lifecycle
- Artifact locking and synchronization
- Repository CRUD functions outside the major flows
- Evaluation scripts and schema migrations

These should be added to the index only after their actual call paths are verified. The first normalization pass should improve comment consistency without changing runtime code.
