"""
Standalone night sync runner.

Called directly by systemd — completely independent of the web server.
Auto-deploy restarting uvicorn does NOT affect this process.

Usage:
    /opt/parcel-manager/venv/bin/python -m app.night_sync_runner
"""

import logging
import os
import sys

# Load .env before importing anything that reads env vars
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    stream=sys.stdout,
)

from app.services.night_sync_service import run_night_sync  # noqa: E402

if __name__ == "__main__":
    run_night_sync()
