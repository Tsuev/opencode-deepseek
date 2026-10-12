import tempfile
from pathlib import Path
from server.conversation_cache import ConversationCache
from unittest.mock import patch

from providers.access import AccessGuard
from providers import access
from server import api
from server.config import DEEPSEEK_MODEL_MAP, QWEN_MODEL_MAP


def isolated_access(case):
    """Legacy transport fixtures explicitly enable models; never touch real state."""
    directory = tempfile.TemporaryDirectory()
    case.addCleanup(directory.cleanup)
    cache_patch = patch.object(api, 'conversation_cache', ConversationCache(Path(directory.name) / 'conversations.sqlite3'))
    cache_patch.start()
    case.addCleanup(cache_patch.stop)
    isolated = AccessGuard(path=None, interval=0)
    for patcher in (patch.object(api, "guard", isolated), patch.object(access, "guard", isolated),
                    patch.dict(api.MODEL_MAP, {**DEEPSEEK_MODEL_MAP, **QWEN_MODEL_MAP}, clear=True)):
        patcher.start()
        case.addCleanup(patcher.stop)
