"""Construction of the single-pass requirement-processing graph.

Node implementations are supplied by the compatibility facade so this module
owns topology without importing ``app.agents`` and creating an import cycle.
"""

import logging
from collections.abc import Callable
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.workflow_orchestration.routing import (
    route_from_matcher,
    route_from_router,
    route_on_demand,
)
from app.workflow_orchestration.state import AgentState

logger = logging.getLogger("app.agents")

WorkflowNode = Callable[[AgentState], Any]


# ROUTING 1.3 — Construct and compile the single-pass LangGraph topology.
# Trigger: app.agents module initialization.
# Input: router, matcher, Gatherer, Auditor, Architect, and delete node callables.
# Calls: ROUTING 1.1 START edge, ROUTING 4.1 router edge, ROUTING 4.2 matcher edge.
# Branches: each conditional edge maps only to declared nodes or END.
# Writes: in-memory compiled graph only; no project/database state.
# Failure: graph configuration errors fail application import/startup.
# Next: compiled prd_workflow invoked by the requirements pipeline.
def build_prd_workflow(
    *,
    router_node: WorkflowNode,
    requirement_matcher_node: WorkflowNode,
    gatherer_node: WorkflowNode,
    auditor_node: WorkflowNode,
    architect_node: WorkflowNode,
    delete_requirement_node: WorkflowNode,
):
    """Build the established graph while keeping node ownership independent."""
    workflow = StateGraph(AgentState)

    workflow.add_node("router_node", router_node)
    workflow.add_node("requirement_matcher_node", requirement_matcher_node)
    workflow.add_node("gatherer_node", gatherer_node)
    workflow.add_node("auditor_node", auditor_node)
    workflow.add_node("architect_node", architect_node)
    workflow.add_node("delete_requirement_node", delete_requirement_node)

    # ROUTING 1.1 — Register the graph's conditional START edge.
    # Calls: ROUTING 1.2 route_on_demand for every workflow invocation.
    workflow.add_conditional_edges(
        START,
        route_on_demand,
        {
            "router_node": "router_node",
            "requirement_matcher_node": "requirement_matcher_node",
            "auditor_node": "auditor_node",
            "architect_node": "architect_node",
            "delete_requirement_node": "delete_requirement_node",
        },
    )
    workflow.add_conditional_edges(
        "router_node",
        route_from_router,
        {
            "requirement_matcher_node": "requirement_matcher_node",
            "auditor_node": "auditor_node",
            "architect_node": "architect_node",
            END: END,
        },
    )
    workflow.add_conditional_edges(
        "requirement_matcher_node",
        route_from_matcher,
        {
            "gatherer_node": "gatherer_node",
            "delete_requirement_node": "delete_requirement_node",
            END: END,
        },
    )

    workflow.add_edge("gatherer_node", END)
    workflow.add_edge("auditor_node", END)
    workflow.add_edge("architect_node", END)
    workflow.add_edge("delete_requirement_node", END)

    compiled = workflow.compile()
    logger.info(
        "StateGraph compiled successfully with requirement matcher agent and workflow router."
    )
    return compiled
