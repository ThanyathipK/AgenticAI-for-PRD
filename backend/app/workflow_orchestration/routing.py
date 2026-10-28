"""Pure routing decisions for the requirement-processing graph."""

import logging

from langgraph.graph import END

from app.workflow_orchestration.state import AgentState

logger = logging.getLogger("app.agents")


# ROUTING 1.2 — Select the first graph node for explicit or default processing.
# Trigger: ROUTING 1.1 conditional START edge.
# Input: target_agent and detected_intent from AgentState.
# Branches: auditor → auditor; architect → architect; delete → matcher;
#           otherwise → ROUTING 2.0 router node.
# Writes: NONE; returns a graph node name.
# Failure: absent target/intent safely defaults to router_node.
def route_on_demand(state: AgentState) -> str:
    """Select the graph entry node for an explicitly requested agent."""
    target = state.get("target_agent", "gatherer")
    detected_intent = state.get("detected_intent", "")

    if target == "auditor":
        logger.info("Routing to auditor_node on-demand.")
        return "auditor_node"
    if target == "architect":
        logger.info("Routing to architect_node on-demand.")
        return "architect_node"
    if target == "delete_requirement" or detected_intent == "DELETE_REQUIREMENT":
        logger.info("Routing delete request through requirement matching before archive preview.")
        return "requirement_matcher_node"
    logger.info("Routing to router_node before Gatherer.")
    return "router_node"


# ROUTING 4.0 — Conditional dispatch phase after classification and matching.
# Trigger: router_node or requirement_matcher_node completes.
# Calls: ROUTING 4.1 or ROUTING 4.2 according to the completed node.
# Writes: NONE; both decisions return a graph destination or END.

# ROUTING 4.1 — Dispatch the classifier result after the router node.
# Trigger: ROUTING 2.0 completes and writes workflow_routing/target_agent.
# Input: workflow classification and optional command target.
# Branches: REQUIREMENT → matcher; supported COMMAND → Auditor/Architect;
#           CHAT, QUESTION, or unsupported COMMAND → END.
# Writes: NONE; returns a node name or END.
def route_from_router(state: AgentState) -> str:
    """Dispatch from the classifier result to a worker or graph termination."""
    routing = state.get("workflow_routing") or {}
    workflow = str(routing.get("workflow", "REQUIREMENT")).upper()
    if workflow == "REQUIREMENT":
        return "requirement_matcher_node"
    if workflow == "COMMAND":
        target = state.get("target_agent")
        if target == "auditor":
            return "auditor_node"
        if target == "architect":
            return "architect_node"
    return END


# MATCHING 4.0 — Dispatch the graph match outcome to its consumer.
# Trigger: MATCHING 1.5 or MATCHING 1.6 returns from requirement_matcher_node.
# Input: detected intent, match status/confidence, and validation status.
# Branches: confident DELETE → MATCHING 3.3; unresolved/invalid → END;
#           confident non-delete → Gatherer (MATCHING 3.1/3.2).
# Writes: NONE; this is the MATCHING flow's graph handoff boundary.
#
# ROUTING 4.2 — Prevent unresolved matches from reaching mutation nodes.
# Trigger: requirement_matcher_node completes.
# Input: detected intent, match status/confidence, and requirement validation state.
# Branches: confident DELETE → delete node; ambiguous/invalid → END;
#           otherwise → Gatherer.
# Writes: NONE; returns a node name or END.
def route_from_matcher(state: AgentState) -> str:
    """Prevent ambiguous matches from reaching a mutation node."""
    requirement_state = state.get("requirement_state", {})
    if state.get("detected_intent") == "DELETE_REQUIREMENT":
        match = state.get("requirement_match") or {}
        status = str(match.get("status") or "").upper()
        if status in {"AMBIGUOUS", "LOW_CONFIDENCE"} or float(match.get("confidence", 0.0)) < 0.75:
            return END
        return "delete_requirement_node"
    if requirement_state.get("validation_status") == "invalid":
        return END
    return "gatherer_node"
