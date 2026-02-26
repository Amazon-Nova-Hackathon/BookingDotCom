# -*- coding: utf-8 -*-
"""
Browser Agent - Booking.com Automation
Uses browser-use with AWS Bedrock Nova Lite 2 and Playwright
to automate hotel search, filtering, selection, and booking on Booking.com.
"""
import asyncio
import os
import traceback
from datetime import datetime

from dotenv import load_dotenv
from loguru import logger

load_dotenv(override=True)

# Browser-Use and LLM imports
from browser_use import Agent as BrowserUseAgent, Browser
from browser_use.llm.aws import ChatAWSBedrock

# Bedrock credentials for Nova Lite 2
# Supports explicit keys or BEDROCK_PROFILE / AWS_PROFILE
BEDROCK_ACCESS_KEY_ID = os.getenv("BEDROCK_ACCESS_KEY_ID", os.getenv("AWS_ACCESS_KEY_ID"))
BEDROCK_SECRET_ACCESS_KEY = os.getenv("BEDROCK_SECRET_ACCESS_KEY", os.getenv("AWS_SECRET_ACCESS_KEY"))
BEDROCK_PROFILE = os.getenv("BEDROCK_PROFILE", os.getenv("AWS_PROFILE"))
BEDROCK_REGION = os.getenv("BEDROCK_REGION", "ap-southeast-1")
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "amazon.nova-2-lite-v1:0")

BOOKING_URL = os.getenv("BOOKING_URL", "https://www.booking.com")


class BrowserAgentHandler:
    """
    Handles browser automation for Booking.com.
    Each action corresponds to a tool call from Nova Sonic 2.
    """

    def __init__(self):
        self.browser: Browser | None = None
        self.llm = None
        self.sessions: dict = {}  # session_id -> session state
        self._initialized = False

    async def _ensure_initialized(self):
        """Lazy initialization of browser and LLM."""
        if self._initialized:
            return

        logger.info("Initializing Browser Agent...")
        # ... rest of the file ...
