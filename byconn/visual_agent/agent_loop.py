import logging
import asyncio
from typing import Dict, Any, List
from playwright.async_api import Page
from .dom_parser import DOMParser

logger = logging.getLogger("byconnx.visual_agent.agent_loop")

class VisualBrowserAgent:
    """
    AI Browser Agent that iteratively drives web actions.
    Uses visual screenshot captures and DOM representations to run clicks, typing, and page traversal.
    """
    def __init__(self, page: Page, llm_client: Any):
        self.page = page
        self.llm_client = llm_client
        self.dom_parser = DOMParser()

    async def execute_task(self, prompt: str, max_steps: int = 10) -> bool:
        """Executes browser interactions step-by-step to achieve the goal."""
        logger.info(f"Visual Agent starting execution of goal: '{prompt}'")
        
        for step in range(max_steps):
            logger.info(f"--- Step {step + 1}/{max_steps} ---")
            
            # 1. Take a screenshot for Vision model consumption
            screenshot_bytes = await self.page.screenshot(type="png")
            
            # 2. Parse the interactable visual nodes
            nodes = await self.dom_parser.get_interactables(self.page)
            
            # 3. Request LLM decision using both screen and node coordinates
            action = await self._decide_action(prompt, nodes, screenshot_bytes)
            logger.info(f"Agent decided action: {action}")
            
            if action.get("type") == "stop":
                logger.info("Goal reached or agent requested completion.")
                return True
                
            # 4. Perform the decided action
            await self._run_action(action)
            await asyncio.sleep(1.5)  # Wait for page layout re-renders
            
        logger.error("Reached maximum steps without fully executing agent task.")
        return False

    async def _decide_action(self, prompt: str, nodes: List[Dict[str, Any]], screenshot: bytes) -> Dict[str, Any]:
        """
        Mock of LLM call. In production, this packages the screenshot as base64
        alongside the list of interactable nodes, prompts the model, and parses a JSON response.
        """
        # Simplistic heuristic / Mock behavior for the skeleton
        # If target search field is present, type. If submit exists, click.
        for node in nodes:
            if "search" in node["text"].lower() or "input" in node["role"]:
                return {
                    "type": "type",
                    "x": node["x"],
                    "y": node["y"],
                    "value": "Byconn-X Data Engine GitHub"
                }
            if "submit" in node["text"].lower() or "enter" in node["text"].lower():
                return {
                    "type": "click",
                    "x": node["x"],
                    "y": node["y"]
                }
        
        # Stop fallback if no immediate actions match
        return {"type": "stop"}

    async def _run_action(self, action: Dict[str, Any]):
        """Executes precise coordinates-based mouse clicks and keyboard actions."""
        action_type = action.get("type")
        x, y = action.get("x", 0), action.get("y", 0)

        if action_type == "click":
            logger.info(f"Clicking coordinate: ({x}, {y})")
            await self.page.mouse.click(x, y)
        elif action_type == "type":
            val = action.get("value", "")
            logger.info(f"Clicking coordinate ({x}, {y}) and typing: '{val}'")
            await self.page.mouse.click(x, y)
            await self.page.keyboard.type(val)
            await self.page.keyboard.press("Enter")
        elif action_type == "hover":
            logger.info(f"Hovering over coordinate: ({x}, {y})")
            await self.page.mouse.move(x, y)
        else:
            logger.warning(f"Unrecognized action skipped: {action_type}")
class VisualAgent:
    """
    User-facing facade over VisualBrowserAgent.
    Manages the Playwright browser lifecycle so callers don't have to.

    Usage:
        agent = VisualAgent(model="gpt-4o")
        await agent.navigate("https://example.com")
        await agent.act("Find the login button and click it")
    """
    def __init__(self, model: str = "gpt-4o", headless: bool = True):
        self.model = model
        self.headless = headless
        self._playwright = None
        self._browser = None
        self._page = None
        self._agent = None

    async def _ensure_browser(self):
        """Lazily initialises the Playwright browser on first use."""
        if self._page is None:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=self.headless)
            self._page = await self._browser.new_page()
            # Stub LLM client — replace with openai.AsyncOpenAI() when key is set
            self._agent = VisualBrowserAgent(page=self._page, llm_client=None)

    async def navigate(self, url: str):
        """Navigates the AI-controlled browser to the given URL."""
        await self._ensure_browser()
        await self._page.goto(url, wait_until="domcontentloaded")
        logger.info(f"VisualAgent navigated to: {url}")

    async def act(self, prompt: str, max_steps: int = 10) -> bool:
        """Instructs the AI agent to achieve the described goal on the current page."""
        await self._ensure_browser()
        return await self._agent.execute_task(prompt=prompt, max_steps=max_steps)

    async def close(self):
        """Releases the browser resources."""
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("VisualAgent browser closed.")
