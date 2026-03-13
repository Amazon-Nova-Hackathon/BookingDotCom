import aiohttp
import pytest


BROWSER_SERVICE_URL = "http://localhost:7863"


async def post_execute(payload: dict) -> tuple[int, dict]:
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{BROWSER_SERVICE_URL}/api/execute", json=payload, timeout=90) as resp:
            return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_health_check_endpoint():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BROWSER_SERVICE_URL}/api/health", timeout=3) as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["status"] == "ok"
    except aiohttp.ClientConnectorError:
        pytest.skip("Browser service is not running. Start `python main_browser_service.py` first.")


@pytest.mark.asyncio
async def test_execute_search_endpoint():
    payload = {
        "action": "search_hotel",
        "params": {
            "destination": "Paris",
            "checkin_date": "2026-04-10",
            "checkout_date": "2026-04-12",
            "adults": 2,
        },
        "session_id": "integration_search_only",
        "request_id": "integration_search_only",
    }

    try:
        status, data = await post_execute(payload)
    except aiohttp.ClientConnectorError:
        pytest.skip("Browser service is not running. Start `python main_browser_service.py` first.")

    assert status == 200
    assert data["success"] is True
    assert "Found" in data["result"]


@pytest.mark.asyncio
async def test_search_to_reservation_pipeline_endpoint():
    search_payload = {
        "action": "search_hotel",
        "params": {
            "destination": "Paris",
            "checkin_date": "2026-04-10",
            "checkout_date": "2026-04-12",
            "adults": 2,
        },
        "session_id": "integration_pipeline_search",
        "request_id": "integration_pipeline_search",
    }
    select_payload = {
        "action": "select_hotel",
        "params": {
            "hotel_index": 1,
        },
        "session_id": "integration_pipeline_select",
        "request_id": "integration_pipeline_select",
    }
    reserve_payload = {
        "action": "reserve_hotel",
        "params": {},
        "session_id": "integration_pipeline_reserve",
        "request_id": "integration_pipeline_reserve",
    }

    try:
        search_status, search_data = await post_execute(search_payload)
        select_status, select_data = await post_execute(select_payload)
        reserve_status, reserve_data = await post_execute(reserve_payload)
    except aiohttp.ClientConnectorError:
        pytest.skip("Browser service is not running. Start `python main_browser_service.py` first.")

    assert search_status == 200
    assert search_data["success"] is True
    assert select_status == 200
    assert select_data["success"] is True
    assert reserve_status == 200
    assert reserve_data["success"] is True
    assert "i opened" in select_data["result"].lower()
    assert "booking flow" in reserve_data["result"].lower()
    assert "personal information" in reserve_data["result"].lower()
