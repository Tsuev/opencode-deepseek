"""Offline regressions for the bridge's trust boundaries and protocol handling."""
import atexit
import os
import secrets
import tempfile
from pathlib import Path

# The /v1 API requires a private bearer token. Fixture clients act as the
# authorized local client: they get an isolated token file and fixture Host
# names. tests.test_crosscheck_fixes.ApiAuthBoundary tests unauthenticated refusals.
_AUTH_DIRECTORY = tempfile.TemporaryDirectory(prefix="bridge-api-auth-")
atexit.register(_AUTH_DIRECTORY.cleanup)
TEST_API_TOKEN = secrets.token_urlsafe(32)
_token_file = Path(_AUTH_DIRECTORY.name) / "api-token"
_token_file.write_text(TEST_API_TOKEN + "\n")
_token_file.chmod(0o600)
os.environ["BRIDGE_API_TOKEN_FILE"] = str(_token_file)
os.environ.setdefault("API_ALLOWED_HOSTS", "testserver,audit.local,test.local,fixture.local,local.test")
os.environ.pop("API_AUTH", None)
NO_FIXTURE_AUTH = "x-test-no-fixture-auth"


def _authorize(request):
    if request.url.path.startswith("/v1") and "authorization" not in request.headers:
        if request.headers.get(NO_FIXTURE_AUTH) is None:
            request.headers["Authorization"] = "Bearer " + TEST_API_TOKEN
    request.headers.pop(NO_FIXTURE_AUTH, None)


def _install_fixture_auth():
    import httpx
    original_async = httpx.ASGITransport.handle_async_request

    async def handle_async_request(self, request):
        _authorize(request)
        return await original_async(self, request)

    httpx.ASGITransport.handle_async_request = handle_async_request
    try:
        from starlette import testclient
    except ImportError:
        return
    original_sync = testclient._TestClientTransport.handle_request

    def handle_request(self, request):
        _authorize(request)
        return original_sync(self, request)

    testclient._TestClientTransport.handle_request = handle_request


try:
    _install_fixture_auth()
except ImportError:  # Dependency-free subsets still import the package.
    pass
