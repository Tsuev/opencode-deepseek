# README artwork and terminal captures

The artwork is original SVG source. The OpenCode images are captures of real
sessions, rendered from terminal output; they are not generated model replies.

## OpenCode demos

Recorded on **2026-10-02** with **OpenCode 1.18.34**, against bridge code
`c9a1edef7d971b35ffb5961c72ac69bbf89f15ea`. These recordings predate the
provider pause and opt-in safety changes. They do not qualify the changed
DeepSeek authentication/headers; no live retest used the restricted account.

Each successful demo ran the native `plan` agent in a disposable workspace with
separate OpenCode home/config/data/cache/state directories. Only a public
`hello.py` fixture was provided:

```python
def health_status():
    return {"status": "ok"}
```

The agent was asked to use `read`, then state the return value. Each published
successful frame requires a completed native `read` and the expected final
answer. Editing, shell commands and web fetches were denied. The bridge used
the existing authorized accounts; no personal project content was submitted.

| Image | OpenCode model | Result |
| --- | --- | --- |
| [Qwen Flash](qwen-flash-opencode.png) | `local-qwen/qwen3.8-omni-flash` | Completed `read` and final answer |
| [Qwen Max](qwen-max-opencode.png) | `local-qwen/qwen3.8-max` | Completed `read` and final answer |
| [GLM](glm-opencode.png) | `local-glm/glm-web` | Completed `read` and final answer |
| [Kimi](kimi-opencode.png) | `local-kimi/kimi-web` | Completed `read` and final answer |

The finished sessions were reopened in the real OpenCode TUI without sending
another model request. A 130×24 PTY captured the terminal cells and ANSI
colours; pyte and Pillow rendered them into PNGs. The title bar identifies the
application and model. The messages, tool indicators and timings come from
OpenCode and are unchanged. Image hashes are in [capture-manifest.json](capture-manifest.json).

These timings include client startup, tool turns and browser waiting; they
are not a speed benchmark. The TUI's `$0.00` does not establish free unlimited
usage: no provider price is configured for the local alias, and website
allowances still apply. Raw account state, browser cookies, keys and session
exports are not published.

The initial GLM recording had no connected browser tab and timed out. The
published run was recorded after reopening and pairing the tab.
DeepSeek's fresh attempt stopped: an owned plain-request probe received HTTP
200 JSON with `biz_code=5`, `biz_msg="user is muted"`. No successful DeepSeek
frame was substituted, and requests stopped. No successful Mistral, Grok or
Gemini agent images are claimed; see the [current status](../../README.en.md#provider-status).

To run your own read-only demo, use an empty folder, add the fixture above and
configure an available provider from [examples/opencode.json](../../examples/opencode.json):

```bash
opencode run --agent plan --model local-qwen/qwen3.8-omni-flash \
  'Use the read tool to read hello.py and state the return value. Do not modify files.'
```

This command consumes the account's normal allowance. Browser providers need
an open, paired tab. Stop on access or quota errors.

## Editable artwork

- [Hero SVG](bridge-hero.svg) / [PNG](bridge-hero.png)
- [Architecture SVG](bridge-architecture.svg) / [PNG](bridge-architecture.png)
- [Rendering source](render_artwork.py)

Regenerate the artwork with Python and Pillow:

```bash
python -m pip install Pillow
python docs/media/render_artwork.py
```

The renderer uses installed Arial/Menlo or DejaVu fonts. It makes no network
requests and reads no account or terminal data. The diagram distinguishes
saved HTTP sessions, signed-in Safari tabs and the official Antigravity CLI;
its status annotations describe the dated controlled tests.
