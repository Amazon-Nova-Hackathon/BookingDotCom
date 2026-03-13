import asyncio
import base64
from urllib.parse import urlencode

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
        self._last_search_params: dict[str, object] = {}
        self._last_guest_info: dict[str, str] = {}

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
        if not selectors:
            return False

        try:
            combined_selector = ", ".join(selectors)
            await self.page.wait_for_selector(combined_selector, state="attached", timeout=timeout)
        except Exception:
            return False

        for selector in selectors:
            try:
                locator = self.page.locator(selector).first
                if await locator.count() == 0:
                    continue
                await locator.scroll_into_view_if_needed(timeout=500)
                await locator.click(timeout=500)
                logger.info(f"Clicked selector: {selector}")
                return True
            except Exception:
                continue
        return False

    @staticmethod
    def _clean_text(value: str) -> str:
        return " ".join((value or "").split()).strip()

    async def _get_first_text(self, selectors: list[str], timeout: int = 1200) -> str:
        if not selectors:
            return ""

        try:
            combined_selector = ", ".join(selectors)
            await self.page.wait_for_selector(combined_selector, state="attached", timeout=timeout)
        except Exception:
            return ""

        for selector in selectors:
            try:
                locator = self.page.locator(selector).first
                if await locator.count() == 0:
                    continue
                text = await locator.text_content(timeout=500)
                text = self._clean_text(text or "")
                if text:
                    return text
            except Exception:
                continue
        return ""

    async def _get_text_list(self, selectors: list[str], limit: int = 4, timeout: int = 1200) -> list[str]:
        values: list[str] = []
        if not selectors:
            return values

        try:
            combined_selector = ", ".join(selectors)
            await self.page.wait_for_selector(combined_selector, state="attached", timeout=timeout)
        except Exception:
            pass

        for selector in selectors:
            try:
                locator = self.page.locator(selector)
                count = await locator.count()
                if count == 0:
                    continue
                count = min(count, limit)
                for index in range(count):
                    text = await locator.nth(index).text_content(timeout=500)
                    text = self._clean_text(text or "")
                    if text and text not in values:
                        values.append(text)
                    if len(values) >= limit:
                        return values
            except Exception:
                continue
        return values

    async def _get_form_controls(self) -> list[dict]:
        return await self.page.evaluate(
            """
            () => {
                const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                const controls = Array.from(document.querySelectorAll('input, textarea, select'));
                return controls.map((el, index) => {
                    const style = window.getComputedStyle(el);
                    const visible = !el.disabled
                        && style.display !== 'none'
                        && style.visibility !== 'hidden'
                        && style.opacity !== '0'
                        && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);

                    const labels = [];
                    if (el.labels) {
                        labels.push(...Array.from(el.labels).map(label => label.innerText || ''));
                    }
                    const wrappedLabel = el.closest('label');
                    if (wrappedLabel) {
                        labels.push(wrappedLabel.innerText || '');
                    }
                    if (el.id) {
                        const forLabel = document.querySelector(`label[for="${el.id}"]`);
                        if (forLabel) {
                            labels.push(forLabel.innerText || '');
                        }
                    }
                    const describedBy = (el.getAttribute('aria-describedby') || '').split(/\\s+/).filter(Boolean);
                    for (const id of describedBy) {
                        const node = document.getElementById(id);
                        if (node) {
                            labels.push(node.innerText || '');
                        }
                    }

                    return {
                        index,
                        tag: el.tagName.toLowerCase(),
                        type: (el.getAttribute('type') || '').toLowerCase(),
                        name: el.getAttribute('name') || '',
                        id: el.id || '',
                        placeholder: el.getAttribute('placeholder') || '',
                        ariaLabel: el.getAttribute('aria-label') || '',
                        autocomplete: el.getAttribute('autocomplete') || '',
                        label: clean(labels.join(' ')),
                        required: !!(el.required || el.getAttribute('required') !== null || el.getAttribute('aria-required') === 'true'),
                        value: 'value' in el ? String(el.value || '') : '',
                        checked: 'checked' in el ? !!el.checked : false,
                        visible,
                        disabled: !!el.disabled,
                        options: el.tagName.toLowerCase() === 'select'
                            ? Array.from(el.options).map(opt => ({
                                value: opt.value || '',
                                label: clean(opt.textContent || ''),
                            }))
                            : [],
                    };
                }).filter(control => control.visible);
            }
            """
        )

    @staticmethod
    def _split_full_name(full_name: str) -> tuple[str, str]:
        parts = [part for part in (full_name or "").split() if part]
        if not parts:
            return "", ""
        if len(parts) == 1:
            return parts[0], ""
        return parts[0], " ".join(parts[1:])

    def _merge_guest_info(self, **kwargs) -> dict[str, str]:
        guest_info = dict(self._last_guest_info)
        for key, value in kwargs.items():
            if value is None:
                continue
            text = str(value).strip()
            if text:
                guest_info[key] = text

        if guest_info.get("full_name") and (not guest_info.get("first_name") or not guest_info.get("last_name")):
            first_name, last_name = self._split_full_name(guest_info["full_name"])
            guest_info.setdefault("first_name", first_name)
            if last_name:
                guest_info.setdefault("last_name", last_name)
        elif guest_info.get("first_name") and guest_info.get("last_name") and not guest_info.get("full_name"):
            guest_info["full_name"] = f"{guest_info['first_name']} {guest_info['last_name']}".strip()

        if guest_info.get("region") and not guest_info.get("phone_country_code"):
            guest_info["phone_country_code"] = guest_info["region"]

        return guest_info

    @staticmethod
    def _control_descriptor(control: dict) -> str:
        return " ".join(
            str(control.get(key, "") or "")
            for key in ("label", "name", "id", "placeholder", "ariaLabel", "autocomplete", "type")
        ).lower()

    @staticmethod
    def _is_required_control(control: dict) -> bool:
        text = " ".join(
            str(control.get(key, "") or "")
            for key in ("label", "placeholder", "ariaLabel", "name")
        )
        return bool(control.get("required")) or "*" in text

    def _standard_field_key_for_control(self, control: dict) -> str | None:
        text = self._control_descriptor(control)
        field_type = (control.get("type") or "").lower()
        tag = (control.get("tag") or "").lower()

        if field_type in ("hidden", "submit", "button", "search", "reset", "file"):
            return None
        if "email" in text:
            return "email"
        if tag == "select" and any(
            token in text for token in ("phone", "mobile", "telephone", "tel", "dial code", "calling code", "country code")
        ):
            return "phone_country_code"
        if any(token in text for token in ("phone", "mobile", "telephone", "tel")):
            return "phone"
        if any(token in text for token in ("first name", "firstname", "given name", "given_name")):
            return "first_name"
        if any(token in text for token in ("last name", "lastname", "surname", "family name", "family_name")):
            return "last_name"
        if any(token in text for token in ("address line 1", "address1", "street address", "address line1")):
            return "address_line1"
        if any(token in text for token in ("address line 2", "address2", "apartment", "suite", "unit")):
            return "address_line2"
        if any(token in text for token in ("city", "town")):
            return "city"
        if "full name" in text or ("guest name" in text and "first" not in text and "last" not in text):
            return "full_name"
        if any(token in text for token in ("region", "country")) and tag == "select":
            return "region"
        if any(token in text for token in ("arrival", "check-in time", "arrival time")):
            return "arrival_time"
        if tag == "textarea" and any(token in text for token in ("special request", "special requirement", "message", "note")):
            return "special_requests"
        if "name" in text and "user" not in text and "email" not in text and "phone" not in text:
            return "full_name"
        return None

    async def _fill_control(self, control: dict, value: str) -> bool:
        locator = self.page.locator("input, textarea, select").nth(int(control["index"]))
        await locator.scroll_into_view_if_needed(timeout=1200)

        tag = (control.get("tag") or "").lower()
        field_type = (control.get("type") or "").lower()
        if tag == "select":
            options = control.get("options") or []
            lower_value = value.lower()
            for option in options:
                option_label = str(option.get("label", "")).lower()
                option_value = str(option.get("value", "")).lower()
                if lower_value == option_label or lower_value == option_value or lower_value in option_label:
                    if option.get("value"):
                        await locator.select_option(value=option["value"], timeout=1200)
                    elif option.get("label"):
                        await locator.select_option(label=option["label"], timeout=1200)
                    else:
                        return False
                    return True
            return False

        if field_type in ("checkbox", "radio"):
            desired = value.lower() in ("true", "yes", "1")
            if desired:
                await locator.check(timeout=1200)
            else:
                await locator.uncheck(timeout=1200)
            return True

        await locator.fill(value, timeout=1200)
        return True

    async def _apply_optional_choices(self, choices: list[str]) -> list[str]:
        applied: list[str] = []
        if not choices:
            return applied

        controls = await self._get_form_controls()
        normalized_pairs = [
            (choice, self._clean_text(choice).lower())
            for choice in choices
            if self._clean_text(choice)
        ]

        for raw_choice, normalized_choice in normalized_pairs:
            matched = False

            for control in controls:
                if self._standard_field_key_for_control(control):
                    continue

                tag = (control.get("tag") or "").lower()
                field_type = (control.get("type") or "").lower()
                label = self._clean_text(
                    str(control.get("label") or control.get("placeholder") or control.get("ariaLabel") or control.get("name") or "")
                )
                label_lower = label.lower()
                descriptor = self._control_descriptor(control)

                try:
                    if field_type in ("checkbox", "radio"):
                        if normalized_choice not in descriptor and (not label_lower or label_lower not in normalized_choice):
                            continue
                        if not control.get("checked"):
                            await self._fill_control(control, "yes")
                        applied.append(label or raw_choice)
                        matched = True
                        break

                    if tag == "select":
                        options = control.get("options") or []
                        option_match = next(
                            (
                                option
                                for option in options
                                if normalized_choice in str(option.get("label", "")).lower()
                                or normalized_choice in str(option.get("value", "")).lower()
                            ),
                            None,
                        )
                        if not option_match:
                            continue
                        option_value = option_match.get("value") or option_match.get("label") or raw_choice
                        await self._fill_control(control, str(option_value))
                        option_label = self._clean_text(str(option_match.get("label") or option_value))
                        applied.append(f"{label or 'option'}: {option_label}")
                        matched = True
                        break
                except Exception:
                    continue

            if not matched:
                logger.info(f"No optional control matched choice: {raw_choice}")

        return applied

    async def _collect_special_form_questions(self) -> list[str]:
        questions: list[str] = []
        controls = await self._get_form_controls()
        for control in controls:
            field_type = (control.get("type") or "").lower()
            tag = (control.get("tag") or "").lower()
            if field_type in ("hidden", "submit", "button", "search", "reset", "file"):
                continue

            tag = (control.get("tag") or "").lower()
            field_type = (control.get("type") or "").lower()
            current_value = self._clean_text(str(control.get("value", "") or ""))
            if field_type in ("checkbox", "radio") and control.get("checked"):
                continue
            if tag == "select" and current_value:
                continue
            if tag in ("input", "textarea") and current_value:
                continue
            if self._is_required_control(control):
                continue

            known_key = self._standard_field_key_for_control(control)
            if known_key in ("arrival_time", "special_requests"):
                label = "arrival time" if known_key == "arrival_time" else "special requests"
                if label not in questions:
                    questions.append(label)
                if len(questions) >= 4:
                    break
                continue
            if known_key:
                continue

            label = self._clean_text(str(control.get("label") or control.get("placeholder") or control.get("ariaLabel") or control.get("name") or ""))
            if not label or len(label) < 3:
                continue
            if tag == "select" or field_type in ("checkbox", "radio") or control.get("required"):
                if label not in questions:
                    questions.append(label)
            if len(questions) >= 4:
                break
        return questions

    async def _collect_validation_messages(self) -> list[str]:
        messages = await self._get_text_list(
            [
                ".bui-field-error",
                '[data-testid*="error"]',
                '[aria-live="polite"]',
                '[role="alert"]',
                ".form-group__error",
                ".c-form__error",
            ],
            limit=6,
        )
        return [message for message in messages if len(message) > 2]

    async def _summarize_selected_hotel(self, fallback_name: str) -> str:
        title = await self._get_first_text([
            'h2[data-testid="title"]',
            '[data-testid="title"]',
            "#hp_hotel_name h2",
            "h1",
            "h2",
        ], timeout=700)
        score = await self._get_first_text([
            '[data-testid="review-score-component"]',
            '[data-testid="review-score"]',
        ], timeout=500)
        price = await self._get_first_text([
            '[data-testid="price-for-x-nights"]',
            '[data-testid="price-and-discounted-price"]',
            ".prco-valign-middle-helper",
        ], timeout=500)

        name = title or fallback_name or "the selected hotel"
        parts = [f"I opened {name}."]
        if score:
            parts.append(f"Rating: {score}.")
        if price:
            parts.append(f"Price shown: {price}.")
        parts.append("If you want to book it, tell me to reserve this hotel.")
        return " ".join(parts)

    async def _collect_guest_fields(self, required_only: bool | None = None) -> list[str]:
        controls = await self._get_form_controls()
        display_labels = {
            "full_name": "full name",
            "first_name": "full name",
            "last_name": "full name",
            "email": "email",
            "phone": "phone number",
            "phone_country_code": "",
            "region": "region",
            "address_line1": "address line 1",
            "address_line2": "address line 2",
            "city": "city",
            "arrival_time": "arrival time",
        }

        visible_fields: list[str] = []
        for control in controls:
            key = self._standard_field_key_for_control(control)
            label = display_labels.get(key or "")
            if required_only is True and not self._is_required_control(control):
                continue
            if required_only is False and self._is_required_control(control):
                continue
            if label and label not in visible_fields:
                visible_fields.append(label)

        ordered_labels = (
            "full name",
            "email",
            "region",
            "phone number",
            "address line 1",
            "address line 2",
            "city",
            "arrival time",
        )
        return [label for label in ordered_labels if label in visible_fields]

    async def _collect_missing_field_labels(self, required_only: bool) -> list[str]:
        controls = await self._get_form_controls()
        display_labels = {
            "full_name": "full name",
            "first_name": "full name",
            "last_name": "full name",
            "email": "email",
            "phone": "phone number",
            "phone_country_code": "",
            "region": "region",
            "address_line1": "address line 1",
            "address_line2": "address line 2",
            "city": "city",
            "arrival_time": "arrival time",
        }

        missing: list[str] = []
        for control in controls:
            key = self._standard_field_key_for_control(control)
            label = display_labels.get(key or "")
            if not label:
                continue
            if required_only and not self._is_required_control(control):
                continue
            if not required_only and self._is_required_control(control):
                continue

            tag = (control.get("tag") or "").lower()
            field_type = (control.get("type") or "").lower()
            current_value = self._clean_text(str(control.get("value", "") or ""))

            if field_type in ("checkbox", "radio") and control.get("checked"):
                continue
            if tag == "select" and current_value:
                continue
            if tag in ("input", "textarea") and current_value:
                continue

            if label not in missing:
                missing.append(label)

        ordered_labels = (
            "full name",
            "email",
            "region",
            "phone number",
            "address line 1",
            "address line 2",
            "city",
            "arrival time",
        )
        return [label for label in ordered_labels if label in missing]

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
        
        try:
            combined_selector = ", ".join(selectors)
            await self.page.wait_for_selector(combined_selector, state="attached", timeout=1200)
        except Exception:
            pass

        for selector in selectors:
            try:
                locator = self.page.locator(selector).first
                if await locator.count() == 0:
                    continue
                await locator.scroll_into_view_if_needed(timeout=500)
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
                        adults=params.get("adults"),
                        children=params.get("children"),
                        children_ages=params.get("children_ages"),
                        rooms=params.get("rooms"),
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
                if action == "fill_guest_info":
                    return await self.fill_guest_info(
                        full_name=params.get("full_name", ""),
                        first_name=params.get("first_name", ""),
                        last_name=params.get("last_name", ""),
                        email=params.get("email", ""),
                        phone=params.get("phone", ""),
                        region=params.get("region", ""),
                        address_line1=params.get("address_line1", ""),
                        address_line2=params.get("address_line2", ""),
                        city=params.get("city", ""),
                        optional_choices=params.get("optional_choices"),
                        arrival_time=params.get("arrival_time", ""),
                        special_requests=params.get("special_requests", ""),
                    )
                if action == "continue_to_payment":
                    return await self.continue_to_payment()
                return {"success": False, "error": f"Unknown action: {action}"}
            except Exception as exc:
                logger.error(f"Error: {exc}")
                await self._snap()
                return {"success": False, "error": str(exc)}

    def _merge_search_params(
        self,
        destination: str = "",
        checkin: str = "",
        checkout: str = "",
        adults: int | None = None,
        children: int | None = None,
        children_ages: list[int] | None = None,
        rooms: int | None = None,
    ) -> dict[str, object]:
        params = dict(self._last_search_params)

        if destination:
            params["destination"] = destination
        if checkin:
            params["checkin_date"] = checkin
        if checkout:
            params["checkout_date"] = checkout
        if adults is not None:
            params["adults"] = adults
        if children is not None:
            params["children"] = max(children, 0)
            if children == 0:
                params["children_ages"] = []
        if children_ages is not None:
            params["children_ages"] = [int(age) for age in children_ages]
            if children is None:
                params["children"] = len(children_ages)
        if rooms is not None:
            params["rooms"] = rooms

        params.setdefault("children", 0)
        params.setdefault("children_ages", [])
        params.setdefault("rooms", 1)

        return params

    async def search_hotel(
        self,
        destination: str = "",
        checkin: str = "",
        checkout: str = "",
        adults: int | None = None,
        children: int | None = None,
        children_ages: list[int] | None = None,
        rooms: int | None = None,
    ) -> dict:
        search_params = self._merge_search_params(
            destination=destination,
            checkin=checkin,
            checkout=checkout,
            adults=adults,
            children=children,
            children_ages=children_ages,
            rooms=rooms,
        )

        destination = str(search_params.get("destination", "")).strip()
        checkin = str(search_params.get("checkin_date", "")).strip()
        checkout = str(search_params.get("checkout_date", "")).strip()
        adults = int(search_params.get("adults", 0) or 0)
        children = int(search_params.get("children", 0) or 0)
        rooms = int(search_params.get("rooms", 1) or 1)
        ages = [int(age) for age in search_params.get("children_ages", []) or []]

        if not destination or not checkin or not checkout or adults <= 0:
            raise Exception("Search is missing destination, dates, or adult count.")
        if rooms <= 0:
            raise Exception("Rooms must be at least 1.")
        if children < 0:
            raise Exception("Children cannot be negative.")

        logger.info(
            f"Searching: {destination!r}, {checkin} -> {checkout}, {adults} adults, "
            f"{children} children, {rooms} rooms"
        )

        try:
            query_params: list[tuple[str, str | int]] = [
                ("ss", destination),
                ("checkin", checkin),
                ("checkout", checkout),
                ("group_adults", adults),
                ("no_rooms", rooms),
                ("group_children", children),
            ]
            for age in ages:
                query_params.append(("age", age))

            search_url = (
                "https://www.booking.com/searchresults.html?"
                + urlencode(query_params, doseq=True)
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
            self._last_search_params = {
                "destination": destination,
                "checkin_date": checkin,
                "checkout_date": checkout,
                "adults": adults,
                "children": children,
                "children_ages": ages,
                "rooms": rooms,
            }
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
            required_fields = await self._collect_guest_fields(required_only=True)
            optional_fields = await self._collect_guest_fields(required_only=False)
            special_questions = await self._collect_special_form_questions()
            await self._snap()

            guest_hint = "Ask the guest for the required details shown on the form."
            if required_fields:
                guest_hint = "Required fields: " + ", ".join(required_fields) + "."
            elif form_visible:
                guest_hint = "The guest information form is on screen. Ask for the required details now."
            else:
                guest_hint = (
                    "The booking flow is open, but the guest form is not visible yet. "
                    "Scroll a little further or choose the next booking button shown on the page."
                )

            optional_items: list[str] = []
            for item in optional_fields + special_questions:
                if item not in optional_items:
                    optional_items.append(item)

            special_hint = ""
            if optional_items:
                special_hint = " Optional fields or choices: " + ", ".join(optional_items) + "."

            return {
                "success": True,
                "result": (
                    f"I opened the booking flow for {selected_name}. "
                    f"{guest_hint}{special_hint}"
                ),
            }
        except Exception as exc:
            await self._snap()
            raise Exception(f"Failed to start reservation: {exc}")

    async def fill_guest_info(
        self,
        full_name: str = "",
        first_name: str = "",
        last_name: str = "",
        email: str = "",
        phone: str = "",
        region: str = "",
        address_line1: str = "",
        address_line2: str = "",
        city: str = "",
        optional_choices: list[str] | None = None,
        arrival_time: str = "",
        special_requests: str = "",
    ) -> dict:
        logger.info("Filling guest information on the booking form")

        try:
            await self._dismiss_overlays()
            await self._scroll_to_guest_form()

            guest_info = self._merge_guest_info(
                full_name=full_name,
                first_name=first_name,
                last_name=last_name,
                email=email,
                phone=phone,
                region=region,
                address_line1=address_line1,
                address_line2=address_line2,
                city=city,
                arrival_time=arrival_time,
                special_requests=special_requests,
            )

            controls = await self._get_form_controls()
            filled_labels: list[str] = []
            seen_keys: set[str] = set()
            display_labels = {
                "full_name": "full name",
                "first_name": "full name",
                "last_name": "full name",
                "email": "email",
                "phone": "phone number",
                "phone_country_code": "",
                "region": "region",
                "address_line1": "address line 1",
                "address_line2": "address line 2",
                "city": "city",
                "arrival_time": "arrival time",
                "special_requests": "special requests",
            }

            for control in controls:
                key = self._standard_field_key_for_control(control)
                if not key or key in seen_keys:
                    continue
                value = guest_info.get(key, "").strip()
                if not value:
                    continue
                current_value = str(control.get("value", "") or "").strip()
                if current_value and key != "special_requests":
                    seen_keys.add(key)
                    continue
                try:
                    filled = await self._fill_control(control, value)
                except Exception:
                    filled = False
                if filled:
                    label = display_labels.get(key, self._clean_text(str(control.get("label") or control.get("name") or key)))
                    if label and label not in filled_labels:
                        filled_labels.append(label)
                    seen_keys.add(key)

            self._last_guest_info = guest_info
            selected_options = await self._apply_optional_choices(optional_choices or [])

            missing_required_fields = await self._collect_missing_field_labels(required_only=True)
            missing_optional_fields = await self._collect_missing_field_labels(required_only=False)
            special_questions = await self._collect_special_form_questions()
            validation_messages = await self._collect_validation_messages()
            await self._snap()

            filled_text = "I updated the guest form."
            if filled_labels:
                filled_text = "I filled the guest details on the form."
            if selected_options:
                filled_text += " I also selected: " + ", ".join(selected_options) + "."

            follow_up = ""
            if missing_required_fields:
                follow_up += " Required fields still missing: " + ", ".join(missing_required_fields) + "."
            optional_items: list[str] = []
            for item in missing_optional_fields + special_questions:
                if item not in optional_items and item not in missing_required_fields:
                    optional_items.append(item)
            if optional_items:
                follow_up += (
                    " Optional fields or choices still visible: "
                    + ", ".join(optional_items)
                    + ". Ask the guest whether any of them should be selected."
                )
            if validation_messages:
                follow_up += " Current form messages: " + "; ".join(validation_messages) + "."
            if not follow_up:
                follow_up = " If everything looks correct, tell me to continue to payment."

            return {"success": True, "result": filled_text + follow_up}
        except Exception as exc:
            await self._snap()
            raise Exception(f"Failed to fill guest information: {exc}")

    async def continue_to_payment(self) -> dict:
        logger.info("Continuing booking flow to payment")

        try:
            await self._dismiss_overlays()
            clicked = await self._click_first_visible(
                [
                    'button:has-text("Next: Final details")',
                    'button:has-text("Tiếp theo: Chi tiết cuối cùng")',
                    'button:has-text("Continue")',
                    'a:has-text("Continue")',
                    'button:has-text("Go to payment")',
                    'a:has-text("Go to payment")',
                    'button:has-text("Proceed to payment")',
                    'a:has-text("Proceed to payment")',
                    'button:has-text("Đặt ngay")',
                    'a:has-text("Đặt ngay")',
                    'button:has-text("Book now")',
                    'a:has-text("Book now")',
                    ".hprt-reservation-cta",
                    "button.txp-bui-main-pp",
                    '.bui-button--primary:has-text("Tôi sẽ đặt")',
                    '.bui-button--primary:has-text("Đặt ngay")',
                ],
                timeout=1800,
            )
            if not clicked:
                await self._snap()
                return {
                    "success": True,
                    "result": "I could not find the next payment button yet. Please check whether the booking form still has missing information.",
                }

            await self._wait_brief_navigation()
            await self.page.wait_for_timeout(300)
            await self._dismiss_overlays()
            await self._scroll_to_guest_form()

            validation_messages = await self._collect_validation_messages()
            special_questions = await self._collect_special_form_questions()
            page_text = await self._get_text_list(
                [
                    "h1",
                    "h2",
                    '[data-testid="title"]',
                    '[data-testid="checkout-step-title"]',
                ],
                limit=4,
            )
            await self._snap()

            if validation_messages:
                return {
                    "success": True,
                    "result": (
                        "I tried to continue, but the form still needs attention: "
                        + "; ".join(validation_messages)
                        + "."
                    ),
                }

            if special_questions:
                return {
                    "success": True,
                    "result": (
                        "I tried to continue, but the page still shows these questions: "
                        + ", ".join(special_questions)
                        + "."
                    ),
                }

            page_summary = ""
            if page_text:
                page_summary = " Current page: " + " | ".join(page_text[:3]) + "."

            return {
                "success": True,
                "result": "I moved to the next booking step. The payment page or final details step should now be open." + page_summary,
            }
        except Exception as exc:
            await self._snap()
            raise Exception(f"Failed to continue to payment: {exc}")

    async def close(self):
        await self._safe_close()


booking_agent = BookingAgent()
