import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.playwright_agent import booking_agent


@pytest.mark.asyncio
async def test_search_hotel_success():
    params = {
        "destination": "Paris",
        "checkin_date": "2026-04-10",
        "checkout_date": "2026-04-12",
        "adults": 2,
    }

    try:
        result = await booking_agent.execute_action("search_hotel", params, session_id="test_search_only")
    finally:
        await booking_agent._safe_close()

    assert result.get("success") is True
    assert "Found" in result.get("result", "")
    assert "hotel" in result.get("result", "").lower()


@pytest.mark.asyncio
async def test_search_to_reservation_pipeline():
    search_params = {
        "destination": "Paris",
        "checkin_date": "2026-04-10",
        "checkout_date": "2026-04-12",
        "adults": 2,
    }

    try:
        search_result = await booking_agent.execute_action(
            "search_hotel",
            search_params,
            session_id="test_reservation_pipeline_search",
        )
        assert search_result.get("success") is True

        select_result = await booking_agent.execute_action(
            "select_hotel",
            {"hotel_index": 1},
            session_id="test_reservation_pipeline_select",
        )
        assert select_result.get("success") is True

        reserve_result = await booking_agent.execute_action(
            "reserve_hotel",
            {},
            session_id="test_reservation_pipeline_reserve",
        )
    finally:
        await booking_agent._safe_close()

    assert "i opened" in select_result.get("result", "").lower()
    assert reserve_result.get("success") is True
    assert "booking flow" in reserve_result.get("result", "").lower()
    assert "personal information" in reserve_result.get("result", "").lower()


@pytest.mark.asyncio
async def test_invalid_action():
    try:
        result = await booking_agent.execute_action("fake_action", {}, session_id="test_invalid_action")
    finally:
        await booking_agent._safe_close()

    assert result.get("success") is False
    assert "Unknown action" in result.get("error", "")
