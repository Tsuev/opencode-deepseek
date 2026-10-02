"""Interactive DeepSeek chat in your terminal.

    ./ds                      # resume the last conversation
    ./ds --new                # start a fresh conversation
    ./ds --model expert       # stronger, slower model
    ./ds --think --search     # DeepThink reasoning + web search

Type /help inside the chat for the list of commands.

It talks to chat.deepseek.com directly through `DeepSeekClient` — no separate
server process needed. On the very first run a browser window opens so you can
sign in; the session is then reused silently.
"""

from __future__ import annotations

import argparse
import json
import sys

from settings import ROOT, load_environment

sys.path.insert(0, str(ROOT))

from deepseek import DeepSeekClient  # noqa: E402
from deepseek.auth import LoginRequired, write_private_json  # noqa: E402

STATE_FILE = ROOT / "session" / "chat_state.json"

MODELS = {"default": "Instant (fast)", "expert": "Expert (slower, stronger)"}


def _c(code: str, text: str) -> str:
    return text if not sys.stdout.isatty() else f"\033[{code}m{text}\033[0m"


DIM, BOLD, CYAN, YELLOW, RED, GREEN = "2", "1", "36", "33", "31", "32"


class Chat:
    def __init__(self, model: str = "default", thinking: bool = False,
                 search: bool = False, conversation_id: str | None = None):
        self.client = DeepSeekClient()
        self.model = model
        self.thinking = thinking
        self.search = search
        self.cid: str | None = conversation_id

    # -- persistence ---------------------------------------------------------

    def save(self) -> None:
        write_private_json(STATE_FILE, {
            "conversation_id": self.cid,
            "model": self.model,
            "thinking": self.thinking,
            "search": self.search,
        })

    def status(self) -> str:
        flags = []
        if self.thinking:
            flags.append(_c(YELLOW, "think"))
        if self.search:
            flags.append(_c(YELLOW, "web"))
        return "  ".join([_c(CYAN, f"model:{self.model}"), *flags]) or "  "

    # -- turns ---------------------------------------------------------------

    def ask(self, prompt: str) -> None:
        # A thread's model is fixed when it's created, so `model` may only be
        # sent on the first turn of a conversation.
        model = self.model if self.cid is None else None
        if self.thinking:
            # Reasoning fragments are filtered out of the stream, so DeepThink
            # replies arrive silently for a while — say so up front.
            print(_c(DIM, "thinking…"), flush=True)
        chunks = self.client.stream(
            prompt,
            conversation_id=self.cid,
            model=model,
            thinking=self.thinking,
            search=self.search,
        )
        try:
            for chunk in chunks:
                sys.stdout.write(chunk)
                sys.stdout.flush()
        finally:
            print()
        if chunks.conversation_id:
            self.cid = chunks.conversation_id
        self.save()

    # -- slash commands ------------------------------------------------------

    def command(self, line: str) -> bool:
        """Run a slash command. Returns False to quit the REPL."""
        name, _, arg = line[1:].strip().partition(" ")
        arg = arg.strip()

        if name in ("exit", "quit", "q"):
            return False
        if name in ("help", "h", "?"):
            print(HELP)
            return True
        if name in ("new", "n"):
            self.cid = None
            self.save()
            print(_c(GREEN, "started a new conversation"))
            return True
        if name == "model":
            if arg not in MODELS:
                print(_c(RED, f"usage: /model default|expert  ({', '.join(MODELS)})"))
                return True
            if arg != self.model:
                if self.cid is not None:
                    print(_c(YELLOW, "model is fixed per thread — starting a new "
                                     "conversation"))
                    self.cid = None
                self.model = arg
            self.save()
            print(_c(GREEN, f"model: {self.model} — {MODELS[self.model]}"))
            return True
        if name in ("think", "thinking"):
            self.thinking = not self.thinking
            self.save()
            print(_c(GREEN, f"DeepThink: {'on' if self.thinking else 'off'}"))
            return True
        if name in ("search", "web"):
            self.search = not self.search
            self.save()
            print(_c(GREEN, f"web search: {'on' if self.search else 'off'}"))
            return True
        if name == "status":
            print("  " + self.status())
            return True
        print(_c(RED, f"unknown command /{name} — try /help"))
        return True

    def close(self) -> None:
        self.client.close()


HELP = _c(BOLD, "commands") + """
  /new              start a new conversation (history on the site stays)
  /model default    fast Instant model        (/model expert — stronger, slower)
  /think            toggle DeepThink reasoning
  /search           toggle web search
  /status           show the current model and toggles
  /exit             leave (Ctrl-D works too)

  Multi-line input: end a line with a single \\ to continue on the next one.
"""


def main() -> int:
    load_environment()

    p = argparse.ArgumentParser(
        description="Interactive DeepSeek chat in the terminal.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--model", choices=MODELS, default=None,
                   help="default = Instant (fast), expert = stronger/slower")
    p.add_argument("--think", action=argparse.BooleanOptionalAction, default=None,
                   help="toggle DeepThink reasoning (--no-think to disable)")
    p.add_argument("--search", action=argparse.BooleanOptionalAction, default=None,
                   help="toggle web search (--no-search to disable)")
    p.add_argument("--new", action="store_true",
                   help="ignore the saved conversation and start a new one")
    p.add_argument("--continue", dest="resume", action="store_true",
                   help="resume the saved conversation (the default)")
    args = p.parse_args()

    state = None
    if not args.new and STATE_FILE.exists():
        try:
            loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            state = loaded if isinstance(loaded, dict) else None
        except Exception:
            state = None

    model = args.model or (state or {}).get("model", "default")
    cid = (state or {}).get("conversation_id")
    if args.model is not None and args.model != (state or {}).get("model"):
        cid = None
    thinking = args.think if args.think is not None else (state or {}).get("thinking", False)
    search = args.search if args.search is not None else (state or {}).get("search", False)

    try:
        chat = Chat(model=model, thinking=thinking, search=search,
                    conversation_id=cid)
    except LoginRequired as e:
        print(_c(RED, str(e)))
        return 1
    except Exception as e:  # browser/PoW/setup problems
        print(_c(RED, f"could not start: {type(e).__name__}: {e}"))
        return 1

    resumed = "resumed" if chat.cid else "new"
    print(_c(BOLD, "DeepSeek chat") + _c(DIM, f"  ({resumed} conversation)")
          + "\n" + "  " + chat.status() + "\n" + _c(DIM, "  /help for commands")
          + "\n")

    pending: list[str] = []
    try:
        while True:
            try:
                if pending:
                    line = input(_c(CYAN, "… ")).strip()
                else:
                    line = input(_c(CYAN, "you> ")).strip()
            except EOFError:
                print()
                break

            if not line:
                pending.clear()
                continue
            if line.endswith("\\") and not line.endswith("\\\\"):
                pending.append(line[:-1].rstrip())
                continue
            pending.append(line)
            prompt = "\n".join(pending)
            pending.clear()

            if prompt.startswith("/") and "\n" not in prompt:
                if not chat.command(prompt):
                    break
                continue

            print(_c(DIM, "dee> "), end="", flush=True)
            try:
                chat.ask(prompt)
            except KeyboardInterrupt:
                print("\n" + _c(YELLOW, "interrupted"))
            except Exception as e:
                print(_c(RED, f"\nerror: {type(e).__name__}: {e}"))
    finally:
        chat.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
