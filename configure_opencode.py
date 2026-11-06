"""Safely add or update the local DeepSeek provider in OpenCode."""
from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import datetime
from pathlib import Path

PROVIDER_ID = "local-deepseek"


def provider(base_url: str) -> dict:
    return {
        "npm": "@ai-sdk/openai-compatible",
        "name": "DeepSeek (local bridge)",
        "options": {"baseURL": base_url.rstrip("/"), "apiKey": "unused"},
        "models": {
            "deepseek-chat": {
                "name": "DeepSeek Chat (Instant)", "tool_call": True,
                "attachment": True,
                "reasoning": False, "limit": {"context": 64000, "output": 8192},
            },
            "deepseek-expert": {
                "name": "DeepSeek Expert", "tool_call": True,
                "attachment": True,
                "reasoning": False, "limit": {"context": 64000, "output": 8192},
            },
        },
    }


def install(path: Path, base_url: str) -> Path | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    backup = None
    if path.exists():
        with path.open("r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("OpenCode config must be a JSON object")
    providers = data.setdefault("provider", {})
    if not isinstance(providers, dict):
        raise ValueError("OpenCode config 'provider' must be an object")
    new_provider = provider(base_url)
    changed = providers.get(PROVIDER_ID) != new_provider
    if not changed:
        return None
    if path.exists():
        backup = path.with_suffix(path.suffix + ".backup-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(path, backup)
    providers[PROVIDER_ID] = new_provider
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return backup


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/v1")
    args = parser.parse_args()
    target = Path(os.environ.get("OPENCODE_CONFIG", Path.home() / ".config" / "opencode" / "opencode.json"))
    install(target, args.base_url)
    print(f"Updated {PROVIDER_ID} in {target}")
