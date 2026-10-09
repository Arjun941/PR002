"""Load .env before any backend module reads its settings (several read them at import time).
Variables already set in the environment win over .env."""
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
