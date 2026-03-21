#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Browser Agent Service - Standalone HTTP Server
Listens for requests from Voice Bot and performs browser automation
on Booking.com using browser-use with AWS Bedrock Nova Lite 2.
"""
import asyncio
import os
import sys
import json
import time
import traceback

from aiohttp import web
from aiohttp.web import RouteTableDef
from dotenv import load_dotenv
from loguru import logger

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
from src.playwright_agent import booking_agent

load_dotenv(override=True)

routes = RouteTableDef()

# Screenshot debounce: avoid calling take_screenshot more than once per 300ms
_cached_screenshot: bytes | None = None
_cached_screenshot_time: float = 0.0
_SCREENSHOT_MIN_INTERVAL = 0.3


@routes.post("/api/execute")
async def execute_action(request):
    """Execute a booking action from the voice bot."""
    try:
        data = await request.json()
        action = data.get("action", "")
        params = data.get("params", {})
        session_id = data.get("session_id", "")
        request_id = data.get("request_id", "unknown")

        logger.info(
            f"[{request_id}] Received action: {action} | session: {session_id}"
        )
        logger.debug(f"[{request_id}] Params: {json.dumps(params, default=str)}")

        agent_result = await booking_agent.execute_action(
            action=action,
            params=params,
            session_id=session_id,
        )

        if agent_result.get("success"):
            final_message = agent_result.get("result", "Action completed successfully.")
            logger.info(f"[{request_id}] Success: {final_message[:200]}")
            return web.json_response({"success": True, "result": final_message})
        else:
            error_message = agent_result.get("error", "Unknown error.")
            logger.error(f"[{request_id}] Error: {error_message}")
            return web.json_response({"success": False, "error": error_message}, status=400)

    except Exception as e:
        logger.error(f"[{request_id}] Exception: {str(e)}\n{traceback.format_exc()}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


@routes.get("/api/health")
async def health_check(request):
    return web.json_response({"status": "ok"})


@routes.get("/screenshot")
async def get_screenshot(request):
    """
    Return the latest Playwright screenshot as PNG.
    Debounced: serves cached screenshot if called within 300ms.
    """
    global _cached_screenshot, _cached_screenshot_time
    now = time.monotonic()

    png = booking_agent.get_screenshot()
    if png is None:
        return web.Response(status=204)

    # Only update cache if enough time has passed
    if (now - _cached_screenshot_time) >= _SCREENSHOT_MIN_INTERVAL:
        _cached_screenshot = png
        _cached_screenshot_time = now

    return web.Response(
        body=_cached_screenshot or png,
        content_type="image/png",
        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"},
    )


@routes.get("/ws/browser")
async def ws_browser(request):
    """
    WebSocket endpoint for CDP screencast streaming + user input forwarding.
    Server → Client: {"type": "frame", "data": "<base64 JPEG>"}
    Client → Server: {"type": "click"|"type"|"keypress"|"scroll", ...}
    """
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    logger.info("[WS] Browser WebSocket connected")

    if not booking_agent.is_running:
        logger.error("[WS] Browser not initialized — rejecting")
        await ws.close()
        return ws

    # Define callback to push frames
    async def push_frame(base64_jpeg: str):
        if not ws.closed:
            try:
                await ws.send_json({"type": "frame", "data": base64_jpeg})
            except Exception:
                pass

    # Start CDP screencast
    await booking_agent.start_screencast(push_frame)

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    action = data.get("type")
                    if action == "click":
                        await booking_agent.cdp_click(int(data["x"]), int(data["y"]))
                    elif action == "type":
                        await booking_agent.cdp_type(data.get("text", ""))
                    elif action == "keypress":
                        await booking_agent.cdp_keypress(data.get("key", ""))
                    elif action == "scroll":
                        await booking_agent.cdp_scroll(
                            int(data.get("x", 0)), int(data.get("y", 0)),
                            int(data.get("deltaX", 0)), int(data.get("deltaY", 0)),
                        )
                    elif action == "mousemove":
                        await booking_agent.cdp_mousemove(int(data["x"]), int(data["y"]))
                except Exception as e:
                    logger.warning(f"[WS] Input error: {e}")
            elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                break
    except Exception as e:
        logger.warning(f"[WS] Connection error: {e}")
    finally:
        await booking_agent.stop_screencast()
        logger.info("[WS] Browser WebSocket disconnected")

    return ws


@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        return web.Response(headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        })
    response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


async def on_startup(app):
    """Pre-initialize Playwright browser so it's ready before any request."""
    logger.info("Pre-initializing browser...")
    await booking_agent.init_browser()
    logger.info("Browser ready — accepting connections.")


app = web.Application(middlewares=[cors_middleware])
app.on_startup.append(on_startup)
app.add_routes(routes)

if __name__ == "__main__":
    PORT = int(os.getenv("BROWSER_PORT", "7863"))
    HOST = os.getenv("HOST", "0.0.0.0")
    logger.info(f"Starting Browser Agent Service on {HOST}:{PORT}")
    web.run_app(app, host=HOST, port=PORT)

