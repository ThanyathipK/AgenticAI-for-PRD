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

Purpose: display a persisted pending-action draft, call the read-only `IMPACT` preview,
apply approved project changes through the correct commit branch, or discard the draft.

Numbering structure:

- `CONFIRM 1.x` — confirmation UIs, handlers, and pending-action refresh
- `CONFIRM 2.x` — frontend API boundary
- `CONFIRM 3.x` — backend list, IMPACT cross-reference, Save, and Discard routes
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
CONFIRM preview → IMPACT 2.x–7.x → return to CONFIRM decision
expired IMPACT route request → CONFIRM lifecycle cleanup deletes + commits row → 410
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
- `backend/app/repositories/pending_action.py`
- `backend/app/repositories/requirement_state.py`

Canonical name: use `CONFIRM` in identifiers. “Confirmation flow” may still be used as ordinary prose.

### IMPACT — Pending-action change and downstream-reference preview

Purpose: compare a staged requirement proposal with current authoritative state and
derive which requirements, stories, criteria, PRD sections, and diagrams would be affected
before the user makes the separate CONFIRM Save/Cancel decision.

Numbering structure:

- `IMPACT 1.x` — impact panel/modal presentation
- `IMPACT 2.x` — frontend preview lifecycle and request handler
- `IMPACT 3.x` — frontend API client
- `IMPACT 4.x` — project-scoped pending-action impact route
- `IMPACT 5.x` — impact orchestration and summary construction
- `IMPACT 6.x` — shape normalization, artifact diffs, and downstream-reference calculation
- `IMPACT 7.x` — pending-action, requirement-state, PRD-section, and diagram reads

There is no `IMPACT 8.x`: successful analysis is derived and read-only. It publishes no
event, schedules no job, and mutates no governed artifact.

Detailed map:

```text
1.0 impact summary/detail panel

2.0 non-blocking preview lifecycle       2.1 project/action-scoped request

3.1 impact GET client

4.1 authenticated pending-action impact route

5.0 read-only orchestration boundary     5.1 state/diff/downstream/summary sequence

6.1 normalize nested/legacy state shapes
6.2 requirement created/updated/removed diff
6.3 story + acceptance-criteria diff     6.4 PRD/diagram reference impact
6.5 fast database-free staged preview

7.1 project-scoped pending-action read   7.2 authoritative requirement-state read
7.3 PRD-section read                     7.4 versioned diagram-document read
```

Preview path:

```text
CONFIRM panel → IMPACT handler 2.0 → client 3.1 → route 4.1
→ pending action 7.1 → current requirement state 7.2
→ normalize 6.1 → requirement/story/criteria diffs 6.2/6.3
→ affected REQ/US codes → PRD section + diagram scan 6.4/7.3/7.4
→ summary 5.1 → panel 1.0 → return to CONFIRM Save/Cancel decision
```

Impact preview failure does not block Save or Cancel; the panel falls back to counts stored
with the staged draft. A zero-diff result disables Save and legacy no-op drafts may be
automatically discarded by the CONFIRM UI lifecycle.

Important boundary discrepancy: valid analysis is read-only, but route `4.1` retains an
existing CONFIRM lifecycle behavior—an expired pending row is deleted and committed before
the route returns `410`. This cleanup is not downstream impact and does not justify an
`IMPACT 8.x` layer.

The current pending-action impact path does not call `dependency_graph_service`. It scans
stored PRD sections and Mermaid documents directly using the same code extractor as
`TRACEABILITY`. Dependency-graph regeneration planning is a separate path.

Important locations:

- `src/components/ConfirmationPanel.tsx`
- `src/api/client.ts`
- `backend/app/routes/lock.py`
- `backend/app/impact_service.py`
- `backend/app/repositories/pending_action.py`
- `backend/app/repositories/requirement_state.py`
- `backend/app/repositories/prd_section.py`
- `backend/app/repositories/prd.py`

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

- `DOC-UPLOAD 1.x` — paperclip and document-library UI
- `DOC-UPLOAD 2.x` — frontend hook state, actions, progress, and reconciliation
- `DOC-UPLOAD 3.x` — frontend API client boundary
- `DOC-UPLOAD 4.x` — authenticated upload/list/detail/delete/process/cancel HTTP routes
- `DOC-UPLOAD 5.x` — upload/extraction orchestration, milestones, and draft lifecycle
- `DOC-UPLOAD 6.x` — validation/conversion and extraction domain logic
- `DOC-UPLOAD 7.x` — uploaded-document repository and serialization
- `DOC-UPLOAD 8.x` — events, live progress, and process-local cancellation effects

Response DTOs are intentionally unnumbered: they describe HTTP boundaries but do not
execute a flow step.

Detailed map:

```text
1.1 paperclip/select file                  1.2 library/status list
1.3 Markdown preview                      1.4 extract/progress UI
1.5 delete confirmation

2.0 document hook coordinator             2.1 local document state
2.2 list/reconciliation                   2.3 upload action
2.4 extraction action                     2.4.1 live-progress bridge
2.5 Stop action / JOB 2.3                 2.6 delete action
2.7 pending-action resolution refresh

3.1 upload client                         3.2 list client
3.3 detail client                         3.4 extraction client
3.5 cancel client / JOB 3.5               3.6 delete client

4.1 upload route                          4.2 list route
4.3 detail route                          4.4 delete route
4.5 extraction route                      4.6 cancel route / JOB 4.6

5.1 conversion transcript/commit          5.2 delete-time draft eviction
5.3 extraction transcript/commit          5.4 full/chunked Gatherer orchestration
5.5 cancel/failure cleanup                5.6 proposal + pending-action staging

6.1 validation and canonical conversion  6.2 LLM-copy sanitization
6.3 extraction budget/mode                6.4 chunking + one Gatherer pass
6.5 accumulation/de-duplication           6.6 revision-guarded merge proposal
6.7 document traceability marker

7.1 create                                7.2 list
7.3 detail                                7.4 hard delete
7.5 extraction-status update              7.6 serialization

8.1 document_uploaded event               8.2 document_deleted event
8.3 cancellation effect / JOB 5.6         8.4 started/progress/terminal live hints
8.5 draft-ready event
```

Upload path:

```text
ChatPanel 1.1
→ useDocuments upload action 2.3
→ API client 3.1
→ upload route 4.1
→ format/MIME + signature + byte-cap validation 6.1
→ canonical Markdown conversion 6.1
→ DocumentRepository.create 7.1
→ conversion milestone + commit 5.1
→ document_uploaded 8.1
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
library Extract action 1.4 → hook 2.4 → client 3.4
→ process_document validation/rate limit 4.5
→ commit extraction-request transcript 5.3
→ sanitize only the LLM copy 6.2
→ set extraction_pending 7.5 and publish started 8.4
→ register the running request in JOB 5.6
→ budget/mode 6.3 → chunking/Gatherer orchestration 5.4 + 6.4
→ accumulate/de-duplicate structured output 6.5
→ merge current state + stamp revision/document reference 6.6/6.7
→ INSERT_CHUNKED_REQUIREMENTS pending action 5.6
→ commit pending action + tracking status + draft-ready transcript 5.3/5.6
→ publish draft-ready event 8.5
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

Stop crosses into JOB: UI 1.4 → DOC-UPLOAD 2.5 / JOB 2.3 → JOB 3.5 → JOB 4.6
→ JOB 5.6 cancellation (the DOC-UPLOAD 8.3 effect). The extraction request registers
with JOB 5.6 before Gatherer work and unregisters in `finally`. The cancel endpoint only
signals a process-local running task; DOC-UPLOAD 4.5 catches cancellation and performs
the durable status, transcript, and terminal-event cleanup. `cancelled=false` means no
live task matched that project/document.
The process route does not supersede older extraction drafts from the same document, so
repeated extraction can create multiple pending actions. Cancelling any one through CONFIRM
resets the shared document status even if another draft remains; this is current behavior.
The response currently reports `excluded_anything=false` even though DOCX embedded image
data and alt text are intentionally removed from the LLM-only extraction copy. Canonical
stored Markdown remains complete; the flag should not be read as “binary image payloads
were sent to the model.”

Delete path:

```text
ConfirmModal 1.5 → hook 2.6 → client 3.6 → delete route 4.4
→ hard-delete stored row 7.4 → evict matching staged drafts 5.2
→ publish document_deleted 8.2 → reconcile list 2.2
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

### PRD-SECTION — Part-level edit, history, and revert

Purpose: edit one governed PRD part without overwriting concurrent changes, retain
append-only part history, and optionally restore one historical part as a new version.

Numbering structure:

- `PRD-SECTION 1.x` — editor UI; part-history/revert UI is currently absent
- `PRD-SECTION 2.x` — frontend part-state and save/lock handlers
- `PRD-SECTION 3.x` — frontend API client
- `PRD-SECTION 4.x` — list/edit/revert/lock/detail/history routes
- `PRD-SECTION 5.x` — edit/revert orchestration and document propagation
- `PRD-SECTION 6.x` — canonical parts, split, merge, stitch, and seeding logic
- `PRD-SECTION 7.x` — section and append-only section-version repositories
- `PRD-SECTION 8.x` — edit/revert publication and downstream refresh

Detailed map:

```text
1.0 nine-part editor UI

2.1 parts load                         2.2 optimistic save handler
2.3 metadata reconciliation            2.4 lock/unlock handler
2.5 return human-owned part to AI

3.1 list client                        3.2 save client
3.3 lock client                        3.4 unlock client

4.1 list route                         4.2 edit route
4.3 part-revert route                  4.4/4.5 lock/unlock routes
4.6 detail route                       4.7 part-history route

5.1 latest-document reconciliation + merge
5.2 edit persistence + full-document propagation
5.3 append-only revert + full-document propagation

6.1 canonical part/review contracts    6.2 heading-to-key mapping
6.3 document splitting                 6.4 canonical stitching
6.5 three-way edit merge               6.6 assemble stored parts
6.7 first-write seeding

7.1 ordered list                       7.2 key lookup
7.3 create                             7.4 append history + update materialized part
7.5 version-history reads

8.1 prd_section_updated                8.2 prd_section_reverted
```

Edit path:

```text
PRDEditor 1.0 → save handler 2.2 → client 3.2 → edit route 4.2
→ reconcile latest full PRD 5.1 → three-way merge 6.5
→ append previous section version + update part 7.4
→ stitch all parts 6.6 → VERSION ledger first → persist full PRD state 5.2
→ publish prd_section_updated 8.1 → frontend refresh
```

Part-revert path:

```text
history route 4.7 → version read 7.5
→ revert route 4.3 → target version read 7.5
→ append current content + materialize restored content 7.4
→ stitch 6.6 → VERSION ledger first → persist full PRD state 5.3
→ publish prd_section_reverted 8.2
```

Important discrepancy: the SPA API client has no part-history or part-revert methods,
and the editor has no corresponding controls. Routes `4.7` and `4.3` are therefore
API/test-only. Whole-document restore remains in `VERSION`; it is not part revert.

Important locations:

- `src/components/PRDEditor.tsx`
- `src/hooks/useRequirementStore.ts`
- `src/api/client.ts`
- `backend/app/routes/prd_sections.py`
- `backend/app/prd_section_service.py`
- `backend/app/repositories/prd_section.py`

### PRD-EXPORT — Snapshot production and PDF/DOCX downloads

Purpose: create an optional export snapshot or compile the latest saved PRD into a
template-faithful PDF/DOCX with authoritative versioned filenames.

Numbering structure:

- `PRD-EXPORT 1.x` — export menu and PDF/DOCX buttons
- `PRD-EXPORT 2.x` — frontend export/download coordinator
- `PRD-EXPORT 3.x` — Blob API client, filename extraction, and error decoding
- `PRD-EXPORT 4.x` — snapshot/PDF/DOCX routes
- `PRD-EXPORT 5.x` — source resolution, legacy filling, compiler choice, and naming
- `PRD-EXPORT 6.x` — LaTeX normalization and DOCX/PDF compilation
- `PRD-EXPORT 7.x` — saved-document reads and optional ledger insertion
- `PRD-EXPORT 8.x` — binary response/browser download and export-event publication

Detailed map:

```text
1.0 export menu                         1.1 DOCX choice / 1.2 PDF choice

2.0 shared export coordinator          2.1 browser Blob download
2.2 Blob-error presentation

3.0 binary request/filename boundary   3.0.1 Blob error decoder
3.1 PDF client                         3.2 DOCX client

4.0 snapshot-producing export route    4.1 PDF route
4.2 DOCX route

5.0 snapshot assembly/stamping         5.1 latest-source resolution
5.2 legacy Markdown template filling   5.3 compiler selection/fallback
5.4 filename/SemVer response metadata

6.1 sanitize/wrap LaTeX                6.2 native LaTeX → DOCX
6.3 Pandoc DOCX fallback               6.4 DOCX → PDF with LibreOffice
6.5 direct Tectonic PDF fallback       6.6 template filling/version stamping

7.1 current document read with document/ledger compatibility fallback
7.2 direct optional VERSION-ledger insertion for snapshot-producing export

8.1 binary attachment response → browser download
8.2 prd_exported publication for snapshot-producing export
```

File-download path:

```text
WorkspaceNavigation 1.x → coordinator 2.0 → Blob client 3.1/3.2
→ PDF/DOCX route 4.1/4.2 → project + latest saved document reads 7.1
→ legacy Markdown rebuilt in official template 5.2/6.6 when needed
→ native DOCX 6.2 (Pandoc fallback 6.3)
→ PDF: native DOCX → LibreOffice 6.4 (direct Tectonic fallback 6.5)
→ filename from latest ledger SemVer 5.4
→ binary attachment/headers 8.1 → browser download 2.1
```

Compiler/tool failures map to explicit HTTP responses: unusable content `422`, compile
failure `502`, and unavailable toolchain `503`. Binary FastAPI error bodies arrive at the
browser as Blobs and are decoded by `3.0.1` before a generic frontend fallback is shown.

Snapshot-producing path:

```text
route 4.0 → deterministic official-template fill 5.0/6.6
→ fail-soft VERSION 6.9 SemVer prediction → stamp document
→ mandatory direct ledger insert 7.2 / VERSION 7.1
→ publish prd_exported 8.2 → return generated document/version metadata
```

This snapshot producer intentionally bypasses ordinary `VERSION 6.6`: it stores no
computed changed-section summary and does not synchronize the requirement-state version
counter. Its ledger insertion is mandatory; failure returns `500`. The binary PDF/DOCX
routes do not create ledger rows—they compile the latest already-saved document.

Important locations:

- `src/components/WorkspaceNavigation.tsx`
- `src/hooks/useProjectState.ts`
- `src/api/client.ts`
- `backend/app/routes/projects.py`
- `backend/app/latex_service.py`
- `backend/app/latex_docx_native.py`
- `backend/app/prd_filler.py`

There is currently no `backend/app/routes/exports.py`; export endpoints live in
`backend/app/routes/projects.py`.

### TRACEABILITY — Derived requirement coverage matrix

Purpose: derive a read-only relationship matrix from authoritative requirements,
stories, acceptance criteria, PRD sections, and diagrams, including coverage gaps and
stale textual references.

Numbering structure:

- `TRACEABILITY 1.x` — matrix/hierarchy UI
- `TRACEABILITY 2.x` — frontend load/state lifecycle
- `TRACEABILITY 3.x` — frontend API client
- `TRACEABILITY 4.x` — authenticated traceability route
- `TRACEABILITY 5.x` — matrix orchestration and project-health reuse
- `TRACEABILITY 6.x` — reference extraction, relationship assembly, coverage, and staleness
- `TRACEABILITY 7.x` — authoritative artifact reads

There is no `TRACEABILITY 8.x`: this flow is derived and read-only. It performs no
commit, event publication, job scheduling, or downstream mutation.

Detailed map:

```text
1.0 hierarchy/matrix renderer            1.1 Requirements-workspace mount

2.0 colocated matrix state coordinator   2.1 project/token/manual-refresh load

3.1 traceability GET client

4.1 owner-authorized matrix route

5.1 artifact-load + matrix orchestration
5.2 project-health reuse of the same coverage result

6.1 REQ/US code extraction               6.2 valid/stale reference indexing + rows
6.3 coverage and gap derivation

7.1 requirement/epic/story/criterion relational reads
7.2 PRD-section repository read          7.3 PRD/diagram document read
7.4 project existence read
```

Read path:

```text
Requirements workspace 1.1 → matrix UI/state 1.0/2.0
→ load handler 2.1 → client 3.1 → route 4.1
→ verify project 7.4 → orchestrator 5.1
→ read governed artifacts 7.1–7.3
→ extract normalized REQ/US references 6.1
→ index direct + story-inherited section/diagram links and stale codes 6.2
→ calculate traced rows and explicit coverage gaps 6.3
→ return unpersisted matrix DTO → replace frontend state 2.1
```

`traced=true` requires at least one story, acceptance criteria on every linked story,
and at least one PRD-section reference. Diagram linkage is reported separately and is
not required for the traced flag. Archived/inactive stories and criteria are excluded;
unknown codes found in PRD/diagram text are returned as stale references.

Project health calls `5.2`, which reuses this matrix coverage and then combines it with
audit, clarification, waiver, and review information. That consumer does not make the
TRACEABILITY flow stateful.

Response types in `src/api/types.ts` and `backend/app/schemas.py` remain unnumbered DTOs.

Important locations:

- `src/components/MainArtifactPanel.tsx`
- `src/components/RequirementTraceability.tsx`
- `src/api/client.ts`
- `src/api/types.ts`
- `backend/app/routes/traceability.py`
- `backend/app/traceability_service.py`
- `backend/app/project_health_service.py`

### VERSION — PRD snapshot history, diff, and restore

Purpose: append immutable PRD snapshots, page their metadata, calculate section/line comparisons, and copy an old snapshot's available sections into the current document.

Numbering structure:

- `VERSION 1.x` — Versions-tab timeline, diff, and restore UI
- `VERSION 2.x` — frontend ledger/restore state and handlers
- `VERSION 3.x` — frontend API client
- `VERSION 4.x` — authenticated version-list/diff/read/restore routes
- `VERSION 5.x` — restore orchestration and current-document propagation
- `VERSION 6.x` — section comparison, SemVer, ledger recording, and diff domain logic
- `VERSION 7.x` — append-only PRD-version repository
- `VERSION 8.x` — restore event publication and downstream refresh

Response DTOs are intentionally unnumbered because they describe data boundaries rather
than execute flow steps.

```text
1.0 Versions-tab coordinator
1.1 forced-head selection               1.2 automatic diff request/base choice
1.3 line renderer                       1.4 per-section diff card
1.5 confirmed restore UI

2.1 paged ledger-state load             2.2 restore coordinator + state reload

3.1 paged metadata client               3.2 diff client
3.3 restore client

4.1 newest-first metadata list          4.2 diff route
4.3 latest full snapshot                4.4 numbered full snapshot
4.5 whole-document restore route

5.1 select target-present changed unlocked parts and update them
5.2 stitch all current parts, attempt ledger append, persist current PRD

6.1/6.2 classify and split sections     6.3 fallback summary
6.4 parse SemVer                        6.5 derive next SemVer
6.6 ledger append + best-effort state-counter sync
6.7 bounded line diff                   6.8 historical diff using current locks
6.9 fail-soft next-SemVer prediction

7.1 insert max+1 (or optional pin)      7.2 predecessor
7.3 paged project list                  7.4 numbered row
7.5 latest row

8.1 prd_version_restored publication
```

Whole-document restore path:

```text
VersionHistory confirmation 1.5 → restore handler 2.2 → API client 3.3
→ restore route 4.5 → target snapshot 7.4
→ seed from current document and visit target-present parts 5.1
→ preserve locked target parts, skip identical parts, retain current-only parts
→ append section history/update changed unlocked parts through PRD-SECTION 7.4
→ stitch every current part 5.2 → attempt ledger append 6.6 → persist current PRD
→ publish prd_version_restored 8.1 → reload ledger + section metadata 2.2
```

Important behavior:

- `prd_versions` has no update/delete repository path, and identical content still creates a row when 6.6 succeeds.
- The current Versions-tab effect forces selection back to the loaded latest row whenever selection changes, so an older row cannot remain selected; this is documented behavior here, not fixed by the comment pass.
- Snapshot producers do not share one path or failure policy: generation/confirmation use 6.6 and can abort; `PRD-EXPORT 4.0` inserts directly through 7.1 and fails with 500; manual section edit/revert, legacy direct write, and restore catch ledger errors and may persist document state without a matching row.
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
2.1 localStorage resume/backoff polling; 2.2 durable/direct Stop
2.3 document-extraction Stop coordinator

JOB 3.x — frontend API client
3.1 create; 3.2 poll; 3.3 durable cancel; 3.4 direct-request cancel
3.5 document-extraction cancel

JOB 4.x — backend routes
4.1 Architect/Auditor create; 4.2 regeneration create; 4.3 read
4.4 durable cancel; 4.5 direct-request cancel; 4.6 document-extraction cancel

JOB 5.x — application orchestration
5.1 one-active create; 5.2 typed dispatch; 5.3 lifecycle; 5.3.1 launch
5.4 durable cancel; 5.5/5.5.1/5.5.2 direct-request registry
5.6 document-extraction task registry

JOB 7.x — persistence
7.1 generation_jobs row + partial unique active-project index

JOB 8.x — lifecycle effects
8.1 startup recovery; 8.2 shutdown interruption
```

Important behavior:

- Durable jobs survive browser disconnects. Startup requeues active rows unless cancel was requested; shutdown cancellation records interrupted work for recovery.
- One queued/running/cancelling job is allowed per project across all job types. Heavy jobs also share a process-wide inference semaphore and whole-job timeout.
- The browser resumes a saved job id; non-401 read failure discards it and attempts creation, while 401 preserves it for later sign-in.
- Generation Stop sends server cancellation best-effort and immediately aborts local
  polling. A known durable id uses JOB 5.4; otherwise the separate JOB 5.5 direct-request
  registry is used.
- An active document extraction takes the dedicated JOB 2.3 → 3.5 → 4.6 → 5.6 branch.
  JOB 5.6 only cancels the process-local task; the running DOC-UPLOAD 4.5 request owns
  durable cleanup. The registry remains implemented in `workflow_cancellation.py` but is
  a separate singleton from the JOB 5.5 direct-workflow registry.
- Regeneration execution currently requests generic lock type `project`, which LOCK 5.0 does not support; those jobs fail before regeneration. This is documented, not fixed.
- The frontend wire type omits runtime `auditor` from its job-type union; this discrepancy is documented, not fixed.

Important locations:

- `src/hooks/chat/backgroundJobs.ts`
- `src/hooks/chat/useAgentCancellation.ts`
- `backend/app/routes/generation_jobs.py`
- `backend/app/generation_job_service.py`
- `backend/app/workflow_cancellation.py`

Document-extraction cancellation cross-flow:

```text
DOC-UPLOAD 4.5 extraction run → JOB 5.6 register
DocumentLibrary/global Stop → DOC-UPLOAD 2.5 / JOB 2.3
→ JOB 3.5 client → JOB 4.6 route → JOB 5.6 cancel
→ running DOC-UPLOAD 4.5 catches CancelledError
→ DOC-UPLOAD 5.5 durable cleanup + DOC-UPLOAD 8.4 terminal publication
→ JOB 5.6 unregister in finally
```

There is no durable cancellation row for extraction. Registry loss on process exit means
the cancel endpoint cannot find that task; extraction persistence remains owned by the
request path rather than JOB 7.x.

### EVENT — Persisted artifact-event history queries

Purpose: query the append-only `artifact_event_logs` history by recency, artifact,
action, or project. This flow reads durable audit history; it does not deliver live
notifications or reconcile workspace state.

Implemented numbering:

- `EVENT 4.x` — authenticated history-query routes
- `EVENT 7.x` — `ArtifactEventLogRepository` history reads

The following conventional layers are currently absent and therefore unnumbered:

- No `EVENT 1.x` history UI
- No `EVENT 2.x` frontend query/filter state
- No `EVENT 3.x` API client
- No `EVENT 5.x` orchestration service; routes validate and call repositories directly
- No `EVENT 6.x` domain transformation
- No `EVENT 8.x` effects; queries do not publish or trigger refresh

Detailed map:

```text
4.1 recent owner-scoped history across projects → 7.1 recent read
4.2 owner-scoped artifact type/id history       → 7.2 artifact read
4.3 owner-scoped action history                 → 7.3 action read
4.4 owner-authorized project/filter history     → 7.4 project read
```

All queries are newest-first and apply `limit`/`offset`. The response `total` is the
number of rows in the returned page, not a database-wide count.

Known route-order discrepancy: dynamic route `4.2`
(`/api/events/{artifact_type}/{artifact_id}`) is registered before static-prefix route
`4.3` (`/api/events/action/{action}`). Depending on FastAPI/Starlette route matching,
an action URL can be consumed as artifact type `action`; this comment pass documents the
ordering and does not change it.

`ArtifactEventLogRepository.log_event` remains the append-only writer used by mutation
flows. It is not numbered as an EVENT query step. Likewise, `SYNC 4.1` remains the SSE
route even though it shares `routes/events.py`; it uses the process-local event manager,
not persisted `artifact_event_logs` reads.

Event-history response schemas are unnumbered DTOs.

Important locations:

- `backend/app/routes/events.py`
- `backend/app/repositories/event_log.py`
- `backend/app/schemas.py`

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

Intentionally absent layers:

- No `SYNC 1.x`: synchronization has no standalone UI component.
- No `SYNC 3.x`: `useProjectSync` constructs the browser-native `EventSource` directly;
  there is no API-client wrapper.
- No `SYNC 5.x–7.x`: the live bus has no application service, domain transform, or
  repository boundary. Subscriber queues, sequence counters, and replay history are
  process-local memory owned by `SYNC 8.x`.

Important behavior:

- SSE normally carries only a notification. General events debounce for 350 ms and trigger a full state pull plus pending-action, section-lock and latest-version reads. Extraction progress bypasses the full pull.
- Initial project loading aborts the previous request and rejects older `updated_at`; SSE reconciliation does not apply that ordering check, and forced catch-up may overlap another refresh.
- Replay is bounded and process-local; subscriber queues are unbounded. Multi-worker startup is rejected unless an explicit unsafe override is enabled.
- Event publication is not transaction-aware. Many mutation routes publish before request-dependency commit, so a debounced pull can still observe pre-commit state and there is no guaranteed follow-up frame.
- Restart loses replay history and resets sequence ids. The frontend ignores every `connected` handshake, so reconnect after restart does not itself force state catch-up.
- There is no periodic polling fallback; EventSource handles reconnects automatically.
- Persisted historical queries are `EVENT 4.x/7.x`; live subscription, replay, heartbeat,
  publication, and workspace reconciliation remain `SYNC`. Sharing
  `backend/app/routes/events.py` does not merge those two flows.

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

- Operational governance and review workflow
- Repository CRUD functions outside the major flows
- Evaluation scripts and schema migrations

These should be added to the index only after their actual call paths are verified. The first normalization pass should improve comment consistency without changing runtime code.
