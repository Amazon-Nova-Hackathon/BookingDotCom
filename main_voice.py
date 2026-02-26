#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Voice Bot Service - Entry Point
Standalone service running the Nova Sonic 2 S2S voice pipeline.
Communicates with Browser Agent Service via HTTP API.
"""
import sys
import os

# Filter ONNX Runtime GPU warning (harmless on CPU-only systems)
class FilteredStderr:
    def __init__(self, stream):
        self.stream = stream

    def write(self, text):
        if "GPU device discovery failed" in text or "device_discovery.cc" in text:
            return len(text)
        self.stream.write(text)
        return len(text)

    def flush(self):
        self.stream.flush()


sys.stderr = FilteredStderr(sys.stderr)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from src.voice_bot import create_app
from aiohttp import web
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

if __name__ == "__main__":
    PORT = int(os.getenv("PORT", "7860"))
    HOST = os.getenv("HOST", "0.0.0.0")
    BROWSER_SERVICE_URL = os.getenv("BROWSER_SERVICE_URL", "http://localhost:7863")

    logger.info(f"Starting Booking Voice Bot Service on {HOST}:{PORT}")
    logger.info(f"Browser Service URL: {BROWSER_SERVICE_URL}")

    app = create_app()
    web.run_app(app, host=HOST, port=PORT)
