"""A full-screen terminal interface, written against the terminal itself.

WHY NO LIBRARY

textual and prompt_toolkit would both do this better than a few hundred lines
can. They also turn a `git clone` into a dependency tree, on every machine this
has to run on, for a program whose entire job is to talk to one HTTP endpoint.
The alternate screen buffer, cursor addressing and raw key input are already in
the terminal and in the standard library. What was missing is written here.

HOW IT IS PUT TOGETHER

One worker thread runs the request; the main thread owns the screen and the
keyboard, and never blocks for longer than a tick. Everything shared between
them is one list of finished lines behind one lock. That is the whole
concurrency story, and it is deliberately that small: a UI that repaints from
two threads is a UI that tears.

Repainting is whole-frame. Each visible row is written with the cursor placed
on it and the rest of the line erased, so nothing has to be tracked between
frames and a resize is just a differently shaped frame.

WRAPPING WITH COLOUR IN IT

Lines arrive already carrying ANSI codes, so measuring them by len() gives the
wrong width and cutting them blindly leaves a colour turned on forever. The
wrapper below counts only printable characters and carries the active code
across the break.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from pathlib import Path

import actions
import render as r
from client import Client

ESC = "\033"
ALT_ON, ALT_OFF = f"{ESC}[?1049h", f"{ESC}[?1049l"
HIDE, SHOW = f"{ESC}[?25l", f"{ESC}[?25h"
CLEAR_LINE = f"{ESC}[K"
SGR = re.compile(r"\033\[[0-9;]*m")


# -- text -------------------------------------------------------------------

def visible(text: str) -> int:
    return len(SGR.sub("", text))


def wrap(text: str, width: int) -> list[str]:
    """Wrap one coloured line to `width` printable columns.

    Words are kept whole where they fit. A word longer than the width is cut,
    because the alternative is a row that overflows and smears the frame.
    """
    if visible(text) <= width:
        return [text]

    rows: list[str] = []
    row, count, active = "", 0, ""
    for token in re.split(r"(\s+)", text):
        for code in SGR.findall(token):
            active = "" if code == f"{ESC}[0m" else code
        size = visible(token)
        if count + size > width and count:
            rows.append(row + (f"{ESC}[0m" if active else ""))
            row, count = active, 0
            if not token.strip():
                continue
        while size > width:  # a single token longer than the whole row
            rows.append(row + token[:width])
            token, size, row, count = token[width:], size - width, active, 0
        row += token
        count += size
    if row.strip():
        rows.append(row)
    return rows or [""]


# -- keyboard ---------------------------------------------------------------

class Keys:
    """Raw key reading, polled so the main loop never blocks on input."""

    def __init__(self) -> None:
        self.windows = os.name == "nt"
        self._saved = None

    def __enter__(self) -> "Keys":
        if not self.windows:
            import termios
            import tty

            self._saved = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
        return self

    def __exit__(self, *_: object) -> None:
        if not self.windows and self._saved is not None:
            import termios

            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self._saved)

    def get(self, timeout: float) -> str | None:
        """One key, or None if nothing arrived before the timeout.

        Returns a printable character, or a name: enter, backspace, up, down,
        pgup, pgdn, ctrl-c, ctrl-d, ctrl-l.
        """
        if self.windows:
            import msvcrt

            deadline = time.monotonic() + timeout
            while not msvcrt.kbhit():
                if time.monotonic() >= deadline:
                    return None
                time.sleep(0.01)
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):  # a two-part special key
                return {"H": "up", "P": "down", "I": "pgup", "Q": "pgdn"}.get(msvcrt.getwch(), "")
            return self._name(ch)

        import select

        if not select.select([sys.stdin], [], [], timeout)[0]:
            return None
        ch = sys.stdin.read(1)
        if ch == ESC:  # an escape sequence, or a lone escape key
            if not select.select([sys.stdin], [], [], 0.05)[0]:
                return "escape"
            rest = sys.stdin.read(1)
            if rest != "[":
                return "escape"
            body = ""
            while True:
                nxt = sys.stdin.read(1)
                body += nxt
                if nxt.isalpha() or nxt == "~":
                    break
            return {"A": "up", "B": "down", "5~": "pgup", "6~": "pgdn"}.get(body, "")
        return self._name(ch)

    @staticmethod
    def _name(ch: str) -> str:
        if ch in ("\r", "\n"):
            return "enter"
        if ch in ("\x7f", "\b"):
            return "backspace"
        if ch == "\x03":
            return "ctrl-c"
        if ch == "\x04":
            return "ctrl-d"
        if ch == "\x0c":
            return "ctrl-l"
        return ch if ch.isprintable() else ""


# -- the app ----------------------------------------------------------------

class App:
    """Screen, keyboard, and the UI a Session talks to."""

    TICK = 0.25

    def __init__(self, client: Client, root: Path, agent: bool, health: dict) -> None:
        self.client = client
        self.health = health
        self.lines: list[str] = []
        self.lock = threading.Lock()
        self.scroll = 0          # rows from the bottom; 0 is following the tail
        self.entry = ""
        self.history: list[str] = []
        self.history_at = 0
        self.working_since: float | None = None
        self.running = True
        self.dirty = True

        self.pending: actions.Action | None = None
        self.answered = threading.Event()
        self.answer: bool = False

        import nimbus  # imported here: nimbus imports this module to start it

        self.session = nimbus.Session(client, root, agent, self)
        self.command = nimbus.command

        self.write(r.grey(f"  {client.config.url}  ·  {health.get('models', 0)} models"))
        self.write(r.grey(f"  {root}"))
        self.write(r.grey("  /help for commands  ·  Ctrl-D to leave"))
        self.write("")

    # -- the UI a Session sees ---------------------------------------------

    def write(self, text: str) -> None:
        with self.lock:
            self.lines.extend(text.split("\n"))
            self.scroll = 0  # new output pulls the view back to the tail
            self.dirty = True

    def busy(self, working: bool) -> None:
        self.working_since = time.monotonic() if working else None
        self.dirty = True

    def confirm(self, action: actions.Action) -> bool:
        """Ask, from the worker thread, and wait for the main thread to answer."""
        self.write("")
        self.write(r.yellow(f"  {action.kind}: ") + r.bold(action.target or action.body.strip()))
        if action.kind == "write":
            for line in action.body.splitlines()[:20]:
                self.write(r.grey("  | ") + line)
            extra = len(action.body.splitlines()) - 20
            if extra > 0:
                self.write(r.grey(f"  | ... {extra} more lines"))
        self.answered.clear()
        self.pending = action
        self.dirty = True
        self.answered.wait()
        self.pending = None
        self.dirty = True
        return self.answer

    # -- painting -----------------------------------------------------------

    def size(self) -> tuple[int, int]:
        import shutil

        size = shutil.get_terminal_size((100, 30))
        return max(40, size.columns), max(10, size.lines)

    def paint(self) -> None:
        width, height = self.size()
        body_height = height - 3  # header, status, input

        with self.lock:
            rows: list[str] = []
            for line in self.lines:
                rows.extend(wrap(line, width - 1))

        top = max(0, len(rows) - body_height - self.scroll)
        view = rows[top:top + body_height]
        view += [""] * (body_height - len(view))

        out = [f"{ESC}[H", self._header(width), CLEAR_LINE, "\n"]
        for row in view:
            out += [row, CLEAR_LINE, "\n"]
        out += [self._status(width), CLEAR_LINE, "\n", self._input(width), CLEAR_LINE]
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def _header(self, width: int) -> str:
        left = r.bold(" AhoosAI ") + r.grey("nimbus")
        right = r.grey(("agent" if self.session.agent else "chat") + " ")
        gap = max(1, width - visible(left) - visible(right))
        return left + " " * gap + right

    def _status(self, width: int) -> str:
        if self.pending is not None:
            return r.yellow("  do it?  ") + r.grey("y = yes,  n = no")
        if self.working_since is not None:
            seconds = int(time.monotonic() - self.working_since)
            note = "  (a sleeping free tier takes about a minute to wake)" if seconds >= 20 else ""
            return r.grey(f"  working  {seconds}s{note}")
        hint = "  enter to send  ·  pgup/pgdn to scroll  ·  ctrl-d to leave"
        if self.scroll:
            hint = f"  scrolled {self.scroll} rows up  ·  pgdn to follow again"
        return r.grey(hint)

    def _input(self, width: int) -> str:
        prompt = r.cyan(" > ")
        room = width - 4
        shown = self.entry[-room:] if len(self.entry) > room else self.entry
        return prompt + shown

    # -- the loop -----------------------------------------------------------

    def run(self) -> int:
        sys.stdout.write(ALT_ON + HIDE)
        sys.stdout.flush()
        try:
            with Keys() as keys:
                while self.running:
                    if self.dirty or self.working_since is not None:
                        self.dirty = False
                        self.paint()
                    key = keys.get(self.TICK)
                    if key:
                        self.key(key)
        finally:
            sys.stdout.write(SHOW + ALT_OFF)
            sys.stdout.flush()
        return 0

    def key(self, key: str) -> None:
        self.dirty = True

        if self.pending is not None:  # a confirmation is on screen
            if key in ("y", "Y"):
                self.answer = True
                self.answered.set()
            elif key in ("n", "N", "escape", "enter", "ctrl-c"):
                self.answer = False
                self.answered.set()
            return

        if key == "ctrl-d":
            self.running = False
        elif key == "ctrl-c":
            self.entry = ""
        elif key == "ctrl-l":
            pass  # the next paint is a full frame anyway
        elif key == "pgup":
            self.scroll += max(1, self.size()[1] - 5)
        elif key == "pgdn":
            self.scroll = max(0, self.scroll - max(1, self.size()[1] - 5))
        elif key == "up":
            if self.history and self.history_at > 0:
                self.history_at -= 1
                self.entry = self.history[self.history_at]
        elif key == "down":
            if self.history_at < len(self.history) - 1:
                self.history_at += 1
                self.entry = self.history[self.history_at]
            else:
                self.history_at = len(self.history)
                self.entry = ""
        elif key == "backspace":
            self.entry = self.entry[:-1]
        elif key == "enter":
            self.submit()
        elif len(key) == 1:
            self.entry += key

    def submit(self) -> None:
        line = self.entry.strip()
        self.entry = ""
        if not line or self.working_since is not None:
            return
        self.history.append(line)
        self.history_at = len(self.history)
        self.write(r.cyan("> ") + line)

        if line.startswith("/"):
            if not self.command(line, self.session):
                self.running = False
            self.write("")
            return

        threading.Thread(target=self._work, args=(line,), daemon=True).start()

    def _work(self, line: str) -> None:
        try:
            self.session.ask(line)
        except Exception as exc:  # a worker that dies silently is a hang
            self.write(r.red(f"  {type(exc).__name__}: {exc}"))
        finally:
            self.busy(False)
            self.write("")


def run(client: Client, root: Path, agent: bool, health: dict) -> int:
    return App(client, root, agent, health).run()
