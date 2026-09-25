"""Action planners for the visual browser agent.

A planner turns the goal plus the interactable DOM nodes into a single action
dict. Two implementations are provided:

* :class:`LLMPlanner` — asks a real LLM (OpenAI or Gemini, reusing the provider
  resolution in :mod:`byconn.pipeline.llm_extractor`) to choose the next action
  from the node list and its coordinates.
* :class:`HeuristicPlanner` — deterministic keyword matching, used when no
  API key is configured.

Both return the same shape so :class:`~byconn.visual_agent.agent_loop.VisualBrowserAgent`
is agnostic to which one is active.
"""

import logging
from typing import Any, Dict, Optional, Sequence

logger = logging.getLogger("byconnx.visual_agent.planner")

VALID_ACTIONS = {"click", "type", "hover", "scroll", "stop"}

# Truncation keeps the node list inside a reasonable prompt budget.
MAX_NODES_IN_PROMPT = 60
MAX_LABEL_CHARS = 60
# Images are the dominant token cost, so oversized captures are dropped and
# the planner falls back to reasoning over DOM nodes alone.
MAX_IMAGE_BYTES = 1_500_000
VISION_NOTE = (
    "A screenshot of the current viewport is attached. Use it to judge what is "
    "actually visible and whether an action helped.\n\n"
)
NO_VISION_NOTE = "(No screenshot available; rely on the element list below.)\n\n"

PLANNER_PROMPT = """
You are controlling a web page to accomplish a goal.

GOAL:
{goal}

CURRENT PAGE: {url}
{vision_note}
INTERACTABLE ELEMENTS (id, role, label, x, y):
{nodes}

Choose exactly ONE next action. Reply with JSON only, in this shape:
{{"type": "click|type|hover|scroll|stop", "id": <element id>, "x": <int>, "y": <int>, "value": "<text to type>", "reason": "<short reason>"}}

Rules:
- Use an element's id and its x/y coordinates exactly as listed.
- Use "type" with a "value" when the goal requires entering text.
- Use "scroll" to reach elements further down the page.
- Use "stop" when the goal is already satisfied or no action can help.
"""


def _format_nodes(nodes: Sequence[Dict[str, Any]]) -> str:
    """Renders nodes as a compact, model-friendly list."""
    lines = []
    for node in list(nodes)[:MAX_NODES_IN_PROMPT]:
        label = (node.get("text") or "").strip()[:MAX_LABEL_CHARS]
        lines.append(
            f"- id={node.get('id')} role={node.get('role')} "
            f"label={label!r} x={node.get('x')} y={node.get('y')}"
        )
    return "\n".join(lines) if lines else "(no interactable elements found)"


def normalise_action(raw: Any, nodes: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Coerces a model reply into a valid action dict.

    Resolves the element by ``id`` when possible so coordinates always match a
    real node, and falls back to ``stop`` for anything unrecognised.
    """
    if not isinstance(raw, dict):
        return {"type": "stop"}

    action_type = str(raw.get("type") or "").strip().lower()
    if action_type not in VALID_ACTIONS:
        return {"type": "stop"}
    if action_type == "stop":
        return {"type": "stop"}

    action: Dict[str, Any] = {"type": action_type}

    # Prefer the declared id, then the raw coordinates, to locate the target.
    target = None
    node_id = raw.get("id")
    if node_id is not None:
        for node in nodes:
            if str(node.get("id")) == str(node_id):
                target = node
                break
    if target is None and raw.get("x") is not None and raw.get("y") is not None:
        try:
            x, y = int(raw["x"]), int(raw["y"])
            for node in nodes:
                if node.get("x") == x and node.get("y") == y:
                    target = node
                    break
        except (TypeError, ValueError):
            target = None

    if target is not None:
        action["x"] = target.get("x")
        action["y"] = target.get("y")
        action["element_id"] = target.get("id")
    elif action_type in {"click", "type", "hover"}:
        # A click with no resolvable target is not safe to perform.
        try:
            action["x"] = int(raw["x"])
            action["y"] = int(raw["y"])
        except (KeyError, TypeError, ValueError):
            return {"type": "stop"}

    if action_type == "type":
        action["value"] = str(raw.get("value") or "")
    if raw.get("reason"):
        action["reason"] = str(raw["reason"])[:200]
    return action


class HeuristicPlanner:
    """Deterministic keyword planner; the fallback when no LLM is configured."""

    SUBMIT_WORDS = ("submit", "enter", "go", "search", "continue", "next")
    STOP_WORDS = ("done", "complete", "finished", "success")

    def __init__(self, typed_value: str = ""):
        self.typed_value = typed_value

    async def plan(
        self,
        goal: str,
        nodes: Sequence[Dict[str, Any]],
        url: str = "",
        screenshot: Optional[bytes] = None,
    ) -> Dict[str, Any]:
        """Picks an input to fill, then a submit control, else stops.

        ``screenshot`` is accepted for interface parity and ignored; the
        heuristic has no vision.
        """
        lowered = (goal or "").lower()
        if any(word in lowered for word in self.STOP_WORDS) and not nodes:
            return {"type": "stop"}

        for node in nodes:
            role = (node.get("role") or "").lower()
            label = (node.get("text") or "").lower()
            if "search" in label or role in {"input", "searchbox", "textbox"}:
                return {
                    "type": "type",
                    "x": node.get("x"),
                    "y": node.get("y"),
                    "element_id": node.get("id"),
                    "value": self.typed_value or goal,
                    "reason": "heuristic: input field matched",
                }

        for node in nodes:
            label = (node.get("text") or "").lower()
            if any(word in label for word in self.SUBMIT_WORDS):
                return {
                    "type": "click",
                    "x": node.get("x"),
                    "y": node.get("y"),
                    "element_id": node.get("id"),
                    "reason": "heuristic: submit control matched",
                }

        if nodes:
            first = nodes[0]
            return {
                "type": "click",
                "x": first.get("x"),
                "y": first.get("y"),
                "element_id": first.get("id"),
                "reason": "heuristic: fallback to first interactable",
            }
        return {"type": "stop"}


class LLMPlanner:
    """Planner backed by a real LLM call.

    Reuses the provider resolution in
    :class:`~byconn.pipeline.llm_extractor.LLMExtractor` so OpenAI and Gemini
    work with the same environment variables.
    """

    def __init__(self, extractor: Any = None):
        self._extractor = extractor

    @property
    def extractor(self) -> Any:
        """Lazily builds the shared extractor."""
        if self._extractor is None:
            from byconn.pipeline.llm_extractor import LLMExtractor

            self._extractor = LLMExtractor()
        return self._extractor

    @property
    def is_available(self) -> bool:
        """Reports whether a provider key is configured."""
        try:
            return bool(self.extractor.is_available)
        except Exception:
            return False

    async def plan(
        self,
        goal: str,
        nodes: Sequence[Dict[str, Any]],
        url: str = "",
        screenshot: Optional[bytes] = None,
    ) -> Dict[str, Any]:
        """Asks the model for the next action; falls back to ``stop`` on error.

        A screenshot is forwarded as a vision input when one is supplied and is
        small enough, letting the model judge what is actually on screen.
        """
        extractor = self.extractor
        if not extractor.is_available:
            return {"type": "stop"}

        image = screenshot if (screenshot and len(screenshot) <= MAX_IMAGE_BYTES) else None
        if screenshot and image is None:
            logger.debug(
                "Screenshot too large for vision input (%d bytes); using DOM only.",
                len(screenshot),
            )

        prompt = PLANNER_PROMPT.format(
            goal=goal or "(unspecified)",
            url=url or "(unknown)",
            vision_note=VISION_NOTE if image else NO_VISION_NOTE,
            nodes=_format_nodes(nodes),
        )

        raw_text = await extractor._call_with_retries(prompt, image)
        if raw_text is None:
            return {"type": "stop"}

        data = extractor._loads(raw_text)
        if isinstance(data, list) and data:
            data = data[0]
        if not isinstance(data, dict):
            # Some models wrap the action in {"action": {...}}.
            inner = data.get("action") if isinstance(data, dict) else None
            data = inner if isinstance(inner, dict) else data

        action = normalise_action(data, nodes)
        logger.debug("LLM planner chose %s", action.get("type"))
        return action


def build_planner(prefer_llm: bool = True, typed_value: str = "") -> Any:
    """Returns an :class:`LLMPlanner` when a key exists, else the heuristic."""
    if prefer_llm:
        planner = LLMPlanner()
        if planner.is_available:
            logger.info("Visual agent using the LLM planner.")
            return planner
        logger.warning("No LLM key configured; visual agent using the heuristic planner.")
    return HeuristicPlanner(typed_value=typed_value)


__all__ = ["LLMPlanner", "HeuristicPlanner", "build_planner", "normalise_action"]
