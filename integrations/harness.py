"""Configure DeepSeek Harness without authenticating or calling a model."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit
import uuid

import httpx
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from providers.kimi_models import MODELS as KIMI_MODELS, PUBLIC_MODELS as KIMI_PUBLIC_MODELS


ROUTE = "local-browser-bridge"
PROVIDERS = {"deepseek", "qwen", "glm", "kimi", "grok", "mistral", "gemini", "chatgpt", "claude"}
BROWSERLESS_PROVIDERS = {"deepseek", "qwen", "gemini"}
LABELS = {
    "deepseek-chat": "DeepSeek Chat · web",
    "deepseek-expert": "DeepSeek Expert · web",
    "qwen3.8-omni-flash": "Qwen3.8 Omni Flash · web",
    "qwen3.8-max": "Qwen3.8 Max · web",
    "glm-web": "GLM · selected website model",
    "kimi-web": "Kimi · selected website model",
    "kimi-instant": "Kimi Instant · web",
    "grok-web": "Grok · experimental",
    "mistral-web": "Mistral · experimental",
    "qwen-web": "Qwen · selected website model",
    "gemini-web": "Gemini · selected website model",
    "chatgpt-web": "ChatGPT · selected website model",
    "claude-web": "Claude · selected website model",
}
LABELS.update({spec['public_id']: spec['label'] for spec in KIMI_MODELS.values()})
BRIDGE_ONLY_MODELS = frozenset({
    "qwen-web", "gemini-web", "grok-web", "mistral-web", "glm-web", "kimi-web",
    "chatgpt-web", "claude-web",
})


def local_url(value: str) -> str:
    """Accept only a literal loopback HTTP endpoint, with no proxy or redirects."""
    parsed = urlsplit(value)
    try:
        local = ipaddress.ip_address(parsed.hostname or "").is_loopback
        port = parsed.port
    except ValueError:
        local, port = False, None
    if (parsed.scheme != "http" or not local or port == 0
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path.rstrip("/") != "/v1"):
        raise ValueError("Use a literal loopback URL such as http://127.0.0.1:8000/v1")
    return value.rstrip("/")


def api_token() -> str:
    """The bridge's private /v1 bearer token (created on first use, never printed)."""
    from server.api_auth import ensure_token
    return ensure_token()


def discover(base_url: str, without_browser=False) -> list:
    """Read the bridge catalog only; this does not probe account availability."""
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=5) as client:
        response = client.get(local_url(base_url) + "/models",
                              headers={"Authorization": "Bearer " + api_token()})
        response.raise_for_status()
        if len(response.content) > 128 * 1024:
            raise ValueError("Bridge model catalog is too large")
        return catalog_models(response.json(), without_browser=without_browser)


def catalog_models(catalog: dict, without_browser=False) -> list:
    rows = catalog.get("data") if isinstance(catalog, dict) else None
    # Bare ids plus owner/id aliases roughly double the catalog size.
    if not isinstance(rows, list) or not rows or len(rows) > 200:
        raise ValueError("Bridge advertises no models or an invalid catalog; enable a provider first")
    models, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid bridge model entry")
        model = row.get("id")
        owner = row.get("owned_by")
        # Bare ids and matching `owner/id` aliases from the bridge catalog.
        if (not isinstance(model, str)
                or not re.fullmatch(
                    r"(?:[a-z][a-z0-9-]{0,31}/)?[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", model)
                or model in seen or not isinstance(owner, str) or owner not in PROVIDERS):
            raise ValueError("Invalid or duplicate bridge model identity")
        bare = model.split("/", 1)[-1]
        if "/" in model and not model.startswith(owner + "/"):
            raise ValueError("Invalid or duplicate bridge model identity")
        seen.add(model)
        models.append({"id": model, "name": LABELS.get(bare, LABELS.get(model, model + " · bridge")),
                       "input": ["text"], "reasoningEfforts": False,
                       "contextWindow": 32768, "maxTokens": 4096})
    # Validate the entire advertised catalog before excluding any provider.
    if without_browser:
        models = [model for row, model in zip(rows, models)
                  if row["id"].split("/", 1)[-1] not in BRIDGE_ONLY_MODELS
                  and (row["owned_by"] in BROWSERLESS_PROVIDERS
                       or (row["owned_by"] == "kimi"
                           and row["id"].split("/", 1)[-1] in KIMI_PUBLIC_MODELS))]
        if not models:
            raise ValueError("Bridge advertises no browserless models for --without-browser")
    return models


def provider_config(base_url: str, models: list, token: str | None = None) -> dict:
    return {
        "displayName": "Local account bridge",
        "api": "openai-completions", "baseURL": local_url(base_url),
        # The bridge /v1 token, not a provider credential. The profile file is
        # written 0600; account credentials stay exclusively in the bridge.
        "headers": {"Authorization": "Bearer " + (api_token() if token is None else token)},
        "retryPolicy": {"mode": "normal", "maxRetries": 0},
        "timeoutMs": 600000, "streamIdleTimeoutMs": 600000,
        "compat": {"supportsStore": False, "supportsDeveloperRole": False,
                   "supportsReasoningEffort": False, "supportsUsageInStreaming": False,
                   "supportsStrictMode": False, "maxTokensField": "max_tokens"},
        "models": copy.deepcopy(models),
    }


def detached_config(value):
    """Copy a modified config without reusing named anchors from other rows."""
    result = copy.deepcopy(value)
    seen = set()

    def visit(node):
        if id(node) in seen:
            return
        seen.add(id(node))
        if getattr(node, "merge", None):
            raise ValueError("YAML merge keys in the edited config need manual integration")
        if hasattr(node, "yaml_set_anchor"):
            node.yaml_set_anchor(None)
        if isinstance(node, dict):
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(result)
    return result


def patch_document(document: list, base_url: str, models: list, default_model=None,
                   without_browser=False) -> list:
    """Merge one owned route; refuse ambiguous patch operations on edited rows."""
    if not isinstance(document, list):
        raise ValueError("Harness profile patch must be a YAML list")
    if without_browser and not models:
        raise ValueError("No browserless models selected for --without-browser")
    result = copy.deepcopy(document)
    target_ids = {"llm-pi-ai", "session-title-llm", "agent-default-model"}
    selected_ids = {model["id"] for model in models}
    if default_model is not None:
        if default_model not in selected_ids:
            raise ValueError("Default model must be advertised by the bridge")
    if default_model is not None or without_browser:
        target_ids.add("agent-default-model")
    existing = {}
    for row in result:
        if not isinstance(row, dict):
            raise ValueError("Unsupported Harness patch entry")
        # Nested patches and dynamic config require the Harness editor. Do not
        # attempt to evaluate !!js or infer a custom composition here.
        if (set(row) == {"insert"} and isinstance(row["insert"], list)
                and len(row["insert"]) == 1 and isinstance(row["insert"][0], dict)
                and row["insert"][0].get("id") in
                {"local-bridge-budget", "local-bridge-shell-safety"}):
            continue
        if "id" not in row:
            raise ValueError("Install supports id-targeted patches only; use --output for custom profiles")
        key = row["id"]
        if key not in target_ids:
            continue
        if key in existing or set(row) - {"id", "config", "disabled"}:
            raise ValueError("Ambiguous Harness patch for " + key)
        if "config" in row and not isinstance(row["config"], dict):
            raise ValueError("Dynamic Harness config needs manual editing: " + key)
        existing[key] = row

    if default_model is None and "agent-default-model" in existing:
        default = existing["agent-default-model"].get("config", {})
        provider = default.get("provider")
        if not isinstance(provider, str):
            raise ValueError("Dynamic or non-string default provider cannot be checked for --without-browser; "
                             "select an advertised --default-model explicitly")
        if provider == ROUTE:
            model = default.get("model")
            if not isinstance(model, str):
                raise ValueError("Dynamic or non-string owned default model cannot be checked for --without-browser; "
                                 "select an advertised --default-model explicitly")
            if model not in selected_ids:
                raise ValueError("Existing default model is absent from the selected catalog; "
                                 "select an advertised --default-model explicitly")

    def entry(key):
        if key not in existing:
            existing[key] = {"id": key}
            result.append(existing[key])
        return existing[key]

    llm = entry("llm-pi-ai")
    if llm.get("disabled", False) is not False:
        raise ValueError("The Harness model adapter is disabled; enable it explicitly before installing")
    # Detach YAML anchors so changing this row cannot mutate an unrelated alias.
    config = detached_config(llm.get("config", {}))
    llm["config"] = config
    providers = config.get("providers", {})
    config["providers"] = providers
    if not isinstance(providers, dict):
        raise ValueError("Dynamic provider configuration needs manual editing")
    providers[ROUTE] = provider_config(base_url, models)
    llm["disabled"] = False
    # First-prompt title generation otherwise sends an extra model request.
    entry("session-title-llm")["disabled"] = True
    if default_model is not None:
        entry("agent-default-model")["config"] = {"provider": ROUTE, "model": default_model}
        entry("agent-default-model")["disabled"] = False
    return result


def yaml_codec():
    codec = YAML(typ="rt")
    codec.preserve_quotes = True
    codec.allow_duplicate_keys = False
    return codec


def render(document: list) -> bytes:
    stream = io.StringIO()
    yaml_codec().dump(document, stream)
    return stream.getvalue().encode("utf-8")


def write_private(path: Path, data: bytes, before=None) -> Path | None:
    """Back up exact previous bytes, then atomically install a private patch."""
    if path.is_symlink():
        raise ValueError("Refusing to replace a symlink")
    current = path.read_bytes() if path.exists() else None
    if current != before:
        raise ValueError("Configuration changed while preparing it; run again")
    if current == data:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if current is not None:
        backup = path.with_name(path.name + ".bridge-backup-" + uuid.uuid4().hex)
        with os.fdopen(os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
            out.write(current)
            out.flush()
            os.fsync(out.fileno())
    descriptor, temporary = tempfile.mkstemp(prefix=".bridge-", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        # Do not overwrite an edit made during the backup/write interval.
        if path.is_symlink() or (path.read_bytes() if path.exists() else None) != before:
            raise ValueError("Configuration changed while writing it; original retained")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return backup


def default_home() -> Path:
    """Harness home: DSH_HOME when set, else ~/.dsh (shared by every installer)."""
    return Path(os.getenv("DSH_HOME", "").strip() or "~/.dsh").expanduser()


def install_plugin_copy(home, name, sources) -> Path:
    """Copy plugin files into the Harness home and return the entry path.

    The profile then references a stable private copy, so moving or deleting
    this repository cannot silently unload a protection plugin.
    """
    directory = Path(home) / "plugins" / "opencode-local-bridge" / name
    if any(part.is_symlink() for part in (directory, *directory.parents) if part.exists()
           and Path(home) in (part, *part.parents)):
        raise ValueError("Refusing a symlinked plugin directory")
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    for source in sources:
        source = Path(source)
        target = directory / source.name
        if target.is_symlink():
            raise ValueError("Refusing a symlinked plugin file")
        data = source.read_bytes()
        if target.exists() and target.read_bytes() == data:
            continue
        descriptor, temporary = tempfile.mkstemp(prefix=".plugin-", dir=str(directory))
        try:
            with os.fdopen(descriptor, "wb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return directory / Path(sources[0]).name


@contextmanager
def profile_lock(home, profile):
    from providers.access import _file_lock
    path = Path(home) / ('.bridge-install-' + profile + '.lock')
    with path.open('a+') as lock:
        os.chmod(path, 0o600)
        _file_lock(lock)
        try:
            yield
        finally:
            _file_lock(lock, unlock=True)


def install(home, profile, *args, **kwargs):
    with profile_lock(home, profile):
        return _install_unlocked(home, profile, *args, **kwargs)


def _install_unlocked(home: Path, profile: str, base_url: str, models: list, default_model=None,
            without_browser=False):
    """Edit a standard initialized profile, retaining other provider settings."""
    directory = home / "profiles" / profile
    manifest = json.loads((directory / "package.json").read_text())
    bundles = manifest.get("dsh", {}).get("profile", {}).get("bundles")
    expected = ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-" + (
        "headless" if profile == "headless" else "web-app")]
    if bundles != expected:
        raise ValueError("Custom bundle composition: export with --output and merge through Harness")
    for other in (home / "cordis.patch.yml", directory / "cordis.yml"):
        if other.exists() and yaml_codec().load(other) not in (None, []):
            raise ValueError("Nonempty home/root overrides: merge an exported patch through Harness")
    path = directory / "cordis.patch.yml"
    before = path.read_bytes()
    document = yaml_codec().load(before.decode("utf-8"))
    after = patch_document([] if document is None else document, base_url, models, default_model,
                           without_browser=without_browser)
    return path, write_private(path, render(after), before)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/v1")
    parser.add_argument("--default-model", help="Explicitly select the default for new Harness sessions")
    parser.add_argument("--without-browser", action="store_true",
                        help="Select only advertised DeepSeek, Qwen and Gemini adapters")
    parser.add_argument("--home", type=Path, default=default_home())
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--install", choices=("desktop", "web", "headless"), metavar="PROFILE")
    mode.add_argument("--output", type=Path, help="Export a standalone YAML patch to a new file")
    args = parser.parse_args(argv)
    try:
        models = discover(args.base_url, without_browser=args.without_browser)
        if args.install:
            path, backup = install(args.home.expanduser(), args.install, args.base_url, models, args.default_model,
                                   without_browser=args.without_browser)
        else:
            path, backup = args.output, None
            if path.exists() or path.is_symlink():
                raise ValueError("Output exists; choose a new path")
            write_private(path, render(patch_document([], args.base_url, models, args.default_model,
                                                      without_browser=args.without_browser)))
        print("Harness configuration: " + str(path))
        if backup is not None:
            print("Previous configuration: " + str(backup))
        print("Models: " + ", ".join(model["id"] for model in models))
        print("Automatic retries and model-generated session titles are disabled in this profile.")
        print("No account requests sent. Provider enablement and durable pauses are unchanged.")
    except YAMLError:
        parser.exit(1, "Harness setup failed: invalid YAML; configuration was not changed\n")
    except (OSError, ValueError, httpx.HTTPError) as exc:
        # HTTP errors can include endpoint text, but never provider responses.
        parser.exit(1, "Harness setup failed: " + str(exc) + "\n")


if __name__ == "__main__":
    main()
