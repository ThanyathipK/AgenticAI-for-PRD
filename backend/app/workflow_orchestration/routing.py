"""Pure routing decisions for the requirement-processing graph."""

import logging

from langgraph.graph import END

from app.workflow_orchestration.state import AgentState

logger = logging.getLogger("app.agents")


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
