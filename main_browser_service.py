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
import traceback

from aiohttp import web
from aiohttp.web import RouteTableDef
from dotenv import load_dotenv
from loguru import logger

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
from src.browser_agent import browser_agent

load_dotenv(override=True)

routes = RouteTableDef()


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

        agent_result = await browser_agent.execute_action(
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


app = web.Application()
app.add_routes(routes)

if __name__ == "__main__":
    PORT = int(os.getenv("PORT", "7863"))
    HOST = os.getenv("HOST", "0.0.0.0")
    logger.info(f"Starting Browser Agent Service on {HOST}:{PORT}")
    web.run_app(app, host=HOST, port=PORT)
