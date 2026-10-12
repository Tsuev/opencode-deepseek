"""Load project-local environment settings before consumers read them."""

from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent


def load_environment() -> None:
    load_dotenv(ROOT / ".env")


load_environment()
