# -*- coding: utf-8 -*-
import asyncio
import os
import time
import uuid
from loguru import logger
import aiohttp
from aiohttp import web
from dotenv import load_dotenv

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.pipeline.runner import PipelineRunner
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCRequestHandler,
    SmallWebRTCRequest,
    SmallWebRTCPatchRequest,
    IceCandidate,
)
from pipecat.transports.smallwebrtc.connection import IceServer
from pipecat.services.aws.nova_sonic.llm import AWSNovaSonicLLMService, Params as NovaSonicParams
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.frames.frames import LLMContextFrame
from pipecat.frames.frames import (
    Frame,
    TextFrame,
    TranscriptionFrame,
    TTSTextFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection

from src.prompts import SYSTEM_PROMPT

import json
load_dotenv(override=True)

BROWSER_SERVICE_URL = os.getenv("BROWSER_SERVICE_URL", "http://localhost:7863")

# Shared aiohttp session for browser service proxying (reused across requests)
_http_session: aiohttp.ClientSession | None = None

# Screenshot cache to avoid hammering browser service
_screenshot_cache: bytes | None = None
_screenshot_cache_time: float = 0.0
_SCREENSHOT_CACHE_TTL = 0.3  # serve cached screenshot for 300ms

async def get_http_session() -> aiohttp.ClientSession:
    """Get or create the shared aiohttp session."""
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()
    return _http_session

# Global WebRTC request handler (manages all peer connections)
webrtc_handler = SmallWebRTCRequestHandler(
    ice_servers=[
        IceServer(urls=["stun:stun.l.google.com:19302"]),
    ]
)

# ── SSE event broadcast ────────────────────────────────────────────────────────
# All connected SSE clients subscribe here
_sse_clients: list[asyncio.Queue] = []

async def broadcast_event(event_type: str, data: dict):
    """Push a JSON event to every connected SSE client."""
    payload = json.dumps({"type": event_type, **data})
    for q in list(_sse_clients):
        await q.put(payload)


class ConversationEventLogger(FrameProcessor):
    """Broadcasts transcript events from the correct side of the LLM."""

    def __init__(self, *, capture_user: bool = False, capture_bot: bool = False):
        super().__init__()
        self._capture_user = capture_user
        self._capture_bot = capture_bot

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if (
            self._capture_user
            and direction == FrameDirection.UPSTREAM
            and isinstance(frame, TranscriptionFrame)
            and frame.text.strip()
        ):
            logger.info(f"🎙️  USER  → '{frame.text}'")
            await broadcast_event("user_transcript", {"text": frame.text})
        elif (
            self._capture_bot
            and direction == FrameDirection.DOWNSTREAM
            and isinstance(frame, TTSTextFrame)
            and frame.text.strip()
        ):
            logger.info(f"🤖  BOT   → '{frame.text}'")
            await broadcast_event("bot_response", {"text": frame.text})

        await self.push_frame(frame, direction)


class AssistantTurnTrigger(FrameProcessor):
    """Triggers Nova Sonic to answer when the user finishes speaking."""

    def __init__(self, llm):
        super().__init__()
        self._llm = llm

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, UserStoppedSpeakingFrame):
            await self._llm.trigger_assistant_response()

        await self.push_frame(frame, direction)


class ResilientAWSNovaSonicLLMService(AWSNovaSonicLLMService):
    """Suppresses expected closed-stream races during disconnect."""

    @staticmethod
    def _is_closed_stream_error(exc: BaseException) -> bool:
        messages = [str(exc).lower()]
        cause = exc.__cause__
        while cause:
            messages.append(str(cause).lower())
            cause = cause.__cause__

        return any(
            token in message
            for message in messages
            for token in (
                "closed stream",
                "failed to write to stream",
                "closed or closing provider",
                "attempted to write to a closed",
            )
        )

    async def _send_client_event(self, event_json: str):
        if self._disconnecting or not self._stream:
            return

        try:
            await super()._send_client_event(event_json)
        except Exception as exc:
            if not self._is_closed_stream_error(exc):
                raise

            # The WebRTC side can disconnect while a few buffered mic frames are
            # still draining through the pipeline. Treat that as a normal teardown.
            logger.warning("Nova Sonic stream is already closing; dropping late audio/event frame.")
            self._disconnecting = True
            self._stream = None


async def invoke_browser_action(action: str, args: dict, result_callback):
    session_id = str(uuid.uuid4())
    await broadcast_event("tool_called", {"action": action, "args": args})

    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                "action": action,
                "params": args,
                "session_id": session_id,
                "request_id": session_id,
            }
            logger.info(f"Forwarding action '{action}' to Browser Agent Service...")
            async with session.post(
                f"{BROWSER_SERVICE_URL}/api/execute", json=payload, timeout=aiohttp.ClientTimeout(total=120)
            ) as response:
                result_json = await response.json()
                if result_json.get("success"):
                    text_result = result_json.get("result", "Action completed.")
                    await broadcast_event("tool_result", {"action": action, "result": text_result})
                    await result_callback({"result": text_result})
                else:
                    err_msg = f"Browser agent error: {result_json.get('error', 'unknown')}"
                    await broadcast_event("tool_result", {"action": action, "error": err_msg})
                    await result_callback({"error": err_msg})
    except Exception as e:
        logger.error(f"Error calling Browser Service for action '{action}': {e}")
        err_msg = f"Browser service unavailable: {str(e)}"
        await broadcast_event("tool_result", {"action": action, "error": err_msg})
        await result_callback({"error": err_msg})

async def search_hotel_tool(function_name, tool_call_id, args, llm, context, result_callback):
    """Callback invoked when the LLM calls the 'search_hotel' tool."""
    logger.info(f"Tool 'search_hotel' called with args: {args}")
    await invoke_browser_action("search_hotel", args, result_callback)


async def select_hotel_tool(function_name, tool_call_id, args, llm, context, result_callback):
    """Callback invoked when the LLM calls the 'select_hotel' tool."""
    logger.info(f"Tool 'select_hotel' called with args: {args}")
    await invoke_browser_action("select_hotel", args, result_callback)


async def reserve_hotel_tool(function_name, tool_call_id, args, llm, context, result_callback):
    """Callback invoked when the LLM calls the 'reserve_hotel' tool."""
    logger.info(f"Tool 'reserve_hotel' called with args: {args}")
    await invoke_browser_action("reserve_hotel", args, result_callback)



async def run_pipeline_for_connection(webrtc_connection: SmallWebRTCConnection):
    """Spin up a full Pipecat pipeline for one WebRTC peer connection."""
    logger.info(f"Starting pipeline for pc_id: {webrtc_connection.pc_id}")

    # --- Nova Sonic LLM ---
    nova_region = os.getenv("NOVA_SONIC_REGION", "us-east-1")
    logger.info(f"Nova Sonic region: {nova_region}, model: {os.getenv('NOVA_SONIC_MODEL_ID', 'amazon.nova-2-sonic-v1:0')}")
    llm = ResilientAWSNovaSonicLLMService(
        model=os.getenv("NOVA_SONIC_MODEL_ID", "amazon.nova-2-sonic-v1:0"),
        access_key_id=os.getenv("AWS_ACCESS_KEY_ID", ""),
        secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", ""),
        region=nova_region,
        session_token=os.getenv("AWS_SESSION_TOKEN") or None,
        system_instruction=SYSTEM_PROMPT,  # pass directly → no need to wait for LLMContextFrame
        params=NovaSonicParams(),
    )

    # Register handler (just the name + callback, no extra kwargs)
    llm.register_function("search_hotel", search_hotel_tool)
    llm.register_function("select_hotel", select_hotel_tool)
    llm.register_function("reserve_hotel", reserve_hotel_tool)

    # Define tool schema for Nova Sonic via ToolsSchema
    tools = ToolsSchema(standard_tools=[
        FunctionSchema(
            name="search_hotel",
            description="Searches for available hotels on Booking.com.",
            properties={
                "destination": {"type": "string", "description": "City or hotel name to search for"},
                "checkin_date": {"type": "string", "description": "Check-in date in YYYY-MM-DD format"},
                "checkout_date": {"type": "string", "description": "Check-out date in YYYY-MM-DD format"},
                "adults": {"type": "integer", "description": "Number of adult guests"},
            },
            required=["destination", "checkin_date", "checkout_date", "adults"],
        ),
        FunctionSchema(
            name="select_hotel",
            description="Opens a hotel detail page on Booking.com and returns a short summary of the selected hotel.",
            properties={
                "hotel_name": {"type": "string", "description": "Hotel name chosen by the user, if they mentioned one"},
                "hotel_index": {"type": "integer", "description": "1-based index of the hotel option if the user says first, second, third, etc."},
            },
            required=[],
        ),
        FunctionSchema(
            name="reserve_hotel",
            description="Clicks the booking or reserve button for the current hotel and moves into the guest-information step.",
            properties={
                "hotel_name": {"type": "string", "description": "Hotel name chosen by the user, if they mentioned one"},
                "hotel_index": {"type": "integer", "description": "1-based index of the hotel option if the user says first, second, third, etc."},
            },
            required=[],
        )
    ])

    # --- Context & Aggregators ---
    # NOTE: The system prompt must be in LLMContext as a "system" role message.
    # If messages=[], the AWSNovaSonicLLMAdapter hits a bug (line 124 of adapter):
    #   return self.ConvertedMessages()  ← missing required 'messages' positional arg
    context = LLMContext(
        messages=[{"role": "system", "content": [{"text": SYSTEM_PROMPT}]}],
        tools=tools,
    )
    context_aggregator = LLMContextAggregatorPair(context)

    # --- Transport (WebRTC) ---
    transport = SmallWebRTCTransport(
        webrtc_connection=webrtc_connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_enabled=True,
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(
                    confidence=0.65,
                    start_secs=0.15,
                    stop_secs=0.35,
                    min_volume=0.45,
                )
            ),
        ),
    )

    # --- Pipeline ---
    # NOTE: Do NOT include context_aggregator.user() here for Nova Sonic S2S.
    # The LLMUserAggregator consumes UserStartedSpeakingFrame and
    # UserStoppedSpeakingFrame internally without forwarding them downstream,
    # so Nova Sonic never receives the signal that the user finished speaking
    # and never generates a response.
    # For S2S, audio frames must flow directly: transport → Nova Sonic.
    pipeline = Pipeline(
        [
            transport.input(),
            AssistantTurnTrigger(llm),
            ConversationEventLogger(capture_user=True),
            llm,                         # Nova Sonic S2S handles audio directly
            ConversationEventLogger(capture_bot=True),
            transport.output(),
            context_aggregator.assistant(),  # keeps conversation history
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
    )

    @transport.event_handler("on_client_connected")
    async def on_connected(*args, **kwargs):
        logger.info("Client connected — pushing initial context to unblock Nova Sonic.")
        # ⚡ Push an empty LLMContextFrame so Nova Sonic can finish its setup
        # (open audio input, start receive loop) without waiting for the first user utterance.
        await task.queue_frames([LLMContextFrame(context=context)])

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(*args, **kwargs):
        logger.info("Client disconnected — cancelling task.")
        await task.cancel()

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)


# ─── HTTP Endpoints ────────────────────────────────────────────────────────────

async def handle_offer(request: web.Request) -> web.Response:
    """
    Endpoint: POST /offer
    Frontend sends its WebRTC SDP offer here. We return the server's SDP answer.
    """
    try:
        body = await request.json()
        webrtc_request = SmallWebRTCRequest(
            sdp=body["sdp"],
            type=body["type"],
            pc_id=body.get("pc_id"),
            restart_pc=body.get("restart_pc", False),
        )

        # ⚡ Run pipeline as a BACKGROUND TASK so we can return the SDP answer
        # immediately. Without this, the HTTP response would be delayed until the
        # entire pipeline finishes (~60s), causing the browser's ICE candidates to
        # arrive after the server already closed the connection.
        async def pipeline_callback(webrtc_connection):
            asyncio.create_task(run_pipeline_for_connection(webrtc_connection))

        answer = await webrtc_handler.handle_web_request(
            webrtc_request, pipeline_callback
        )
        logger.debug(f"SDP Answer returned immediately for pc_id={answer['pc_id']}")
        return web.json_response(answer)
    except Exception as e:
        logger.error(f"/offer error: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def handle_ice(request: web.Request) -> web.Response:
    """
    Endpoint: PATCH /offer
    Frontend sends ICE candidates here after the initial offer/answer exchange.
    """
    try:
        body = await request.json()
        pc_id = body.get("pc_id")
        candidates_raw = body.get("candidates", [])
        candidates = [
            IceCandidate(
                candidate=c["candidate"],
                sdp_mid=c["sdpMid"],
                sdp_mline_index=c["sdpMLineIndex"],
            )
            for c in candidates_raw
        ]
        patch_request = SmallWebRTCPatchRequest(pc_id=pc_id, candidates=candidates)
        await webrtc_handler.handle_patch_request(patch_request)
        return web.json_response({"ok": True})
    except Exception as e:
        logger.error(f"PATCH /offer error: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def handle_screenshot(request: web.Request) -> web.Response:
    """Proxy screenshots from browser service with caching to reduce load."""
    global _screenshot_cache, _screenshot_cache_time
    now = time.monotonic()

    # Serve cached screenshot if still fresh
    if _screenshot_cache and (now - _screenshot_cache_time) < _SCREENSHOT_CACHE_TTL:
        return web.Response(
            body=_screenshot_cache,
            content_type="image/png",
            headers={"Cache-Control": "no-cache"},
        )

    try:
        session = await get_http_session()
        async with session.get(
            f"{BROWSER_SERVICE_URL}/screenshot",
            timeout=aiohttp.ClientTimeout(total=3),
        ) as resp:
            if resp.status == 204:
                return web.Response(status=204)
            body = await resp.read()
            # Cache it
            _screenshot_cache = body
            _screenshot_cache_time = now
            return web.Response(
                body=body,
                content_type="image/png",
                headers={"Cache-Control": "no-cache"},
            )
    except Exception:
        # Return cached if available, else 204
        if _screenshot_cache:
            return web.Response(
                body=_screenshot_cache,
                content_type="image/png",
                headers={"Cache-Control": "no-cache"},
            )
        return web.Response(status=204)


async def handle_browser_interact(request: web.Request) -> web.Response:
    """Proxy user browser interactions (click/scroll/type) to browser service."""
    try:
        data = await request.json()
        session = await get_http_session()
        async with session.post(
            f"{BROWSER_SERVICE_URL}/api/interact",
            json=data,
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            body = await resp.json()
            return web.json_response(body, status=resp.status)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def handle_events(request: web.Request) -> web.StreamResponse:
    """
    Endpoint: GET /events
    Server-Sent Events stream. The frontend subscribes here to receive
    real-time updates: user transcripts, bot responses, tool calls, results.
    """
    response = web.StreamResponse()
    response.headers["Content-Type"] = "text/event-stream"
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["Access-Control-Allow-Origin"] = "*"
    await response.prepare(request)

    queue: asyncio.Queue = asyncio.Queue()
    _sse_clients.append(queue)
    logger.info(f"SSE client connected ({len(_sse_clients)} total)")

    try:
        while True:
            payload = await queue.get()
            await response.write(f"data: {payload}\n\n".encode())
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        _sse_clients.remove(queue)
        logger.info(f"SSE client disconnected ({len(_sse_clients)} remaining)")

    return response


def create_app():
    app = web.Application()

    async def cleanup(app):
        global _http_session
        if _http_session and not _http_session.closed:
            await _http_session.close()
            _http_session = None

    app.on_cleanup.append(cleanup)

    # Serve built React frontend
    frontend_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "frontend", "dist"
    )
    if os.path.isdir(frontend_path):
        async def index_handler(request):
            return web.FileResponse(os.path.join(frontend_path, "index.html"))
        app.router.add_get("/", index_handler)
        app.router.add_static("/assets", os.path.join(frontend_path, "assets"))

    # WebRTC signaling endpoints
    app.router.add_post("/offer", handle_offer)
    app.router.add_patch("/offer", handle_ice)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/screenshot", handle_screenshot)  # Proxy from browser service
    app.router.add_post("/browser-interact", handle_browser_interact)  # Proxy interactions
    app.router.add_get("/events", handle_events)   # SSE stream

    return app

