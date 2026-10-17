"""Parse the optional chat-line priority prefixes used by requirement intake."""

from dataclasses import dataclass
import re
from typing import List


_PRIORITY_LINE = re.compile(r"^\s*(!{1,3})(?:\s+|$)(.*)$")


@dataclass(frozen=True)
class PrioritizedRequest:
    text: str
    level: int
    order: int


def parse_prioritized_requests(raw_input: str) -> List[PrioritizedRequest]:
    """Return non-empty request lines in execution order.

    Prefixes are recognized only at the start of a line and only when followed
    by whitespace, so ordinary punctuation such as ``Great!`` is unaffected.
    Unmarked lines have normal priority (level 2).
    """
    requests: List[PrioritizedRequest] = []
    for order, line in enumerate(str(raw_input or "").splitlines()):
        if not line.strip():
            continue
        match = _PRIORITY_LINE.match(line)
        if match:
            marks, text = match.groups()
            if not text.strip():
                continue
            level = {"!!!": 4, "!!": 3, "!": 1}[marks]
            requests.append(PrioritizedRequest(text.strip(), level, order))
        else:
            requests.append(PrioritizedRequest(line.strip(), 2, order))
    return sorted(requests, key=lambda item: (-item.level, item.order))


def format_prioritized_input(raw_input: str) -> str:
    """Build an explicit, stable priority block for the Gatherer prompt."""
    source = str(raw_input or "")
    if not any(_PRIORITY_LINE.match(line) for line in source.splitlines()):
        return source
    requests = parse_prioritized_requests(source)
    if not requests:
        return str(raw_input or "")
    labels = {4: "MUST COMPLETE", 3: "IMPORTANT", 2: "NORMAL", 1: "NICE TO HAVE"}
    return "\n".join(
        f"[{labels[item.level]}] {item.text}" for item in requests
    )
