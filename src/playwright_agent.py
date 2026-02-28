import asyncio
import base64
from playwright.async_api import async_playwright
try:
    from playwright_stealth import stealth_async
except ImportError:
    async def stealth_async(page): pass
from loguru import logger


class BookingAgent:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.is_running = False
        self._latest_screenshot: bytes | None = None
        # CDP screencast
        self._cdp = None
        self._ws_send = None
        self._screencast_active = False

    # ── Screenshot fallback (HTTP /screenshot) ──────────────────────────────

    def get_screenshot(self) -> bytes | None:
        return self._latest_screenshot

    async def _snap(self):
        try:
            if self.page:
                self._latest_screenshot = await self.page.screenshot(type="png", full_page=False)
        except Exception:
            pass

    # ── CDP Screencast ──────────────────────────────────────────────────────

    async def start_screencast(self, ws_send_callback):
        """Start CDP screencast — pushes JPEG frames to ws_send_callback."""
        if not self.page:
            return
        self._ws_send = ws_send_callback
        self._cdp = await self.page.context.new_cdp_session(self.page)

        async def on_frame(params):
            session_id = params.get("sessionId")
            data = params.get("data", "")  # base64 JPEG
            # Acknowledge frame immediately (required by CDP)
            try:
                await self._cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
            except Exception:
                pass
            # Also cache as PNG fallback
            try:
                self._latest_screenshot = base64.b64decode(data)
            except Exception:
                pass
            # Push to WebSocket
            if self._ws_send:
                try:
                    await self._ws_send(data)
                except Exception:
                    pass

        self._cdp.on("Page.screencastFrame", on_frame)
        await self._cdp.send("Page.startScreencast", {
            "format": "jpeg",
            "quality": 60,
            "maxWidth": 1280,
            "maxHeight": 800,
            "everyNthFrame": 1,
        })
        self._screencast_active = True
        logger.info("CDP screencast started")

    async def stop_screencast(self):
        """Stop CDP screencast."""
        self._screencast_active = False
        if self._cdp:
            try:
                await self._cdp.send("Page.stopScreencast")
            except Exception:
                pass
            try:
                await self._cdp.detach()
            except Exception:
                pass
            self._cdp = None
        self._ws_send = None
        logger.info("CDP screencast stopped")

    # ── CDP Input Forwarding ────────────────────────────────────────────────

    async def cdp_click(self, x: int, y: int):
        if not self._cdp:
            return
        try:
            for etype in ["mousePressed", "mouseReleased"]:
                await self._cdp.send("Input.dispatchMouseEvent", {
                    "type": etype, "x": x, "y": y,
                    "button": "left", "clickCount": 1,
                })
        except Exception as e:
            logger.warning(f"cdp_click error: {e}")

    async def cdp_type(self, text: str):
        if not self._cdp:
            return
        try:
            for char in text:
                await self._cdp.send("Input.dispatchKeyEvent", {
                    "type": "keyDown", "text": char,
                })
                await self._cdp.send("Input.dispatchKeyEvent", {
                    "type": "keyUp",
                })
        except Exception as e:
            logger.warning(f"cdp_type error: {e}")

    async def cdp_keypress(self, key: str):
        if not self._cdp:
            return
        # Map browser key names to CDP key identifiers
        key_map = {
            "Enter": "\r", "Tab": "\t", "Backspace": "\b",
            "Escape": "\x1b", "Delete": "\x7f",
        }
        text = key_map.get(key, "")
        try:
            await self._cdp.send("Input.dispatchKeyEvent", {
                "type": "keyDown", "key": key, "code": key, "text": text,
            })
            await self._cdp.send("Input.dispatchKeyEvent", {
                "type": "keyUp", "key": key, "code": key,
            })
        except Exception as e:
            logger.warning(f"cdp_keypress error: {e}")

    async def cdp_scroll(self, x: int, y: int, dx: int, dy: int):
        if not self._cdp:
            return
        try:
            await self._cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseWheel", "x": x, "y": y,
                "deltaX": dx, "deltaY": dy,
            })
        except Exception as e:
            logger.warning(f"cdp_scroll error: {e}")

    async def cdp_mousemove(self, x: int, y: int):
        if not self._cdp:
            return
        try:
            await self._cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseMoved", "x": x, "y": y,
            })
        except Exception as e:
            logger.warning(f"cdp_mousemove error: {e}")

    # ── Browser lifecycle ───────────────────────────────────────────────────

    async def init_browser(self):
        logger.info("Initializing Playwright browser (headless)...")
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.context = await self.browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        self.page = await self.context.new_page()
        await stealth_async(self.page)
        self.is_running = True
        logger.info("Browser initialized.")

    async def _safe_close(self):
        await self.stop_screencast()
        self.is_running = False
        for obj, method in [
            (self.page, "close"), (self.context, "close"),
            (self.browser, "close"), (self.playwright, "stop"),
        ]:
            if obj:
                try:
                    await getattr(obj, method)()
                except Exception:
                    pass

    async def _dismiss_overlays(self):
        for sel in ["#onetrust-accept-btn-handler", 'button[data-testid="accept-cookies-button"]']:
            try:
                await self.page.click(sel, timeout=2000)
                logger.info(f"Accepted cookies via {sel}")
                await self.page.wait_for_timeout(500)
                break
            except Exception:
                pass
        for sel in [
            'button[aria-label="Dismiss sign-in info"]',
            'button[aria-label="Dismiss sign in interruption"]',
            '[data-testid="modal-close-button"]',
        ]:
            try:
                await self.page.click(sel, timeout=1500)
                logger.info(f"Dismissed modal via {sel}")
                await self.page.wait_for_timeout(300)
            except Exception:
                pass
        try:
            await self.page.wait_for_selector(".bbe73dce14", state="hidden", timeout=3000)
        except Exception:
            try:
                await self.page.evaluate(
                    "document.querySelectorAll('.bbe73dce14').forEach(el => el.style.display='none')"
                )
                logger.info("Removed .bbe73dce14 overlay via JS")
            except Exception:
                pass

    # ── Actions ─────────────────────────────────────────────────────────────

    async def execute_action(self, action: str, params: dict, session_id: str = "") -> dict:
        if not self.is_running:
            await self.init_browser()
        try:
            if action == "search_hotel":
                return await self.search_hotel(
                    destination=params.get("destination", ""),
                    checkin=params.get("checkin_date", ""),
                    checkout=params.get("checkout_date", ""),
                    adults=params.get("adults", 2),
                )
            else:
                return {"success": False, "error": f"Unknown action: {action}"}
        except Exception as e:
            logger.error(f"Error: {e}")
            await self._snap()
            return {"success": False, "error": str(e)}

    async def search_hotel(self, destination: str, checkin: str, checkout: str, adults: int) -> dict:
        logger.info(f"Searching: {destination!r}, {checkin} → {checkout}, {adults} adults")

        try:
            await self.page.goto("https://www.booking.com", timeout=60000)
            await self.page.wait_for_timeout(3000)
            await self._dismiss_overlays()

            # Click search input
            logger.info("Clicking search input...")
            try:
                await self.page.click('[name="ss"]', timeout=5000)
            except Exception:
                try:
                    await self.page.click('[name="ss"]', force=True, timeout=3000)
                except Exception:
                    await self.page.evaluate('document.querySelector("[name=ss]").click()')
            await self.page.wait_for_timeout(400)

            # Type destination
            logger.info(f"Typing: {destination!r}")
            await self.page.locator('[name="ss"]').fill("")
            await self.page.locator('[name="ss"]').type(destination, delay=80)
            await self.page.wait_for_timeout(1200)

            # Autocomplete
            try:
                await self.page.locator('[data-testid="autocomplete-results-list"] li').first.click(timeout=4000)
                logger.info("Selected autocomplete")
            except Exception:
                await self.page.keyboard.press("Enter")
            await self.page.wait_for_timeout(500)

            # Dates
            if checkin:
                try:
                    await self.page.click('[data-testid="searchbox-dates-container"]', timeout=3000)
                    await self.page.wait_for_timeout(500)
                    await self.page.click(f'[data-date="{checkin}"]', timeout=5000)
                    logger.info(f"Check-in: {checkin}")
                except Exception as e:
                    logger.warning(f"Check-in error: {e}")
            if checkout:
                try:
                    await self.page.click(f'[data-date="{checkout}"]', timeout=5000)
                    logger.info(f"Check-out: {checkout}")
                except Exception as e:
                    logger.warning(f"Check-out error: {e}")

            # Search
            logger.info("Clicking search...")
            try:
                await self.page.click('button[type="submit"]', timeout=5000)
            except Exception:
                await self.page.keyboard.press("Enter")
            await self.page.wait_for_timeout(1000)

            # Results
            logger.info("Waiting for results...")
            await self.page.wait_for_selector('[data-testid="property-card"]', timeout=25000)
            await self.page.wait_for_timeout(800)

            cards = self.page.locator('[data-testid="property-card"]')
            count = min(await cards.count(), 3)
            results = []
            for i in range(count):
                card = cards.nth(i)
                try:
                    name = (await card.locator('[data-testid="title"]').first.text_content() or "").strip()
                except Exception:
                    name = "Unknown"
                try:
                    price = (await card.locator('[data-testid="price-and-discounted-price"]').first.text_content() or "").strip()
                except Exception:
                    price = "N/A"
                try:
                    score = (await card.locator('[data-testid="review-score"]').first.text_content() or "").strip().replace("\n", " ")
                except Exception:
                    score = ""
                results.append(f"• {name} — {price}" + (f" | {score}" if score else ""))

            result_msg = f"Found {count} hotel(s) in {destination}:\n" + "\n".join(results)
            logger.info(f"Done: {result_msg[:100]}")
            return {"success": True, "result": result_msg}

        except Exception as e:
            await self._snap()
            raise Exception(f"Failed: {e}")

    async def close(self):
        await self._safe_close()


booking_agent = BookingAgent()
