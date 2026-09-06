#!/usr/bin/env python3
"""Nimbus in a terminal.

    python nimbus.py                 chat in the current directory
    python nimbus.py -a              agent mode: it may read, write and run
    python nimbus.py "one question"  ask once and exit

WHAT THIS IS

A client for a Nimbus server, where a manager model reads the request, hands it
to a specialist, and assembles the answer. The interesting part of that is not
the answer, it is the routing, so this prints the plan and each delegation while
they happen instead of showing a spinner.

CONVERSATION

The API takes one string. It has no history, so history is built here: the last
few exchanges are folded into the prompt. That is a client concern, not a server
one, and doing it here keeps the server stateless.

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

sys.path.insert(0, str(Path(__file__).resolve().parent))

import actions  # noqa: E402
import render as r  # noqa: E402
from client import Chip, Client, Config, NimbusError, load_env  # noqa: E402

HISTORY_TURNS = 6
MAX_AGENT_STEPS = 8


class Waiting:
    """A clock, because the first minute of a request shows nothing else.

    The server streams what the family is *doing*, and the first of those events
    is the finished plan -- which arrives only after a whole planning call. On a
    free tier that has gone to sleep, add a cold start in front of that. So
    between pressing enter and the first line of output there can be a minute
    and a half of silence, which is indistinguishable from a hang.

    This is not decoration. It is the difference between "it is working" and
    "it is broken", and nothing else on screen tells those apart.
    """

    COLD = 20  # seconds after which a sleeping free tier is the likely answer

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._tick, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        sys.stdout.write("\r" + " " * 70 + "\r")
        sys.stdout.flush()

    def _tick(self) -> None:
        began = time.monotonic()
        while not self._stop.wait(0.5):
            seconds = int(time.monotonic() - began)
            note = "  (a sleeping free tier takes about a minute to wake)" if seconds >= self.COLD else ""
            sys.stdout.write("\r  " + r.grey("working  " + str(seconds) + "s" + note))
            sys.stdout.flush()


class Session:
    def __init__(self, client: Client, root: Path, agent: bool) -> None:
        self.client = client
        self.workspace = actions.Workspace(root)
        self.agent = agent
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
        step = 0
        pending = message

        while step < MAX_AGENT_STEPS:
            step += 1
            clock = Waiting()
            clock.start()
            try:
                run = self.client.ask(
                    self.prompt_with_history(pending) if step == 1 else pending,
                    chips=self.chips(),
                    on_event=lambda kind, data: self._report(clock, kind, data),
                )
            except NimbusError as exc:
                clock.stop()
                print(r.red("  " + str(exc)))
                return
            finally:
                clock.stop()

            answer = run.get("answer", "")
            self.turns.append(("Nimbus", answer))

            requested = actions.find(answer) if self.agent else []
            prose = actions.strip(answer) if requested else answer
            if prose:
                print(r.answer(prose))

            if not requested:
                self._footer(run)
                return

            results = self._perform(requested)
            if results is None:  # the person declined; stop rather than nag
                self._footer(run)
                return
            pending = "Results of the actions you asked for:\n\n" + results

        print(r.yellow(f"  stopped after {MAX_AGENT_STEPS} steps. Ask again to continue."))

    # -- the agent loop ----------------------------------------------------

    def _perform(self, requested: list[actions.Action]) -> str | None:
        out: list[str] = []
        for action in requested:
            if action.mutating and not self._confirm(action):
                print(r.grey("  skipped"))
                out.append(f"[{action.describe()}] declined by the person")
                continue
            print(r.grey("  " + action.describe()))
            try:
                result = self.workspace.perform(action)
            except ValueError as exc:
                result = f"refused: {exc}"
                print(r.red("  " + result))
            out.append(f"### {action.kind} {action.target}\n{result}")
        return "\n\n".join(out) if out else None

    def _confirm(self, action: actions.Action) -> bool:
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
            reply = input(r.yellow("  do it? [y/N] ")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return reply in ("y", "yes")

    # -- progress and summary ----------------------------------------------

    @staticmethod
    def _report(clock: "Waiting", kind: str, data: dict) -> None:
        """Print an event on its own line, then go back to counting.

        Each delegation is its own wait, so the clock has to survive the events
        rather than stop at the first one. It is cleared, the line is printed
        above it, and it starts again from zero for the next stretch.
        """
        clock.stop()
        Session._progress(kind, data)
        clock.start()

    @staticmethod
    def _progress(kind: str, data: dict) -> None:
        if kind == "plan:start":
            print(r.grey(f"  planning with {data.get('value', 'the manager')}"))
        elif kind == "plan:done":
            if data.get("handle_directly"):
                print(r.grey("  manager answering directly"))
            else:
                who = ", ".join(d.get("model", "?") for d in data.get("delegations", []))
                print(r.grey(f"  plan: {who or 'no delegations'}"))
        elif kind == "delegate:start":
            print(r.grey(f"  -> {data.get('model', '?')}"))
        elif kind == "delegate:done":
            mark = r.red("failed") if data.get("error") else r.green("done")
            print(r.grey(f"  <- {data.get('model', '?')} ") + mark)
        elif kind == "synthesis:start":
            print(r.grey("  assembling"))

    @staticmethod
    def _footer(run: dict) -> None:
        usage = run.get("usage") or {}
        bits = [f"{k} {v}" for k, v in usage.items() if v]
        if bits:
            print(r.grey("  " + " . ".join(bits)))
        print()


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

    if name in ("exit", "quit", "q"):
        return False
    if name == "help":
        print(r.grey(HELP))
    elif name == "models":
        try:
            for m in session.client.models():
                role = m.get("role", "")
                mark = r.yellow("manager") if role == "manager" else r.grey(m.get("domain", ""))
                print(f"  {r.bold(m.get('id', '?'))}  {mark}")
                if m.get("summary"):
                    print(r.grey(f"      {m['summary']}"))
        except NimbusError as exc:
            print(r.red("  " + str(exc)))
    elif name == "agent":
        if rest in ("on", "off"):
            session.agent = rest == "on"
        else:
            session.agent = not session.agent
        print(r.grey(f"  agent {'on' if session.agent else 'off'} - {session.workspace.root}"))
        if session.agent:
            print(r.grey("  it may ask to read, write and run here. Writes and commands ask first."))
    elif name == "cd":
        try:
            session.workspace = actions.Workspace(Path(rest or ".").resolve())
            print(r.grey(f"  {session.workspace.root}"))
        except Exception as exc:
            print(r.red(f"  {exc}"))
    elif name == "pwd":
        print(r.grey(f"  {session.workspace.root}"))
    elif name == "new":
        session.turns.clear()
        print(r.grey("  conversation cleared"))
    else:
        print(r.grey(f"  no such command: /{name}. /help lists them."))
    return True


# -- entry point ------------------------------------------------------------

def main(argv: list[str]) -> int:
    r.setup()

    agent = False
    words: list[str] = []
    for arg in argv:
        if arg in ("-a", "--agent"):
            agent = True
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

    session = Session(client, root, agent)

    if words:  # one question, then leave
        session.ask(" ".join(words))
        return 0

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
        except KeyboardInterrupt:
            print(r.grey("\n  stopped\n"))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
