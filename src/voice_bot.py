# -*- coding: utf-8 -*-
"""
Voice Bot - Booking.com Hotel Booking Agent
Voice interface with WebRTC and AWS Nova Sonic 2 Speech-to-Speech.
Uses function calling to trigger browser automation actions.
"""
import asyncio
import os
import json
import uuid
from datetime import datetime

import aiohttp
from aiohttp import web
from aiohttp.web import RouteTableDef
from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer, VADParams
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.pipeline.runner import PipelineRunner
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection, IceServer
from pipecat.transports.base_transport import TransportParams
from pipecat.services.aws.nova_sonic.llm import AWSNovaSonicLLMService, Params as NovaSonicParams
from pipecat.services.llm_service import FunctionCallParams
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.frames.frames import (
    Frame,
    LLMRunFrame,
    TextFrame,
    TranscriptionFrame,
)

from src.prompts import SYSTEM_PROMPT

load_dotenv(override=True)

# Browser Agent Service URL
BROWSER_SERVICE_URL = os.getenv("BROWSER_SERVICE_URL", "http://localhost:7863")

# AWS credentials for Nova Sonic 2
# Supports explicit keys or AWS_PROFILE
aws_region = os.getenv("AWS_REGION", "ap-northeast-2")
aws_profile = os.getenv("AWS_PROFILE")
aws_access_key = os.getenv("AWS_ACCESS_KEY_ID")
aws_secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
aws_session_token = os.getenv("AWS_SESSION_TOKEN")

# ... rest of the file ...
