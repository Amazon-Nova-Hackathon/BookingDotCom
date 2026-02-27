import asyncio
from playwright.async_api import async_playwright
from playwright_stealth import stealth
import json
from loguru import logger

class BookingAgent:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.is_running = False
        self._latest_screenshot: bytes | None = None  # latest PNG bytes

    def get_screenshot(self) -> bytes | None:
        """Return the most recent screenshot bytes (PNG), or None."""
        return self._latest_screenshot

    async def _snap(self):
        """Take a screenshot and cache it."""
        try:
            if self.page:
                self._latest_screenshot = await self.page.screenshot(type="png", full_page=False)
        except Exception:
            pass

    async def init_browser(self):
        logger.info("Initializing Playwright browser...")
        self.playwright = await async_playwright().start()
        # headless=True so the browser is invisible; screenshots are streamed to the UI
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        self.context = await self.browser.new_context(
            viewport={'width': 1280, 'height': 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        self.page = await self.context.new_page()
        await stealth(self.page)
        self.is_running = True
        logger.info("Browser initialized successfully.")

    async def _safe_close(self):
        self.is_running = False
        if self.page:
            try: await self.page.close()
            except: pass
        if self.context:
            try: await self.context.close()
            except: pass
        if self.browser:
            try: await self.browser.close()
            except: pass
        if self.playwright:
            try: await self.playwright.stop()
            except: pass

    async def execute_action(self, action: str, params: dict, session_id: str = "") -> dict:
        """Entry point called by the browser service"""
        if not self.is_running:
            await self.init_browser()
            
        try:
            if action == "search_hotel":
                return await self.search_hotel(
                    destination=params.get("destination", ""),
                    checkin=params.get("checkin_date", ""),
                    checkout=params.get("checkout_date", ""),
                    adults=params.get("adults", 2)
                )
            else:
                return {"success": False, "error": f"Unknown action: {action}"}
        except Exception as e:
            logger.error(f"Error during execution: {e}")
            await self.page.screenshot(path=f"error_{session_id}.png")
            return {"success": False, "error": str(e)}

    async def search_hotel(self, destination: str, checkin: str, checkout: str, adults: int) -> dict:
        logger.info(f"Searching hotel for destination: {destination}...")
        try:
            await self.page.goto("https://www.booking.com", timeout=60000)
            await self.page.wait_for_timeout(2000)
            await self._snap()  # screenshot: home page loaded
            
            # Dismiss sign-in popup if exists
            try:
                await self.page.click('button[aria-label="Dismiss sign-in info"]', timeout=3000)
                logger.info("Dismissed sign in info")
            except:
                pass
                
            # Dismiss cookie consent if exists
            try:
                await self.page.click('#onetrust-accept-btn-handler', timeout=2000)
                logger.info("Accepted cookies")
            except:
                pass

            await self._snap()  # screenshot: cookies dismissed

            logger.info("Filling destination...")
            search_input = self.page.locator('[name="ss"], [aria-autocomplete="list"]')
            await search_input.first.click()
            await search_input.first.fill("")
            await search_input.first.type(destination, delay=100)
            await self._snap()  # screenshot: destination typed
            await self.page.wait_for_timeout(1000)
            await self.page.keyboard.press("Enter")
            await self.page.keyboard.press("Escape")

            # Click search submit
            logger.info("Clicking search...")
            await self.page.wait_for_timeout(1000)
            await self._snap()  # screenshot: before search click
            await self.page.click('button[type="submit"]')

            # Wait for results
            logger.info("Waiting for property cards...")
            await self.page.wait_for_selector('[data-testid="property-card"]', timeout=20000)
            await self._snap()  # screenshot: results loaded
            
            # Extract first hotel details
            logger.info("Extracting first hotel details...")
            first_card = self.page.locator('[data-testid="property-card"]').first
            
            try:
                hotel_name = await first_card.locator('[data-testid="title"]').first.text_content()
            except:
                hotel_name = "Unknown Hotel"
                
            try:
                hotel_price = await first_card.locator('[data-testid="price-and-discounted-price"]').first.text_content()
            except:
                hotel_price = "Unknown Price"

            result_msg = f"Found hotel: {hotel_name.strip()} at {hotel_price.strip()}."
            logger.info(f"Success: {result_msg}")
            
            return {
                "success": True, 
                "result": result_msg,
                "data": {
                    "hotel_name": hotel_name.strip(),
                    "hotel_price": hotel_price.strip()
                }
            }
            
        except Exception as e:
            await self._snap()  # screenshot on error too
            raise Exception(f"Failed to search on booking.com: {str(e)}")

# Create a singleton instance
booking_agent = BookingAgent()
