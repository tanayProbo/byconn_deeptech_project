import logging
import asyncio
from typing import Any, Callable, Dict, List, Optional
from playwright.async_api import Page
from .dom_parser import DOMParser
from .planner import build_planner

logger = logging.getLogger("byconnx.visual_agent.agent_loop")

# Pause between actions so the page can re-render before the next observation.
STEP_DELAY_SECONDS = 1.5
# JPEG at reduced quality keeps screenshots small enough to be vision input.
SCREENSHOT_TYPE = "jpeg"
SCREENSHOT_QUALITY = 60


class VisualBrowserAgent:
    """
    AI browser agent that iteratively drives web actions.

    Each step captures a screenshot and the interactable DOM nodes, asks a
    planner for the next action, and performs it. When an LLM key is
    configured the planner is model-driven; otherwise a deterministic
    heuristic planner is used, so the agent always has a behaviour.
    """

    def __init__(
        self,
        page: Page,
        llm_client: Any = None,
        planner: Optional[Any] = None,
        step_delay: float = STEP_DELAY_SECONDS,
        on_step: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.page = page
        self.llm_client = llm_client
        self.dom_parser = DOMParser()
        self.planner = planner if planner is not None else build_planner()
        self.step_delay = step_delay
        # Called with each history record as it is taken, for live progress.
        self.on_step = on_step
        # Observation/action trace, useful for debugging and for API responses.
        self.history: List[Dict[str, Any]] = []
        # Why the last run ended without reaching its goal, if it did.
        self.failure_reason: Optional[str] = None

    async def execute_task(self, prompt: str, max_steps: int = 10) -> bool:
        """Executes browser interactions step-by-step to achieve the goal.

        Returns ``True`` only when the planner deliberately chose ``stop``.
        A planner error, an unusable model reply, an impossible action or an
        exhausted step budget all return ``False`` with ``failure_reason``
        set: a stop caused by a failure must never be reported as success.
        """
        logger.info(f"Visual Agent starting execution of goal: '{prompt}'")
        self.failure_reason = None

        for step in range(max_steps):
            logger.info(f"--- Step {step + 1}/{max_steps} ---")

            # 1. Capture a screenshot so a vision-capable planner can judge what
            #    is actually rendered. A capture failure is not fatal: the
            #    planner falls back to reasoning over the DOM node list.
            screenshot_bytes = b""
            try:
                screenshot_bytes = await self.page.screenshot(
                    type=SCREENSHOT_TYPE, quality=SCREENSHOT_QUALITY
                )
            except Exception as exc:
                logger.debug("screenshot capture failed: %s", exc)

            # 2. Parse the interactable visual nodes.
            nodes = await self.dom_parser.get_interactables(self.page)

            # 3. Ask the planner for the next action, passing the screenshot.
            try:
                action = await self.planner.plan(
                    prompt, nodes, url=self.page.url, screenshot=screenshot_bytes
                )
            except TypeError:
                # Planners written before the screenshot argument.
                action = await self.planner.plan(prompt, nodes, url=self.page.url)
            except Exception as exc:
                logger.error("planner failed: %s", exc)
                action = {"type": "stop", "error": f"planner failed: {exc}"}
            logger.info(f"Agent decided action: {action}")

            self.history.append({
                "step": step + 1,
                "screenshot_bytes": len(screenshot_bytes),
                "node_count": len(nodes),
                "action": action,
            })
            if self.on_step is not None:
                try:
                    self.on_step(self.history[-1])
                except Exception as exc:  # observers must never break the task
                    logger.debug("on_step callback failed: %s", exc)

            if action.get("type") == "stop":
                if action.get("error"):
                    self.failure_reason = str(action["error"])
                    logger.error("Agent stopped on error: %s", self.failure_reason)
                    return False
                logger.info("Goal reached or agent requested completion.")
                return True

            # A page with no interactable nodes cannot be acted on. Without this
            # guard a planner that hallucinates a coordinate (typically the
            # origin) has every remaining step "succeed" at clicking nothing, so
            # the run burns its whole budget and reports a misleading failure.
            if not nodes and action.get("type") == "click" and action.get("element_id") is None:
                logger.warning(
                    "No interactable elements on %s and the planner proposed a "
                    "coordinate click (%s, %s); ending the task instead of "
                    "clicking an empty page.",
                    getattr(self.page, "url", "?"),
                    action.get("x"),
                    action.get("y"),
                )
                self.failure_reason = "no interactable elements to act on"
                return False

            # 4. Perform the decided action.
            performed = await self._run_action(action)
            if not performed:
                logger.info("Action could not be performed; ending the task.")
                self.failure_reason = f"could not perform action {action!r}"
                return False
            await asyncio.sleep(self.step_delay)  # wait for layout to re-render

        logger.error("Reached maximum steps without fully executing agent task.")
        self.failure_reason = f"step budget of {max_steps} exhausted"
        return False

    async def _decide_action(
        self, prompt: str, nodes: List[Dict[str, Any]], screenshot: bytes
    ) -> Dict[str, Any]:
        """Backwards-compatible wrapper around the configured planner."""
        return await self.planner.plan(
            prompt, nodes, url=getattr(self.page, "url", ""), screenshot=screenshot
        )

    async def _run_action(self, action: Dict[str, Any]) -> bool:
        """Performs mouse/keyboard actions using element coordinates.

        Returns ``False`` when the action was malformed, so the caller can end
        the task instead of spinning on an impossible step.
        """
        action_type = action.get("type")
        x, y = action.get("x"), action.get("y")

        if action_type == "click":
            if x is None or y is None:
                logger.warning("Click without coordinates; skipping.")
                return False
            logger.info(f"Clicking coordinate: ({x}, {y})")
            await self.page.mouse.click(x, y)
            return True

        if action_type == "type":
            if x is None or y is None:
                logger.warning("Type without coordinates; skipping.")
                return False
            value = action.get("value", "")
            logger.info(f"Clicking coordinate ({x}, {y}) and typing: '{value}'")
            await self.page.mouse.click(x, y)
            if value:
                await self.page.keyboard.type(str(value))
            await self.page.keyboard.press("Enter")
            return True

        if action_type == "hover":
            if x is None or y is None:
                logger.warning("Hover without coordinates; skipping.")
                return False
            logger.info(f"Hovering over coordinate: ({x}, {y})")
            await self.page.mouse.move(x, y)
            return True

        if action_type == "scroll":
            delta = int(action.get("delta", 600))
            logger.info(f"Scrolling page by {delta}px")
            await self.page.mouse.wheel(0, delta)
            return True

        logger.warning(f"Unrecognized action skipped: {action_type}")
        return False
