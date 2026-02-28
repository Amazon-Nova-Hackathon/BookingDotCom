# -*- coding: utf-8 -*-
"""
Browser Agent - Booking.com Automation
Uses browser-use with AWS Bedrock and Playwright
to automate hotel search on Booking.com with live screenshot streaming.
"""
import asyncio
import base64
import os
import sys
import traceback

from dotenv import load_dotenv
from loguru import logger

load_dotenv(override=True)

# Add sibling browser-use clone to sys.path so we can import it directly
_browser_use_path = os.path.join(os.path.dirname(__file__), "..", "..", "browser-use")
if os.path.isdir(_browser_use_path) and _browser_use_path not in sys.path:
    sys.path.insert(0, os.path.abspath(_browser_use_path))

from browser_use import Agent, BrowserSession, BrowserProfile
from browser_use.llm.aws import ChatAWSBedrock


class BrowserAgentHandler:
    """
    Manages a browser-use Agent that autonomously navigates Booking.com.
    Creates a fresh BrowserSession per task (Agent kills sessions after run).
    Caches the latest screenshot (PNG bytes) so the frontend can poll it.
    """

    def __init__(self):
        self.llm: ChatAWSBedrock | None = None
        self._latest_screenshot: bytes | None = None
        self._screenshot_task: asyncio.Task | None = None
        self._current_session: BrowserSession | None = None
        self._llm_ready = False

    # ── public API ──────────────────────────────────────────────────────────

    def get_screenshot(self) -> bytes | None:
        """Return the most recent screenshot PNG bytes, or None."""
        return self._latest_screenshot

    async def execute_action(self, action: str, params: dict, session_id: str = "") -> dict:
        """Entry point called by main_browser_service."""
        self._ensure_llm()
        try:
            if action == "search_hotel":
                return await self._run_agent_task(params, session_id)
            else:
                return {"success": False, "error": f"Unknown action: {action}"}
        except Exception as e:
            logger.error(f"execute_action error: {e}\n{traceback.format_exc()}")
            return {"success": False, "error": str(e)}

    # ── internals ───────────────────────────────────────────────────────────

    def _ensure_llm(self):
        """One-time LLM init from .env vars."""
        if self._llm_ready:
            return

        logger.info("Initializing ChatAWSBedrock LLM …")
        self.llm = ChatAWSBedrock(
            model=os.getenv("BEDROCK_MODEL_ID", "global.anthropic.claude-sonnet-4-6"),
            aws_access_key_id=os.getenv("BEDROCK_ACCESS_KEY_ID", os.getenv("AWS_ACCESS_KEY_ID")),
            aws_secret_access_key=os.getenv("BEDROCK_SECRET_ACCESS_KEY", os.getenv("AWS_SECRET_ACCESS_KEY")),
            aws_region=os.getenv("BEDROCK_REGION", "ap-southeast-1"),
            aws_session_token=os.getenv("BEDROCK_SESSION_TOKEN", os.getenv("AWS_SESSION_TOKEN")),
            max_tokens=4096,
        )
        self._llm_ready = True
        logger.info("LLM ready.")

    def _create_session(self) -> BrowserSession:
        """Create a fresh BrowserSession each time (Agent kills it after run)."""
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

    async def _on_step(self, browser_state, model_output, step_number):
        """Callback fired after every Agent LLM step — grab screenshot from state."""
        try:
            if browser_state and browser_state.screenshot:
                self._latest_screenshot = base64.b64decode(browser_state.screenshot)
                logger.info(f"Step {step_number}: screenshot cached ({len(self._latest_screenshot)} bytes)")
        except Exception as e:
            logger.warning(f"Step callback screenshot error: {e}")

    async def _run_agent_task(self, params: dict, session_id: str) -> dict:
        """Build a task string from params and let browser-use Agent solve it."""
        destination = params.get("destination", "")
        checkin = params.get("checkin_date", "")
        checkout = params.get("checkout_date", "")
        adults = params.get("adults", 2)

        task = (
            f"Go to https://www.booking.com and search for hotels.\n"
            f"Destination: {destination}\n"
        )
        if checkin:
            task += f"Check-in date: {checkin}\n"
        if checkout:
            task += f"Check-out date: {checkout}\n"
        task += (
            f"Number of adults: {adults}\n"
            f"After the search results load, extract the name and price of the first hotel "
            f"and return them."
        )

        logger.info(f"[{session_id}] Running browser-use agent: {task[:120]}…")

        # Fresh session per task — Agent kills it when done
        session = self._create_session()
        self._current_session = session
        self._latest_screenshot = None

        # Start background screenshot polling for this session
        screenshot_task = asyncio.create_task(self._screenshot_loop(session))

        try:
            agent = Agent(
                task=task,
                llm=self.llm,
                browser_session=session,
                register_new_step_callback=self._on_step,
                use_vision=True,
                max_actions_per_step=3,
                max_failures=3,
            )

            result = await agent.run(max_steps=15)

            # Extract final result text
            final_text = result.final_result() if result.final_result() else "Search completed but no result text."
            logger.info(f"[{session_id}] Agent finished: {final_text[:200]}")

            return {"success": True, "result": final_text}
        finally:
            screenshot_task.cancel()
            try:
                await screenshot_task
            except asyncio.CancelledError:
                pass
            self._current_session = None

    async def close(self):
        """Cleanup."""
        if self._current_session:
            try:
                await self._current_session.kill()
            except Exception:
                pass


# Singleton
booking_agent = BrowserAgentHandler()
