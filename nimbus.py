#!/usr/bin/env python3
"""AhoosAI in a terminal.

    python nimbus.py                 the full-screen interface
    python nimbus.py --plain         a plain scrolling prompt
    python nimbus.py -a              agent mode: it may read, write and run
    python nimbus.py "one question"  ask once and exit

WHAT THIS IS

A client for a Nimbus server, where a manager model reads the request, hands it
to the specialist that owns the domain, and assembles the answer. The
interesting part of that is the routing, so the plan and each delegation are
shown while they happen rather than hidden behind a spinner.

TWO FRONT ENDS, ONE SESSION

`Session` never calls print() or input(). It talks to a `UI` -- write a line,
say whether it is busy, ask a yes or no -- and that is the entire seam. The
plain prompt and the full-screen interface each implement those three things
and neither knows the other exists. Adding a third would touch nothing here.

CONVERSATION

The API takes one string and holds no history, so history is built here: the
last few exchanges are folded into the prompt. Keeping it on this side is what
leaves the server stateless.

SAFETY

In agent mode the model can ask to write files and run commands. Both stop and
ask, every time, showing exactly what will happen. There is no "yes to all",
because the value of the prompt is that you read it.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))

import actions  # noqa: E402
import render as r  # noqa: E402
from client import Chip, Client, Config, NimbusError, load_env  # noqa: E402

HISTORY_TURNS = 6
MAX_AGENT_STEPS = 8


class UI(Protocol):
    """Everything a Session needs from the world."""

    def write(self, text: str) -> None: ...
    def busy(self, working: bool) -> None: ...
    def confirm(self, action: "actions.Action") -> bool: ...


class Session:
    """One conversation, and the agent loop that runs inside it."""

    def __init__(self, client: Client, root: Path, agent: bool, ui: UI) -> None:
        self.client = client
        self.workspace = actions.Workspace(root)
        self.agent = agent
        self.ui = ui
        self.turns: list[tuple[str, str]] = []

    # -- context -----------------------------------------------------------

    def prompt_with_history(self, message: str) -> str:
        """Older turns as context, this turn as the request.

        Labelled rather than concatenated: without the labels a model reads the
        whole thing as one long question and answers the first half of it.
        """
        if not self.turns:
            return message
        parts = ["Earlier in this conversation:", ""]
        for who, text in self.turns[-HISTORY_TURNS:]:
            body = text if len(text) < 1500 else text[:1500] + " ..."
            parts.append(f"{who}: {body}")
        parts += ["", "Now answer this:", message]
        return "\n".join(parts)

    def chips(self) -> list[Chip]:
        if not self.agent:
            return []
        return [actions.protocol_chip(self.workspace.root, self.workspace.listing())]

    # -- one exchange ------------------------------------------------------

    def ask(self, message: str) -> None:
        self.turns.append(("Person", message))
        pending = message

        for step in range(1, MAX_AGENT_STEPS + 1):
            self.ui.busy(True)
            try:
                run = self.client.ask(
                    self.prompt_with_history(pending) if step == 1 else pending,
                    chips=self.chips(),
                    on_event=self._progress,
                )
            except NimbusError as exc:
                self.ui.write(r.red("  " + str(exc)))
                return
            finally:
                self.ui.busy(False)

            answer = run.get("answer", "")
            self.turns.append(("Nimbus", answer))

            requested = actions.find(answer) if self.agent else []
            prose = actions.strip(answer) if requested else answer
            if prose:
                self.ui.write(r.answer(prose))

            if not requested:
                self._footer(run)
                return

            results = self._perform(requested)
            if results is None:  # the person declined; stop rather than nag
                self._footer(run)
                return
            pending = "Results of the actions you asked for:\n\n" + results

        self.ui.write(r.yellow(f"  stopped after {MAX_AGENT_STEPS} steps. Ask again to continue."))

    # -- the agent loop ----------------------------------------------------

    def _perform(self, requested: list[actions.Action]) -> str | None:
        out: list[str] = []
        for action in requested:
            if action.mutating and not self.ui.confirm(action):
                self.ui.write(r.grey("  skipped"))
                out.append(f"[{action.describe()}] declined by the person")
                continue
            self.ui.write(r.grey("  " + action.describe()))
            try:
                result = self.workspace.perform(action)
            except ValueError as exc:
                result = f"refused: {exc}"
                self.ui.write(r.red("  " + result))
            out.append(f"### {action.kind} {action.target}\n{result}")
        return "\n\n".join(out) if out else None

    # -- progress and summary ----------------------------------------------

    def _progress(self, kind: str, data: dict) -> None:
        if kind == "plan:start":
            self.ui.write(r.grey(f"  planning with {data.get('value', 'the manager')}"))
        elif kind == "plan:done":
            if data.get("handle_directly"):
                self.ui.write(r.grey("  manager answering directly"))
            else:
                who = ", ".join(d.get("model", "?") for d in data.get("delegations", []))
                self.ui.write(r.grey(f"  plan: {who or 'no delegations'}"))
        elif kind == "delegate:start":
            self.ui.write(r.grey(f"  -> {data.get('model', '?')}"))
        elif kind == "delegate:done":
            mark = r.red("failed") if data.get("error") else r.green("done")
            self.ui.write(r.grey(f"  <- {data.get('model', '?')} ") + mark)
        elif kind == "synthesis:start":
            self.ui.write(r.grey("  assembling"))

    def _footer(self, run: dict) -> None:
        usage = run.get("usage") or {}
        bits = [f"{k} {v}" for k, v in usage.items() if v]
        if bits:
            self.ui.write(r.grey("  " + " . ".join(bits)))


# -- the plain front end ----------------------------------------------------

class PlainUI:
    """A scrolling prompt. The clock lives here, because the redraw does."""

    COLD = 20  # seconds after which a sleeping free tier is the likely answer

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def write(self, text: str) -> None:
        print(text)

    def busy(self, working: bool) -> None:
        if working:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._tick, daemon=True)
            self._thread.start()
            return
        if not self._thread:
            return
        self._stop.set()
        self._thread.join(timeout=1)
        self._thread = None
        sys.stdout.write("\r" + " " * 70 + "\r")
        sys.stdout.flush()

    def _tick(self) -> None:
        began = time.monotonic()
        while not self._stop.wait(0.5):
            seconds = int(time.monotonic() - began)
            note = "  (a sleeping free tier takes about a minute to wake)" if seconds >= self.COLD else ""
            sys.stdout.write("\r  " + r.grey(f"working  {seconds}s{note}"))
            sys.stdout.flush()

    def confirm(self, action: actions.Action) -> bool:
        print()
        print(r.rule(action.kind))
        if action.kind == "run":
            print("  " + r.bold(action.body.strip()))
        else:
            preview = action.body.splitlines()
            head = preview[:20]
            print(r.grey(f"  {action.target}"))
            for line in head:
                print(r.grey("  | ") + line)
            if len(preview) > len(head):
                print(r.grey(f"  | ... {len(preview) - len(head)} more lines"))
        print(r.rule())
        try:
            return input(r.yellow("  do it? [y/N] ")).strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False


# -- commands ---------------------------------------------------------------

HELP = """
  /help              this
  /models            what the server has
  /agent [on|off]    let it read, write and run in this directory
  /cd <path>         change the working directory
  /pwd               where it is working
  /new               forget the conversation
  /exit              leave
"""


def command(line: str, session: Session) -> bool:
    """Handle a slash command. Returns False when it is time to leave."""
    name, _, rest = line[1:].partition(" ")
    rest = rest.strip()
    out = session.ui.write

    if name in ("exit", "quit", "q"):
        return False
    if name == "help":
        out(r.grey(HELP))
    elif name == "models":
        try:
            for m in session.client.models():
                mark = r.yellow("manager") if m.get("role") == "manager" else r.grey(m.get("domain", ""))
                out(f"  {r.bold(m.get('id', '?'))}  {mark}")
                if m.get("summary"):
                    out(r.grey(f"      {m['summary']}"))
        except NimbusError as exc:
            out(r.red("  " + str(exc)))
    elif name == "agent":
        session.agent = rest == "on" if rest in ("on", "off") else not session.agent
        out(r.grey(f"  agent {'on' if session.agent else 'off'} - {session.workspace.root}"))
        if session.agent:
            out(r.grey("  it may ask to read, write and run here. Writes and commands ask first."))
    elif name == "cd":
        try:
            session.workspace = actions.Workspace(Path(rest or ".").resolve())
            out(r.grey(f"  {session.workspace.root}"))
        except Exception as exc:
            out(r.red(f"  {exc}"))
    elif name == "pwd":
        out(r.grey(f"  {session.workspace.root}"))
    elif name == "new":
        session.turns.clear()
        out(r.grey("  conversation cleared"))
    else:
        out(r.grey(f"  no such command: /{name}. /help lists them."))
    return True


# -- entry point ------------------------------------------------------------

def main(argv: list[str]) -> int:
    r.setup()

    agent = False
    plain = False
    words: list[str] = []
    for arg in argv:
        if arg in ("-a", "--agent"):
            agent = True
        elif arg in ("-p", "--plain"):
            plain = True
        elif arg in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            words.append(arg)

    root = Path.cwd()
    load_env(root)
    load_env(Path(__file__).resolve().parent)
    client = Client(Config.from_env())

    try:
        health = client.health()
    except NimbusError as exc:
        print(r.red(str(exc)))
        print(r.grey("  set NIMBUS_URL and NIMBUS_API_KEY, or start a server locally."))
        return 1

    if words:  # one question, then leave
        Session(client, root, agent, PlainUI()).ask(" ".join(words))
        return 0

    if not plain and sys.stdout.isatty():
        try:
            import tui

            return tui.run(client, root, agent, health)
        except Exception as exc:  # a UI that will not start must not be a wall
            print(r.yellow(f"  the full-screen interface did not start ({exc}); using the plain prompt."))

    return plain_loop(client, root, agent, health)


def plain_loop(client: Client, root: Path, agent: bool, health: dict) -> int:
    session = Session(client, root, agent, PlainUI())
    print(r.banner(client.config.url, health.get("models", 0)))
    if agent:
        print(r.grey(f"  agent on - {root}\n"))

    while True:
        try:
            line = input(r.cyan("> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line.startswith("/"):
            if not command(line, session):
                return 0
            continue
        try:
            session.ask(line)
            print()
        except KeyboardInterrupt:
            print(r.grey("\n  stopped\n"))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
