# -*- coding: utf-8 -*-
"""
Browser Agent - Booking.com Automation
Uses browser-use BrowserSession (Playwright) for live browser view,
with direct Playwright automation for fast hotel search.
"""
import asyncio
import base64
import os
import re
import sys
import traceback

from dotenv import load_dotenv
from loguru import logger

load_dotenv(override=True)

# Add sibling browser-use clone to sys.path so we can import it directly
_browser_use_path = os.path.join(os.path.dirname(__file__), "..", "..", "browser-use")
if os.path.isdir(_browser_use_path) and _browser_use_path not in sys.path:
    sys.path.insert(0, os.path.abspath(_browser_use_path))

from browser_use import BrowserSession, BrowserProfile


class BrowserAgentHandler:
    """
    Manages a BrowserSession and drives Playwright directly for fast hotel search.
    Caches the latest screenshot (PNG bytes) so the frontend can poll it.
    """

    def __init__(self):
        self._latest_screenshot: bytes | None = None
        self._screenshot_task: asyncio.Task | None = None
        self._current_session: BrowserSession | None = None

    # ── public API ──────────────────────────────────────────────────────────

    def get_screenshot(self) -> bytes | None:
        """Return the most recent screenshot PNG bytes, or None."""
        return self._latest_screenshot

    async def execute_action(self, action: str, params: dict, session_id: str = "") -> dict:
        """Entry point called by main_browser_service."""
        try:
            if action == "search_hotel":
                return await self._run_direct_search(params, session_id)
            else:
                return {"success": False, "error": f"Unknown action: {action}"}
        except Exception as e:
            logger.error(f"execute_action error: {e}\n{traceback.format_exc()}")
            return {"success": False, "error": str(e)}

    # ── internals ───────────────────────────────────────────────────────────

    def _create_session(self) -> BrowserSession:
        """Create a fresh BrowserSession each time."""
        profile = BrowserProfile(
            headless=True,
            viewport={"width": 1280, "height": 800},
            disable_security=True,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        return BrowserSession(browser_profile=profile)

    async def _screenshot_loop(self, session: BrowserSession):
        """Continuously grab screenshots while the session is alive."""
        while True:
            try:
                if session.is_cdp_connected:
                    png = await session.take_screenshot()
                    if png:
                        self._latest_screenshot = png
            except asyncio.CancelledError:
                break
            except Exception:
                pass  # page may not be ready yet
            await asyncio.sleep(0.4)

    async def _run_direct_search(self, params: dict, session_id: str) -> dict:
        """
        Navigate directly to Booking.com search results URL and extract hotel data.
        Uses browser-use CDP Page API (NOT Playwright).
        """
        import json as _json
        from urllib.parse import quote_plus

        destination = params.get("destination", "")
        checkin = params.get("checkin_date", "")
        checkout = params.get("checkout_date", "")
        adults = params.get("adults", 2)

        logger.info(f"[{session_id}] Direct search: dest={destination}, "
                     f"checkin={checkin}, checkout={checkout}, adults={adults}")

        # Kill previous session if any
        if self._current_session:
            try:
                await self._current_session.kill()
            except Exception:
                pass

        # Fresh session per task
        session = self._create_session()
        self._current_session = session
        self._latest_screenshot = None

        # Start background screenshot polling
        screenshot_task = asyncio.create_task(self._screenshot_loop(session))

        try:
            # Start the browser session
            await session.start()

            page = await session.get_current_page()
            if not page:
                return {"success": False, "error": "Failed to get browser page"}

            # ── Step 1: Navigate directly to search results URL ──
            encoded_dest = quote_plus(destination)
            search_url = (
                f"https://www.booking.com/searchresults.html"
                f"?ss={encoded_dest}"
                f"&checkin={checkin}"
                f"&checkout={checkout}"
                f"&group_adults={adults}"
                f"&no_rooms=1"
                f"&selected_currency=USD"
            )
            logger.info(f"[{session_id}] Navigating to: {search_url}")
            await page.goto(search_url)

            # Wait for page to load (no wait_for_selector in CDP API)
            logger.info(f"[{session_id}] Waiting for search results to load...")
            await asyncio.sleep(8)

            # ── Step 2: Dismiss any popups via JS ──
            try:
                await page.evaluate("""
                    () => {
                        const dismiss = document.querySelector('[aria-label="Dismiss sign-in info."]');
                        if (dismiss) dismiss.click();
                        const cookie = document.querySelector('#onetrust-accept-btn-handler');
                        if (cookie) cookie.click();
                    }
                """)
                await asyncio.sleep(1)
            except Exception:
                pass

            # ── Step 3: Poll for property cards (up to 20s) ──
            hotel_data = None
            for attempt in range(10):
                logger.info(f"[{session_id}] Extraction attempt {attempt + 1}...")
                raw = await page.evaluate("""
                    () => {
                        const hotels = [];
                        const cards = document.querySelectorAll('[data-testid="property-card"]');
                        for (let i = 0; i < Math.min(cards.length, 5); i++) {
                            const card = cards[i];
                            const nameEl = card.querySelector('[data-testid="title"]');
                            const priceEl = card.querySelector('[data-testid="price-and-discounted-price"]');
                            const ratingEl = card.querySelector('[data-testid="review-score"]');
                            const name = nameEl ? nameEl.textContent.trim() : 'Unknown';
                            const price = priceEl ? priceEl.textContent.trim() : 'Price not shown';
                            const rating = ratingEl ? ratingEl.textContent.trim() : '';
                            hotels.push({ name, price, rating });
                        }
                        return JSON.stringify(hotels);
                    }
                """)
                try:
                    parsed = _json.loads(raw) if isinstance(raw, str) else raw
                    if parsed and len(parsed) > 0:
                        hotel_data = parsed
                        break
                except Exception:
                    pass
                await asyncio.sleep(2)

            if hotel_data and len(hotel_data) > 0:
                lines = []
                for i, h in enumerate(hotel_data, 1):
                    line = f"{i}. {h['name']}"
                    if h.get('rating'):
                        line += f" ({h['rating']})"
                    line += f" - {h['price']}"
                    lines.append(line)

                result_text = "Here are the top hotels I found:\n" + "\n".join(lines)
                logger.info(f"[{session_id}] Found {len(hotel_data)} hotels")
                return {"success": True, "result": result_text}
            else:
                # Fallback: try getting any title elements
                raw_titles = await page.evaluate("""
                    () => {
                        const els = document.querySelectorAll('[data-testid="title"]');
                        return JSON.stringify(Array.from(els).slice(0, 5).map(e => e.textContent.trim()));
                    }
                """)
                try:
                    titles = _json.loads(raw_titles) if isinstance(raw_titles, str) else raw_titles
                    if titles and len(titles) > 0:
                        result_text = "Hotels found: " + ", ".join(titles)
                        return {"success": True, "result": result_text}
                except Exception:
                    pass

                # Last resort: get page title to debug
                try:
                    title = await page.get_title()
                    url = await page.get_url()
                    logger.warning(f"[{session_id}] No hotels extracted. Page: {title} | URL: {url}")
                except Exception:
                    pass

                return {"success": True, "result": "Search completed. Please check the browser view for results."}

        except Exception as e:
            logger.error(f"[{session_id}] Direct search error: {e}\n{traceback.format_exc()}")
            return {"success": False, "error": str(e)}
        finally:
            screenshot_task.cancel()
            try:
                await screenshot_task
            except asyncio.CancelledError:
                pass
            # Keep session alive for user interaction (don't kill it)

    # ── User interaction forwarding ────────────────────────────────────────

    async def user_click(self, x: int, y: int) -> bool:
        """Forward a user click to the current browser page."""
        try:
            session = self._current_session
            if session and session.is_cdp_connected:
                page = await session.get_current_page()
                if page:
                    mouse = await page.mouse
                    await mouse.click(x, y)
                    return True
        except Exception as e:
            logger.warning(f"user_click error: {e}")
        return False

    async def user_scroll(self, x: int, y: int, delta_x: int, delta_y: int) -> bool:
        """Forward a scroll event to the current browser page."""
        try:
            session = self._current_session
            if session and session.is_cdp_connected:
                page = await session.get_current_page()
                if page:
                    mouse = await page.mouse
                    await mouse.scroll(x=x, y=y, delta_x=delta_x, delta_y=delta_y)
                    return True
        except Exception as e:
            logger.warning(f"user_scroll error: {e}")
        return False

    async def user_type(self, text: str) -> bool:
        """Forward keyboard input to the current browser page."""
        try:
            session = self._current_session
            if session and session.is_cdp_connected:
                page = await session.get_current_page()
                if page:
                    # Type each character as a key press
                    for ch in text:
                        await page.press(ch)
                    return True
        except Exception as e:
            logger.warning(f"user_type error: {e}")
        return False

    async def user_keypress(self, key: str) -> bool:
        """Forward a single key press (Enter, Tab, Escape, etc.)."""
        try:
            session = self._current_session
            if session and session.is_cdp_connected:
                page = await session.get_current_page()
                if page:
                    await page.press(key)
                    return True
        except Exception as e:
            logger.warning(f"user_keypress error: {e}")
        return False

    async def close(self):
        """Cleanup."""
        if self._current_session:
            try:
                await self._current_session.kill()
            except Exception:
                pass


# Singleton
booking_agent = BrowserAgentHandler()
