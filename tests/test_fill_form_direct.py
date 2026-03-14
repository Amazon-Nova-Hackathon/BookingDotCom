import asyncio
import json
import os
from datetime import datetime

from src.playwright_agent import booking_agent


async def run_direct_fill_test() -> dict:
    """
    Directly test fill_guest_info on a booking form URL.
    This bypasses search/select/reserve flow.

    Required env var:
    - BOOKING_FORM_URL

    Optional env vars:
    - TEST_FULL_NAME
    - TEST_EMAIL
    - TEST_PHONE
    - TEST_REGION
    - TEST_ARRIVAL_TIME
    - TEST_SCREENSHOT_PATH
    """

    form_url = os.getenv("BOOKING_FORM_URL", "").strip()
    if not form_url:
        raise RuntimeError(
            "Missing BOOKING_FORM_URL. "
            "Set it to a direct Booking.com guest form URL, then run again."
        )

    full_name = os.getenv("TEST_FULL_NAME", "Wilson John")
    email = os.getenv("TEST_EMAIL", "wilson@gmail.com")
    phone = os.getenv("TEST_PHONE", "0123456789")
    region = os.getenv("TEST_REGION", "Vietnam")
    arrival_time = os.getenv("TEST_ARRIVAL_TIME", "")
    screenshot_path = os.getenv("TEST_SCREENSHOT_PATH", "").strip()
    if not screenshot_path:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_path = os.path.join("tests", "output", f"fill_form_after_{ts}.png")
    screenshot_path = os.path.abspath(screenshot_path)

    if not booking_agent.is_running:
        await booking_agent.init_browser()

    await booking_agent._goto_with_fallback(form_url, timeout_ms=60000)
    await booking_agent._wait_brief_navigation()
    await booking_agent.page.wait_for_timeout(300)
    await booking_agent._dismiss_overlays()
    form_visible = await booking_agent._scroll_to_guest_form(max_rounds=10)

    missing_required_before = await booking_agent._collect_missing_field_labels(
        required_only=True
    )

    fill_result = await booking_agent.fill_guest_info(
        full_name=full_name,
        email=email,
        phone=phone,
        region=region,
        arrival_time=arrival_time,
    )

    os.makedirs(os.path.dirname(screenshot_path), exist_ok=True)
    await booking_agent.page.screenshot(path=screenshot_path, full_page=True)

    missing_required_after = await booking_agent._collect_missing_field_labels(
        required_only=True
    )

    return {
        "form_url": booking_agent.page.url,
        "form_visible": form_visible,
        "missing_required_before": missing_required_before,
        "fill_result": fill_result,
        "missing_required_after": missing_required_after,
        "screenshot_after_fill": screenshot_path,
    }


async def _main():
    report = {}
    try:
        report = await run_direct_fill_test()
    finally:
        if booking_agent.is_running:
            await booking_agent.close()

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(_main())
