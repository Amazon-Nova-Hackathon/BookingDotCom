import asyncio
import base64
from urllib.parse import quote_plus

from loguru import logger
from playwright.async_api import async_playwright

try:
    from playwright_stealth import stealth_async
except ImportError:
    async def stealth_async(page):
        return None


class BookingAgent:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.is_running = False
        self._latest_screenshot: bytes | None = None
        self._cdp = None
        self._ws_send = None
        self._screencast_active = False
        self._action_lock = asyncio.Lock()

    def get_screenshot(self) -> bytes | None:
        return self._latest_screenshot

    async def _snap(self):
        try:
            if self.page:
                self._latest_screenshot = await self.page.screenshot(type="png", full_page=False)
        except Exception:
            pass

    async def _wait_brief_navigation(self, timeout: int = 5000):
        if not self.page:
            return
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=timeout)
        except Exception:
            pass

    async def _ensure_booking_session_page(self):
        if not self.page:
            return

        current_url = self.page.url or ""
        if current_url and current_url != "about:blank":
            return

        logger.info("Opening Booking.com in the current session...")
        await self.page.goto("https://www.booking.com", timeout=60000)
        await self._wait_brief_navigation()
        await self.page.wait_for_timeout(500)
        await self._dismiss_overlays()

    async def start_screencast(self, ws_send_callback):
        if not self.page:
            return

        await self._ensure_booking_session_page()
        self._ws_send = ws_send_callback
        self._cdp = await self.page.context.new_cdp_session(self.page)

        async def on_frame(params):
            session_id = params.get("sessionId")
            data = params.get("data", "")
            try:
                await self._cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
            except Exception:
                pass
            try:
                self._latest_screenshot = base64.b64decode(data)
            except Exception:
                pass
            if self._ws_send:
                try:
                    await self._ws_send(data)
                except Exception:
                    pass

        self._cdp.on("Page.screencastFrame", on_frame)
        await self._cdp.send(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": 60,
                "maxWidth": 1280,
                "maxHeight": 800,
                "everyNthFrame": 1,
            },
        )
        self._screencast_active = True
        logger.info("CDP screencast started")

    async def stop_screencast(self):
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

    async def cdp_click(self, x: int, y: int):
        if not self._cdp:
            return
        try:
            for event_type in ("mousePressed", "mouseReleased"):
                await self._cdp.send(
                    "Input.dispatchMouseEvent",
                    {
                        "type": event_type,
                        "x": x,
                        "y": y,
                        "button": "left",
                        "clickCount": 1,
                    },
                )
        except Exception as exc:
            logger.warning(f"cdp_click error: {exc}")

    async def cdp_type(self, text: str):
        if not self._cdp:
            return
        try:
            await self._cdp.send("Input.insertText", {"text": text})
        except Exception as exc:
            logger.warning(f"cdp_type error: {exc}")

    async def cdp_keypress(self, key: str):
        if not self._cdp:
            return

        key_defs = {
            "Backspace": {"code": "Backspace", "keyCode": 8, "text": ""},
            "Delete": {"code": "Delete", "keyCode": 46, "text": ""},
            "Enter": {"code": "Enter", "keyCode": 13, "text": "\r"},
            "Tab": {"code": "Tab", "keyCode": 9, "text": ""},
            "Escape": {"code": "Escape", "keyCode": 27, "text": ""},
            "ArrowUp": {"code": "ArrowUp", "keyCode": 38, "text": ""},
            "ArrowDown": {"code": "ArrowDown", "keyCode": 40, "text": ""},
            "ArrowLeft": {"code": "ArrowLeft", "keyCode": 37, "text": ""},
            "ArrowRight": {"code": "ArrowRight", "keyCode": 39, "text": ""},
            "Home": {"code": "Home", "keyCode": 36, "text": ""},
            "End": {"code": "End", "keyCode": 35, "text": ""},
            "PageUp": {"code": "PageUp", "keyCode": 33, "text": ""},
            "PageDown": {"code": "PageDown", "keyCode": 34, "text": ""},
        }
        definition = key_defs.get(key, {"code": key, "keyCode": 0, "text": ""})
        try:
            await self._cdp.send(
                "Input.dispatchKeyEvent",
                {
                    "type": "keyDown",
                    "key": key,
                    "code": definition["code"],
                    "text": definition["text"],
                    "windowsVirtualKeyCode": definition["keyCode"],
                    "nativeVirtualKeyCode": definition["keyCode"],
                },
            )
            await self._cdp.send(
                "Input.dispatchKeyEvent",
                {
                    "type": "keyUp",
                    "key": key,
                    "code": definition["code"],
                    "windowsVirtualKeyCode": definition["keyCode"],
                    "nativeVirtualKeyCode": definition["keyCode"],
                },
            )
        except Exception as exc:
            logger.warning(f"cdp_keypress error: {exc}")

    async def cdp_scroll(self, x: int, y: int, dx: int, dy: int):
        if not self._cdp:
            return
        try:
            await self._cdp.send(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseWheel",
                    "x": x,
                    "y": y,
                    "deltaX": dx,
                    "deltaY": dy,
                },
            )
        except Exception as exc:
            logger.warning(f"cdp_scroll error: {exc}")

    async def cdp_mousemove(self, x: int, y: int):
        if not self._cdp:
            return
        try:
            await self._cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        except Exception as exc:
            logger.warning(f"cdp_mousemove error: {exc}")

    async def _click_first_visible(self, selectors: list[str], timeout: int = 1200) -> bool:
        for selector in selectors:
            try:
                locator = self.page.locator(selector).first
                await locator.scroll_into_view_if_needed(timeout=timeout)
                await locator.click(timeout=timeout)
                logger.info(f"Clicked selector: {selector}")
                return True
            except Exception:
                continue
        return False

    @staticmethod
    def _clean_text(value: str) -> str:
        return " ".join((value or "").split()).strip()

    async def _get_first_text(self, selectors: list[str], timeout: int = 1200) -> str:
        for selector in selectors:
            try:
                text = await self.page.locator(selector).first.text_content(timeout=timeout)
                text = self._clean_text(text or "")
                if text:
                    return text
            except Exception:
                continue
        return ""

    async def _get_text_list(self, selectors: list[str], limit: int = 4) -> list[str]:
        values: list[str] = []
        for selector in selectors:
            try:
                locator = self.page.locator(selector)
                count = min(await locator.count(), limit)
                for index in range(count):
                    text = await locator.nth(index).text_content(timeout=1000)
                    text = self._clean_text(text or "")
                    if text and text not in values:
                        values.append(text)
                    if len(values) >= limit:
                        return values
            except Exception:
                continue
        return values

    async def _summarize_selected_hotel(self, fallback_name: str) -> str:
        title = await self._get_first_text([
            'h2[data-testid="title"]',
            '[data-testid="title"]',
            "#hp_hotel_name h2",
            "h1",
            "h2",
        ])
        address = await self._get_first_text([
            '[data-testid="address"]',
            ".hp_address_subtitle",
        ])
        score = await self._get_first_text([
            '[data-testid="review-score-component"]',
            '[data-testid="review-score"]',
        ])
        price = await self._get_first_text([
            '[data-testid="price-for-x-nights"]',
            '[data-testid="price-and-discounted-price"]',
            ".prco-valign-middle-helper",
        ])
        highlights = await self._get_text_list([
            ".hp_desc_important_facilities .important_facility",
            '[data-testid="hotel-facilities-group"] li',
            "#property_description_content p",
        ], limit=3)

        name = title or fallback_name or "the selected hotel"
        parts = [f"I opened {name}."]
        if address:
            parts.append(f"Location: {address}.")
        if score:
            parts.append(f"Rating: {score}.")
        if price:
            parts.append(f"Price shown: {price}.")
        if highlights:
            parts.append(f"Highlights: {'; '.join(highlights)}.")
        parts.append("If you want to book it, tell me to reserve this hotel.")
        return " ".join(parts)

    async def _collect_guest_fields(self) -> list[str]:
        fields = await self._get_text_list([
            "label",
            ".bui-fieldset__label",
            '[data-testid="checkout-step-title"]',
        ], limit=10)

        visible_fields: list[str] = []
        for field in fields:
            lower = field.lower()
            if any(token in lower for token in ("name", "email", "phone", "address", "guest", "payment", "card")):
                visible_fields.append(field)
        return visible_fields[:4]

    async def _scroll_to_guest_form(self) -> bool:
        selectors = [
            'input[name*="firstname"]',
            'input[name*="last"]',
            'input[name*="email"]',
            'input[type="email"]',
            'input[name*="phone"]',
            'input[type="tel"]',
            'input[name*="card"]',
            'form',
        ]
        for selector in selectors:
            try:
                locator = self.page.locator(selector).first
                if await locator.count() == 0:
                    continue
                await locator.scroll_into_view_if_needed(timeout=1200)
                await self.page.wait_for_timeout(150)
                logger.info(f"Scrolled to guest form via {selector}")
                return True
            except Exception:
                continue
        return False

    async def _open_hotel_from_results(self, hotel_name: str = "", hotel_index: int | None = None) -> str:
        await self.page.wait_for_selector('[data-testid="property-card"]', timeout=12000)
        cards = self.page.locator('[data-testid="property-card"]')
        count = await cards.count()
        if count == 0:
            raise Exception("No hotel search results are visible. Please search first.")

        target_index = None
        selected_name = ""

        if hotel_name:
            lowered_query = hotel_name.lower()
            for index in range(min(count, 25)):
                card = cards.nth(index)
                try:
                    candidate_name = (await card.locator('[data-testid="title"]').first.text_content() or "").strip()
                except Exception:
                    candidate_name = ""
                if candidate_name and lowered_query in candidate_name.lower():
                    target_index = index
                    selected_name = candidate_name
                    break

        if target_index is None and hotel_index is not None:
            zero_based_index = max(hotel_index - 1, 0)
            if zero_based_index >= count:
                raise Exception(f"Only {count} hotel options are visible, so option {hotel_index} is not available.")
            target_index = zero_based_index

        if target_index is None:
            target_index = 0

        target_card = cards.nth(target_index)
        if not selected_name:
            try:
                selected_name = (await target_card.locator('[data-testid="title"]').first.text_content() or "").strip()
            except Exception:
                selected_name = f"option {target_index + 1}"

        for selector in ('a[data-testid="title-link"]', '[data-testid="title"] a', 'a[href*="/hotel/"]'):
            try:
                href = await target_card.locator(selector).first.get_attribute("href")
                if not href:
                    continue
                detail_url = href if href.startswith("http") else f"https://www.booking.com{href}"
                logger.info(f"Opening hotel detail in current tab: {detail_url}")
                await self.page.goto(detail_url, timeout=60000)
                await self._wait_brief_navigation()
                await self.page.wait_for_timeout(350)
                await self._dismiss_overlays()
                return selected_name
            except Exception:
                continue

        try:
            await target_card.click(timeout=1500)
        except Exception as exc:
            raise Exception(f"Could not open hotel details for {selected_name}: {exc}")

        await self._wait_brief_navigation()
        await self.page.wait_for_timeout(350)
        await self._dismiss_overlays()
        return selected_name

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

        await self.context.add_init_script(
            """
            window.open = function(url) {
                if (url) window.location.href = url;
                return window;
            };
            const stripTargets = () => {
                document.querySelectorAll('a[target="_blank"]').forEach(a => a.removeAttribute('target'));
            };
            new MutationObserver(stripTargets).observe(document.documentElement, { childList: true, subtree: true });
            document.addEventListener('DOMContentLoaded', stripTargets);
            """
        )

        self.page = await self.context.new_page()
        await stealth_async(self.page)
        self.context.on("page", lambda new_page: asyncio.ensure_future(self._handle_new_tab(new_page)))
        self.is_running = True
        logger.info("Browser initialized.")

    async def _handle_new_tab(self, new_page):
        if self.page is None or new_page == self.page:
            return
        logger.info("[NewTab] Detected new tab - switching context")
        await new_page.wait_for_load_state("domcontentloaded")
        old_page = self.page
        self.page = new_page

        if self._screencast_active and self._ws_send:
            ws_callback = self._ws_send
            try:
                await self.stop_screencast()
            except Exception:
                pass
            await self.start_screencast(ws_callback)
            logger.info("[NewTab] CDP screencast re-attached to new tab")

        if old_page and old_page != new_page:
            try:
                await old_page.close()
                logger.info("[NewTab] Closed old tab")
            except Exception:
                pass

    async def _safe_close(self):
        await self.stop_screencast()
        self.is_running = False
        for obj, method in (
            (self.page, "close"),
            (self.context, "close"),
            (self.browser, "close"),
            (self.playwright, "stop"),
        ):
            if obj:
                try:
                    await getattr(obj, method)()
                except Exception:
                    pass

    async def _dismiss_overlays(self):
        for selector in ("#onetrust-accept-btn-handler", 'button[data-testid="accept-cookies-button"]'):
            try:
                await self.page.click(selector, timeout=1200)
                logger.info(f"Accepted cookies via {selector}")
                await self.page.wait_for_timeout(150)
                break
            except Exception:
                pass

        for selector in (
            'button[aria-label="Dismiss sign-in info"]',
            'button[aria-label="Dismiss sign in interruption"]',
            '[data-testid="modal-close-button"]',
        ):
            try:
                await self.page.click(selector, timeout=1000)
                logger.info(f"Dismissed modal via {selector}")
                await self.page.wait_for_timeout(120)
            except Exception:
                pass

        try:
            await self.page.wait_for_selector(".bbe73dce14", state="hidden", timeout=1200)
        except Exception:
            try:
                await self.page.evaluate(
                    "document.querySelectorAll('.bbe73dce14').forEach(el => el.style.display='none')"
                )
                logger.info("Removed .bbe73dce14 overlay via JS")
            except Exception:
                pass

    async def execute_action(self, action: str, params: dict, session_id: str = "") -> dict:
        if not self.is_running:
            await self.init_browser()

        async with self._action_lock:
            try:
                if action == "search_hotel":
                    return await self.search_hotel(
                        destination=params.get("destination", ""),
                        checkin=params.get("checkin_date", ""),
                        checkout=params.get("checkout_date", ""),
                        adults=params.get("adults", 2),
                    )
                if action == "select_hotel":
                    return await self.select_hotel(
                        hotel_name=params.get("hotel_name", ""),
                        hotel_index=params.get("hotel_index"),
                    )
                if action == "reserve_hotel":
                    return await self.reserve_hotel(
                        hotel_name=params.get("hotel_name", ""),
                        hotel_index=params.get("hotel_index"),
                    )
                return {"success": False, "error": f"Unknown action: {action}"}
            except Exception as exc:
                logger.error(f"Error: {exc}")
                await self._snap()
                return {"success": False, "error": str(exc)}

    async def search_hotel(self, destination: str, checkin: str, checkout: str, adults: int) -> dict:
        logger.info(f"Searching: {destination!r}, {checkin} -> {checkout}, {adults} adults")

        try:
            search_url = (
                "https://www.booking.com/searchresults.html"
                f"?ss={quote_plus(destination)}"
                f"&checkin={checkin}"
                f"&checkout={checkout}"
                f"&group_adults={adults}"
                "&no_rooms=1"
            )

            logger.info(f"Navigating directly to search results: {search_url}")
            await self.page.goto(search_url, timeout=60000)
            await self._wait_brief_navigation()
            await self.page.wait_for_timeout(300)
            await self._dismiss_overlays()

            logger.info("Waiting for results...")
            await self.page.wait_for_selector('[data-testid="property-card"]', timeout=12000)
            await self.page.wait_for_timeout(250)

            cards = self.page.locator('[data-testid="property-card"]')
            count = min(await cards.count(), 3)
            results = []
            for index in range(count):
                card = cards.nth(index)
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
                results.append(f"- {name} - {price}" + (f" | {score}" if score else ""))

            result_msg = f"Found {count} hotel(s) in {destination}:\n" + "\n".join(results)
            logger.info(f"Done: {result_msg[:140]}")
            return {"success": True, "result": result_msg}
        except Exception as exc:
            await self._snap()
            raise Exception(f"Failed: {exc}")

    async def select_hotel(self, hotel_name: str = "", hotel_index: int | None = None) -> dict:
        logger.info(f"Selecting hotel for hotel_name={hotel_name!r}, hotel_index={hotel_index}")

        try:
            await self._dismiss_overlays()
            selected_name = hotel_name.strip()

            if "searchresults" in self.page.url:
                selected_name = await self._open_hotel_from_results(selected_name, hotel_index)
            elif not selected_name:
                selected_name = await self._get_first_text([
                    'h2[data-testid="title"]',
                    '[data-testid="title"]',
                    "h1",
                    "h2",
                ]) or "the selected hotel"

            summary = await self._summarize_selected_hotel(selected_name)
            await self._snap()
            return {"success": True, "result": summary}
        except Exception as exc:
            await self._snap()
            raise Exception(f"Failed to open hotel details: {exc}")

    async def reserve_hotel(self, hotel_name: str = "", hotel_index: int | None = None) -> dict:
        logger.info(f"Starting reservation flow for hotel_name={hotel_name!r}, hotel_index={hotel_index}")

        try:
            await self._dismiss_overlays()
            selected_name = hotel_name.strip()

            if "searchresults" in self.page.url:
                selected_name = await self._open_hotel_from_results(selected_name, hotel_index)
                await self.page.wait_for_timeout(300)
                await self._dismiss_overlays()
            elif not selected_name:
                selected_name = await self._get_first_text([
                    'h2[data-testid="title"]',
                    '[data-testid="title"]',
                    "h1",
                    "h2",
                ]) or "the selected hotel"

            availability_clicked = await self._click_first_visible(
                [
                    'button[data-testid="availability-cta-btn"]',
                    'a[data-testid="availability-cta-btn"]',
                    'button:has-text("See availability")',
                    'a:has-text("See availability")',
                    'button:has-text("Reserve")',
                    'a:has-text("Reserve")',
                    'button:has-text("Book now")',
                    'a:has-text("Book now")',
                    'button:has-text("Select your room")',
                    'a:has-text("Select your room")',
                ],
                timeout=1500,
            )

            if availability_clicked:
                await self._wait_brief_navigation()
                await self.page.wait_for_timeout(250)
                await self._dismiss_overlays()

            try:
                room_selects = self.page.locator("select.hprt-nos-select")
                if await room_selects.count() > 0:
                    await room_selects.first.select_option(value="1", timeout=800)
                    logger.info("Selected 1 room from dropdown")
            except Exception:
                pass

            reserve_clicked = await self._click_first_visible(
                [
                    "button:has-text(\"I'll reserve\")",
                    "a:has-text(\"I'll reserve\")",
                    'button:has-text("I\'ll reserve")',
                    'a:has-text("I\'ll reserve")',
                    'button:has-text("Toi se dat")',
                    'a:has-text("Toi se dat")',
                    'button:has-text("Tôi sẽ đặt")',
                    'a:has-text("Tôi sẽ đặt")',
                    'button:has-text("Reserve")',
                    'a:has-text("Reserve")',
                    'button:has-text("Select your room")',
                    'a:has-text("Select your room")',
                    'button:has-text("Book now")',
                    'a:has-text("Book now")',
                ],
                timeout=1800,
            )

            if reserve_clicked:
                await self._wait_brief_navigation()
                await self.page.wait_for_timeout(250)
                await self._dismiss_overlays()

                # We will no longer click the second confirmation button here.
                # It will be done after filling out the form.
            form_visible = await self._scroll_to_guest_form()
            guest_fields = await self._collect_guest_fields()
            await self._snap()

            guest_hint = "Ask the guest for the visible personal details and fill the form on the website."
            if guest_fields:
                guest_hint = (
                    "Ask the guest for these details and fill the form on the website: "
                    + ", ".join(guest_fields)
                    + "."
                )
            elif form_visible:
                guest_hint = "The guest information form is on screen. Ask for the visible personal details and fill them in now."
            else:
                guest_hint = (
                    "The booking flow is open, but the guest form is not visible yet. "
                    "Scroll a little further or choose the next booking button shown on the page."
                )

            return {
                "success": True,
                "result": (
                    f"I opened the booking flow for {selected_name}. "
                    f"{guest_hint} Continue as soon as the guest provides the information."
                ),
            }
        except Exception as exc:
            await self._snap()
            raise Exception(f"Failed to start reservation: {exc}")

    async def close(self):
        await self._safe_close()


booking_agent = BookingAgent()
