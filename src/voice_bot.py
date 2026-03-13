# -*- coding: utf-8 -*-
import asyncio
import re
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
    FunctionCallResultProperties,
    LLMFullResponseStartFrame,
    LLMTextFrame,
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
_inflight_browser_actions: dict[str, asyncio.Task] = {}
_recent_browser_action_results: dict[str, tuple[float, dict]] = {}
_BROWSER_ACTION_DEDUPE_TTL = 2.0
_READY_TOKEN_RE = re.compile(r"(?:(?<=^)|(?<=[\s,.;:!?-]))ready(?!\s+to\b)[.!?]?(?=$|[\s,.;:!?-])", re.IGNORECASE)
_DIGIT_WORDS = {
    "zero": "0",
    "oh": "0",
    "o": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
}
_DIGIT_SEQUENCE_RE = re.compile(
    r"(?i)\b(?:zero|oh|o|one|two|three|four|five|six|seven|eight|nine)\b"
    r"(?:[\s,.;:\-]+\b(?:zero|oh|o|one|two|three|four|five|six|seven|eight|nine)\b){2,}"
)
_latest_user_transcript: str = ""

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


def _browser_action_fingerprint(action: str, args: dict) -> str:
    normalized_args = json.dumps(args or {}, sort_keys=True, separators=(",", ":"), default=str)
    return f"{action}:{normalized_args}"


def _clean_user_transcript_text(text: str) -> str:
    def replace_digit_sequence(match: re.Match[str]) -> str:
        words = re.findall(r"(?i)\b(?:zero|oh|o|one|two|three|four|five|six|seven|eight|nine)\b", match.group(0))
        return "".join(_DIGIT_WORDS[word.lower()] for word in words)

    cleaned = _READY_TOKEN_RE.sub(" ", text or "")
    cleaned = _DIGIT_SEQUENCE_RE.sub(replace_digit_sequence, cleaned)
    cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip()


def _user_explicitly_wants_reservation() -> bool:
    return bool(re.search(r"\b(book|booking|reserve|reservation|proceed with booking|go ahead and book)\b", _latest_user_transcript, re.IGNORECASE))


def _user_explicitly_wants_to_continue() -> bool:
    return bool(re.search(r"\b(continue|next|next page|go on|proceed|payment|pay|move on)\b", _latest_user_transcript, re.IGNORECASE))

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
        self._saw_llm_text_this_turn = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if (
            self._capture_user
            and direction == FrameDirection.UPSTREAM
            and isinstance(frame, TranscriptionFrame)
            and frame.text.strip()
        ):
            cleaned_text = _clean_user_transcript_text(frame.text)
            if cleaned_text:
                global _latest_user_transcript
                _latest_user_transcript = cleaned_text
                logger.info(f"USER -> '{cleaned_text}'")
                await broadcast_event("user_transcript", {"text": cleaned_text})
        elif (
            self._capture_bot
            and direction == FrameDirection.DOWNSTREAM
            and isinstance(frame, LLMFullResponseStartFrame)
        ):
            self._saw_llm_text_this_turn = False
            await broadcast_event("bot_response_start", {})
        elif (
            self._capture_bot
            and direction == FrameDirection.DOWNSTREAM
            and isinstance(frame, LLMTextFrame)
            and frame.text.strip()
        ):
            self._saw_llm_text_this_turn = True
            logger.info(f"BOT TEXT -> '{frame.text}'")
            await broadcast_event("bot_response", {"text": frame.text})
        elif (
            self._capture_bot
            and direction == FrameDirection.DOWNSTREAM
            and isinstance(frame, TTSTextFrame)
            and frame.text.strip()
        ):
            if self._saw_llm_text_this_turn:
                await self.push_frame(frame, direction)
                return
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

    async def _report_user_transcription_ended(self):
        text = _clean_user_transcript_text((self._user_text_buffer or "").strip())
        self._user_text_buffer = text

        await super()._report_user_transcription_ended()


async def invoke_browser_action(action: str, args: dict, result_callback):
    fingerprint = _browser_action_fingerprint(action, args)
    now = time.monotonic()
    cached = _recent_browser_action_results.get(fingerprint)
    if cached and (now - cached[0]) <= _BROWSER_ACTION_DEDUPE_TTL:
        logger.info(f"Reusing recent result for duplicate action '{action}'.")
        await result_callback(
            {"duplicate_tool_call": True},
            properties=FunctionCallResultProperties(run_llm=False),
        )
        return

    inflight_task = _inflight_browser_actions.get(fingerprint)
    if inflight_task:
        logger.info(f"Joining in-flight duplicate action '{action}'.")
        await inflight_task
        await result_callback(
            {"duplicate_tool_call": True},
            properties=FunctionCallResultProperties(run_llm=False),
        )
        return

    session_id = str(uuid.uuid4())
    await broadcast_event("tool_called", {"action": action, "args": args})

    async def run_browser_request():
        try:
            session = await get_http_session()
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
                    return {"success": True, "result": text_result}
                err_msg = f"Browser agent error: {result_json.get('error', 'unknown')}"
                return {"success": False, "error": err_msg}
        except Exception as e:
            logger.error(f"Error calling Browser Service for action '{action}': {e}")
            return {"success": False, "error": f"Browser service unavailable: {str(e)}"}

    task = asyncio.create_task(run_browser_request())
    _inflight_browser_actions[fingerprint] = task

    try:
        outcome = await task
        _recent_browser_action_results[fingerprint] = (time.monotonic(), outcome)
        if outcome.get("success"):
            await broadcast_event("tool_result", {"action": action, "result": outcome["result"]})
            await result_callback(
                {"result": outcome["result"]},
                properties=FunctionCallResultProperties(run_llm=True),
            )
        else:
            await broadcast_event("tool_result", {"action": action, "error": outcome["error"]})
            await result_callback(
                {"error": outcome["error"]},
                properties=FunctionCallResultProperties(run_llm=True),
            )
    finally:
        current_task = _inflight_browser_actions.get(fingerprint)
        if current_task is task:
            _inflight_browser_actions.pop(fingerprint, None)

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
    if not _user_explicitly_wants_reservation():
        logger.info("Skipping reserve_hotel because the latest user utterance did not explicitly ask to book.")
        await result_callback(
            {"result": "The user has not explicitly asked to reserve the hotel yet."},
            properties=FunctionCallResultProperties(run_llm=False),
        )
        return
    await invoke_browser_action("reserve_hotel", args, result_callback)


async def fill_guest_info_tool(function_name, tool_call_id, args, llm, context, result_callback):
    """Callback invoked when the LLM calls the 'fill_guest_info' tool."""
    logger.info(f"Tool 'fill_guest_info' called with args: {args}")
    await invoke_browser_action("fill_guest_info", args, result_callback)


async def continue_to_payment_tool(function_name, tool_call_id, args, llm, context, result_callback):
    """Callback invoked when the LLM calls the 'continue_to_payment' tool."""
    logger.info(f"Tool 'continue_to_payment' called with args: {args}")
    if not _user_explicitly_wants_to_continue():
        logger.info("Skipping continue_to_payment because the latest user utterance did not explicitly ask to continue.")
        await result_callback(
            {"result": "The user has not explicitly asked to continue to the next step yet."},
            properties=FunctionCallResultProperties(run_llm=False),
        )
        return
    await invoke_browser_action("continue_to_payment", args, result_callback)



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
        params=NovaSonicParams(),
    )

    # Register handler (just the name + callback, no extra kwargs)
    llm.register_function("search_hotel", search_hotel_tool)
    llm.register_function("select_hotel", select_hotel_tool)
    llm.register_function("reserve_hotel", reserve_hotel_tool)
    llm.register_function("fill_guest_info", fill_guest_info_tool)
    llm.register_function("continue_to_payment", continue_to_payment_tool)

    # Define tool schema for Nova Sonic via ToolsSchema
    tools = ToolsSchema(standard_tools=[
        FunctionSchema(
            name="search_hotel",
            description="Searches for available hotels on Booking.com.",
            properties={
                "destination": {"type": "string", "description": "City or hotel name to search for. Optional for result refinements if the current destination should stay the same."},
                "checkin_date": {"type": "string", "description": "Check-in date in YYYY-MM-DD format. Optional for result refinements if unchanged."},
                "checkout_date": {"type": "string", "description": "Check-out date in YYYY-MM-DD format. Optional for result refinements if unchanged."},
                "adults": {"type": "integer", "description": "Number of adult guests. Optional for result refinements if unchanged."},
                "children": {"type": "integer", "description": "Number of children. Use 0 if there are no children."},
                "children_ages": {
                    "type": "array",
                    "description": "Ages of the children, in order, if the user provides them.",
                    "items": {"type": "integer"},
                },
                "rooms": {"type": "integer", "description": "Number of rooms to search for. Use 1 if unspecified."},
            },
            required=[],
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
        ),
        FunctionSchema(
            name="fill_guest_info",
            description="Fills the visible Booking.com guest-information form with full name, email, region, and phone number, and can also select named optional choices shown on the page.",
            properties={
                "full_name": {"type": "string", "description": "Guest full name"},
                "first_name": {"type": "string", "description": "Guest first name if provided separately"},
                "last_name": {"type": "string", "description": "Guest last name if provided separately"},
                "email": {"type": "string", "description": "Guest email address"},
                "phone": {"type": "string", "description": "Guest phone number"},
                "region": {"type": "string", "description": "Country or region shown in the booking form"},
                "address_line1": {"type": "string", "description": "Address line 1 if the form asks for it"},
                "address_line2": {"type": "string", "description": "Address line 2 if the form asks for it"},
                "city": {"type": "string", "description": "City if the form asks for it"},
                "optional_choices": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional checkbox, radio, or select choices the user wants selected on the booking form",
                },
                "arrival_time": {"type": "string", "description": "Arrival or check-in time if the page asks for it"},
                "special_requests": {"type": "string", "description": "Special requests or notes for the stay"},
            },
            required=[],
        ),
        FunctionSchema(
            name="continue_to_payment",
            description="Clicks the next booking button after the guest confirms the visible form is complete, then reports whether the payment or final-details step opened.",
            properties={},
            required=[],
        )
    ])

    # --- Context & Aggregators ---
    # Nova Sonic's Pipecat adapter crashes when messages=[] because its
    # ConvertedMessages helper incorrectly requires a positional `messages`
    # arg in that path. Keep the system prompt in context so the adapter has
    # a valid first message and can extract the instruction normally.
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
                    start_secs=0.12,
                    stop_secs=0.25,
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

