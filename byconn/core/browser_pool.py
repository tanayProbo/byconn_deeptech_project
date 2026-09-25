import logging
import random
from typing import Dict, Any, Optional
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

logger = logging.getLogger("byconnx.core.browser_pool")

class BrowserPool:
    """
    Manages Playwright browser instances, context options, fingerprint spoofing,
    and proxy rotations (similar to Crawlee's BrowserPool).
    """
    def __init__(self, proxy_list: Optional[list] = None, headless: bool = True):
        self.proxy_list = proxy_list or []
        self.headless = headless
        self.playwright = None
        self.browser: Optional[Browser] = None

    async def initialize(self):
        """Initializes the playwright browser controller."""
        if not self.playwright:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=self.headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-infobars"
                ]
            )
            logger.info("Playwright Chromium browser pool initialized successfully.")

    def _get_random_proxy(self) -> Optional[Dict[str, str]]:
        """Selects a random proxy from the available list."""
        if not self.proxy_list:
            return None
        proxy = random.choice(self.proxy_list)
        return {
            "server": proxy.get("server"),
            "username": proxy.get("username", ""),
            "password": proxy.get("password", "")
        }

    def _generate_fingerprint(self) -> Dict[str, Any]:
        """Spoofs browser fingerprints to look like authentic human traffic."""
        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ]
        return {
            "user_agent": random.choice(user_agents),
            "viewport": {"width": 1920, "height": 1080},
            "device_scale_factor": 1,
            "is_mobile": False,
            "has_touch": False,
            "locale": "en-US",
            "timezone_id": "America/New_York"
        }

    async def new_context(self) -> BrowserContext:
        """Creates a customized browser context with fingerprint & rotating proxy details."""
        if not self.browser:
            await self.initialize()
        
        fingerprint = self._generate_fingerprint()
        proxy = self._get_random_proxy()

        context = await self.browser.new_context(
            user_agent=fingerprint["user_agent"],
            viewport=fingerprint["viewport"],
            device_scale_factor=fingerprint["device_scale_factor"],
            is_mobile=fingerprint["is_mobile"],
            has_touch=fingerprint["has_touch"],
            locale=fingerprint["locale"],
            timezone_id=fingerprint["timezone_id"],
            proxy=proxy,
            ignore_https_errors=True
        )
        
        # Add stealth scripts to bypass automated detection (e.g. navigator.webdriver)
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)
        return context

    async def close(self):
        """Closes the browser pool resource."""
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        logger.info("Browser pool shut down.")
