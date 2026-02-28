# Booking Voice Agent

This project enables voice-based hotel booking using AWS Nova Sonic 2 S2S for speech-to-speech and browser-use with Bedrock Nova Lite 2 for LLM automation. It includes a Python backend (voice and browser agent services), a React frontend, and Docker support.

## Features
- Voice agent using AWS Nova Sonic 2 S2S
- Browser automation agent using browser-use and Bedrock Nova Lite 2
- React frontend for user interaction
- Dockerized deployment

## Quick Start
1. Clone the repo and install dependencies:
   ```sh
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   cd frontend
   npm install
   cd ..
   ```
2. Set up your `.env` file (see `.env.example`).
3. Start backend services:
   ```sh
   python main_voice.py
   python main_browser_service.py (new terminal still venv)
   ```
4. Start the frontend:
   ```sh
   cd frontend
   npm run dev
   ```

## AWS Setup
- Requires AWS credentials with access to Bedrock and Nova Sonic 2 models.
- Set `AWS_PROFILE`, `AWS_REGION`, `BEDROCK_REGION`, and `BEDROCK_MODEL_ID` in your `.env`.

## Project Structure
- `src/voice_bot.py`: Voice pipeline (Pipecat, Nova Sonic 2)
- `src/browser_agent.py`: browser-use agent (Bedrock Nova Lite 2)
- `main_voice.py`, `main_browser_service.py`: Entrypoints
- `frontend/`: React app
- `Dockerfile*`: Docker support

## License
MIT
