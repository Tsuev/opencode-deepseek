"""Entry point — run the OpenAI-compatible DeepSeek/Qwen server.

    python app.py            # serves on http://localhost:8000

Missing sessions require explicit manual sign-in with `python -m deepseek.auth`
or the appropriate provider capture command. The API does not open a browser.
"""

import os

import uvicorn
from settings import load_environment

load_environment()

if __name__ == "__main__":
    from server.api_auth import public_bind_refusal
    host = os.getenv("HOST", "127.0.0.1")
    refusal = public_bind_refusal(host)
    if refusal:
        raise SystemExit(refusal)
    uvicorn.run(
        "server.api:app",
        host=host,
        port=int(os.getenv("PORT", "8000")),
        reload=False,
        proxy_headers=False,
    )
