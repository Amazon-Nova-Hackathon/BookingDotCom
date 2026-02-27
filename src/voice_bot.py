# -*- coding: utf-8 -*-
import asyncio
import os
import uuid
from loguru import logger
import aiohttp
from aiohttp import web
from dotenv import load_dotenv

from pipecat.audio.vad.silero import SileroVADAnalyzer
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
)
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection

from src.prompts import SYSTEM_PROMPT

import json
load_dotenv(override=True)

BROWSER_SERVICE_URL = os.getenv("BROWSER_SERVICE_URL", "http://localhost:7863")

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


class TranscriptionLogger(FrameProcessor):
    """Logs speech transcriptions to terminal + broadcasts SSE events."""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame) and frame.text.strip():
            logger.info(f"🎙️  USER  → '{frame.text}'")
            await broadcast_event("user_transcript", {"text": frame.text})
        elif isinstance(frame, TTSTextFrame) and frame.text.strip():
            logger.info(f"🤖  BOT   → '{frame.text}'")
            await broadcast_event("bot_response", {"text": frame.text})

        await self.push_frame(frame, direction)

async def search_hotel_tool(function_name, tool_call_id, args, llm, context, result_callback):
    """Callback invoked when the LLM calls the 'search_hotel' tool."""
    logger.info(f"Tool 'search_hotel' called with args: {args}")
    session_id = str(uuid.uuid4())

    # Broadcast extraction event so the UI can show the filled-in form
    await broadcast_event("tool_called", {"args": args})

    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                "action": "search_hotel",
                "params": args,
                "session_id": session_id,
                "request_id": session_id,
            }
            logger.info("Forwarding to Browser Agent Service...")
            async with session.post(
                f"{BROWSER_SERVICE_URL}/api/execute", json=payload, timeout=aiohttp.ClientTimeout(total=120)
            ) as response:
                result_json = await response.json()
                if result_json.get("success"):
                    text_result = result_json.get("result", "Search completed.")
                else:
                    text_result = f"Browser agent error: {result_json.get('error', 'unknown')}"
                await broadcast_event("tool_result", {"result": text_result})
                await result_callback({"result": text_result})
    except Exception as e:
        logger.error(f"Error calling Browser Service: {e}")
        err_msg = f"Browser service unavailable: {str(e)}"
        await broadcast_event("tool_result", {"error": err_msg})
        await result_callback({"error": err_msg})



async def run_pipeline_for_connection(webrtc_connection: SmallWebRTCConnection):
    """Spin up a full Pipecat pipeline for one WebRTC peer connection."""
    logger.info(f"Starting pipeline for pc_id: {webrtc_connection.pc_id}")

    # --- Nova Sonic LLM ---
    llm = AWSNovaSonicLLMService(
        model=os.getenv("BEDROCK_MODEL_ID", "amazon.nova-2-sonic-v1:0"),
        access_key_id=os.getenv("AWS_ACCESS_KEY_ID", ""),
        secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", ""),
        region=os.getenv("AWS_REGION", os.getenv("BEDROCK_REGION", "us-east-1")),
        session_token=os.getenv("AWS_SESSION_TOKEN") or None,
        system_instruction=SYSTEM_PROMPT,  # pass directly → no need to wait for LLMContextFrame
        params=NovaSonicParams(),
    )

    # Register handler (just the name + callback, no extra kwargs)
    llm.register_function("search_hotel", search_hotel_tool)

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
            vad_analyzer=SileroVADAnalyzer(),
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
            llm,                         # Nova Sonic S2S handles audio directly
            TranscriptionLogger(),       # logs USER + BOT text to terminal
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
    app.router.add_get("/events", handle_events)   # SSE stream

    return app

