"""A full-screen terminal interface, written against the terminal itself.

WHY NO LIBRARY

textual and prompt_toolkit would both do this better than a few hundred lines
can. They also turn a `git clone` into a dependency tree, on every machine this
has to run on, for a program whose entire job is to talk to one HTTP endpoint.
The alternate screen buffer, cursor addressing and raw key input are already in
the terminal and in the standard library. What was missing is written here.

THE LAYOUT, AND WHY IT SITS ON THE BOTTOM

A conversation grows downward, and the thing you are reading is the newest
thing. So the transcript is anchored to the bottom of its pane and an empty
session shows an empty top rather than a screenful of nothing under three lines
of text. The input is a box, not a line, because it is where you are: it should
be the most solid object on screen.

Every message carries a role, and the role is what makes a wall of text
readable -- who said this, and is this the model working or the model answering.
Session passes the role through `write`; the plain front end ignores it, which
is exactly what an optional argument is for.

HOW IT IS PUT TOGETHER

One worker thread runs the request; the main thread owns the screen and the
keyboard and never blocks for longer than a tick. Everything shared between
them is one list of finished lines behind one lock. A UI that repaints from two
threads is a UI that tears.

Painting is whole-frame. Each visible row is written with the cursor placed on
it and the rest of the line erased, so nothing has to be tracked between frames
and a resize is simply a differently shaped frame.

WRAPPING WITH COLOUR IN IT

Lines arrive carrying ANSI codes, so measuring them with len() gives the wrong
width and cutting them blindly leaves a colour switched on for the rest of the
screen. The wrapper counts only printable characters and carries the active
code across the break.
"""

from __future__ import annotations

import os
import re
import shutil
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
RESET = f"{ESC}[0m"
SGR = re.compile(r"\033\[[0-9;]*m")

# 256-colour, because 16-colour "bright black" is a different colour on every
# terminal theme and these bars have to sit quietly behind the text.
BAR = f"{ESC}[48;5;236m"
INK = f"{ESC}[38;5;252m"
FAINT = f"{ESC}[38;5;244m"
ACCENT = f"{ESC}[38;5;75m"
GOLD = f"{ESC}[38;5;179m"

GUTTER = {
    "you": ACCENT + "▍ " + RESET,
    "answer": GOLD + "▍ " + RESET,
    "error": f"{ESC}[38;5;203m" + "▍ " + RESET,
}
LABEL = {"you": "you", "answer": "nimbus"}


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
            active = "" if code == RESET else code
        size = visible(token)
        if count + size > width and count:
            rows.append(row + (RESET if active else ""))
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

        Returns a printable character, or a name: enter, backspace, left,
        right, up, down, home, end, pgup, pgdn, escape, ctrl-c, ctrl-d, ctrl-u.
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
                return {
                    "H": "up", "P": "down", "K": "left", "M": "right",
                    "G": "home", "O": "end", "I": "pgup", "Q": "pgdn",
                    "S": "delete",
                }.get(msvcrt.getwch(), "")
            return self._name(ch)

        import select

        if not select.select([sys.stdin], [], [], timeout)[0]:
            return None
        ch = sys.stdin.read(1)
        if ch == ESC:
            if not select.select([sys.stdin], [], [], 0.05)[0]:
                return "escape"
            if sys.stdin.read(1) != "[":
                return "escape"
            body = ""
            while True:
                nxt = sys.stdin.read(1)
                body += nxt
                if nxt.isalpha() or nxt == "~":
                    break
            return {
                "A": "up", "B": "down", "C": "right", "D": "left",
                "H": "home", "F": "end", "3~": "delete", "5~": "pgup", "6~": "pgdn",
            }.get(body, "")
        return self._name(ch)

    @staticmethod
    def _name(ch: str) -> str:
        return {
            "\r": "enter", "\n": "enter", "\x7f": "backspace", "\b": "backspace",
            "\x03": "ctrl-c", "\x04": "ctrl-d", "\x0c": "ctrl-l", "\x15": "ctrl-u",
            "\x01": "home", "\x05": "end",
        }.get(ch, ch if ch.isprintable() else "")


# -- the app ----------------------------------------------------------------

class App:
    """Screen, keyboard, and the UI a Session talks to."""

    TICK = 0.2
    CHROME = 5  # header, status, and the three rows of the input box

    def __init__(self, client: Client, root: Path, agent: bool, health: dict) -> None:
        self.client = client
        self.health = health
        self.rows: list[str] = []          # finished, already-styled lines
        self.lock = threading.Lock()
        self.scroll = 0                    # rows above the tail; 0 follows it
        self.entry = ""
        self.caret = 0
        self.history: list[str] = []
        self.history_at = 0
        self.working_since: float | None = None
        self.running = True
        self.dirty = True
        self.last_role: str | None = None

        self.pending: actions.Action | None = None
        self.answered = threading.Event()
        self.answer = False

        import nimbus  # deferred: nimbus imports this module to start it

        self.session = nimbus.Session(client, root, agent, self)
        self.command = nimbus.command

        self.write(f"{FAINT}  {client.config.url}   {health.get('models', 0)} models{RESET}")
        self.write(f"{FAINT}  {root}{RESET}")
        self.write(f"{FAINT}  ask anything, or /help for commands{RESET}")

    # -- the UI a Session sees ---------------------------------------------

    def write(self, text: str, role: str | None = None) -> None:
        gutter = GUTTER.get(role or "", "")
        with self.lock:
            if role in LABEL and role != self.last_role:
                self.rows.append("")
                self.rows.append(f"{FAINT}  {LABEL[role]}{RESET}")
            self.last_role = role
            for line in text.split("\n"):
                self.rows.append(("  " + gutter + line) if gutter else line)
            self.scroll = 0  # new output pulls the view back to the tail
            self.dirty = True

    def busy(self, working: bool) -> None:
        self.working_since = time.monotonic() if working else None
        self.dirty = True

    def confirm(self, action: actions.Action) -> bool:
        """Ask, from the worker thread, and wait for the main thread to answer."""
        head = f"{GOLD}  {action.kind}  {RESET}{r.bold(action.target or action.body.strip())}"
        self.write("")
        self.write(head)
        if action.kind == "write":
            body = action.body.splitlines()
            for line in body[:20]:
                self.write(f"{FAINT}  │ {RESET}{line}")
            if len(body) > 20:
                self.write(f"{FAINT}  │ ... {len(body) - 20} more lines{RESET}")
        self.answered.clear()
        self.pending = action
        self.dirty = True
        self.answered.wait()
        self.pending = None
        self.dirty = True
        return self.answer

    # -- painting -----------------------------------------------------------

    @staticmethod
    def size() -> tuple[int, int]:
        size = shutil.get_terminal_size((100, 30))
        return max(48, size.columns), max(12, size.lines)

    def paint(self) -> None:
        width, height = self.size()
        body = height - self.CHROME
        # Two for the frame margin, four for the gutter a role block adds.
        r.set_width(width - 6)

        with self.lock:
            painted: list[str] = []
            for line in self.rows:
                painted.extend(wrap(line, width - 2))

        top = max(0, len(painted) - body - self.scroll)
        view = painted[top:top + body]
        # Anchored to the bottom: a conversation grows downward, and an empty
        # session should show an empty top rather than a screen of nothing
        # underneath three lines of text.
        view = [""] * (body - len(view)) + view

        out = [f"{ESC}[H", self._header(width), CLEAR_LINE, "\n"]
        for row in view:
            out += [row, CLEAR_LINE, "\n"]
        out += [self._status(width), CLEAR_LINE, "\n"]
        for row in self._box(width):
            out += [row, CLEAR_LINE, "\n"]
        sys.stdout.write("".join(out[:-1]))
        sys.stdout.flush()

    def _bar(self, left: str, right: str, width: int) -> str:
        gap = max(1, width - visible(left) - visible(right))
        return BAR + left + " " * gap + right + RESET

    def _header(self, width: int) -> str:
        left = f"{ESC}[1m{INK} AhoosAI{RESET}{BAR}{FAINT}  nimbus{RESET}{BAR}"
        state = "agent" if self.session.agent else "chat"
        right = f"{ACCENT if self.session.agent else FAINT}{state} {RESET}{BAR}"
        return self._bar(left, right, width)

    def _status(self, width: int) -> str:
        if self.pending is not None:
            return self._bar(f"{GOLD} run it?{RESET}{BAR}", f"{FAINT}y = yes   n = no {RESET}{BAR}", width)
        if self.working_since is not None:
            seconds = int(time.monotonic() - self.working_since)
            note = "   a sleeping free tier takes about a minute" if seconds >= 20 else ""
            return self._bar(f"{GOLD} working {seconds}s{note}{RESET}{BAR}", "", width)
        if self.scroll:
            return self._bar(f"{FAINT} scrolled up {self.scroll}{RESET}{BAR}",
                             f"{FAINT}pgdn to follow {RESET}{BAR}", width)
        return self._bar(f"{FAINT} {self.workdir()}{RESET}{BAR}",
                         f"{FAINT}enter send   ctrl-d quit {RESET}{BAR}", width)

    def workdir(self) -> str:
        name = self.session.workspace.root.name
        return name or str(self.session.workspace.root)

    def _box(self, width: int) -> list[str]:
        """The input, drawn as an object. It is where you are; it should look it."""
        inner = width - 2
        room = inner - 4
        text = self.entry
        start = max(0, self.caret - room + 1)
        shown = text[start:start + room]
        caret_at = self.caret - start

        lit = shown[:caret_at]
        under = shown[caret_at:caret_at + 1] or " "
        rest = shown[caret_at + 1:]
        field = f"{INK}{lit}{ESC}[7m{under}{RESET}{INK}{rest}{RESET}"
        # At the end of the line the caret is a space that is not in `shown`,
        # so padding measured from `shown` leaves the row one column too wide
        # and the right border falls off the screen.
        pad = " " * max(0, room - len(lit) - len(under) - len(rest))

        edge = ACCENT if self.working_since is None else GOLD
        return [
            f"{edge}╭{'─' * inner}╮{RESET}",
            f"{edge}│{RESET} {ACCENT}❯{RESET} {field}{pad} {edge}│{RESET}",
            f"{edge}╰{'─' * inner}╯{RESET}",
        ]

    # -- the loop -----------------------------------------------------------

    def run(self) -> int:
        sys.stdout.write(ALT_ON + HIDE)
        sys.stdout.flush()
        try:
            with Keys() as keys:
                last_size = self.size()
                while self.running:
                    if self.size() != last_size:
                        last_size, self.dirty = self.size(), True
                        sys.stdout.write(f"{ESC}[2J")
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

        page = max(1, self.size()[1] - self.CHROME - 1)
        if key == "ctrl-d":
            self.running = False
        elif key in ("ctrl-c", "ctrl-u"):
            self.entry, self.caret = "", 0
        elif key == "pgup":
            self.scroll += page
        elif key == "pgdn":
            self.scroll = max(0, self.scroll - page)
        elif key == "left":
            self.caret = max(0, self.caret - 1)
        elif key == "right":
            self.caret = min(len(self.entry), self.caret + 1)
        elif key == "home":
            self.caret = 0
        elif key == "end":
            self.caret = len(self.entry)
        elif key == "up":
            if self.history and self.history_at > 0:
                self.history_at -= 1
                self.entry = self.history[self.history_at]
                self.caret = len(self.entry)
        elif key == "down":
            if self.history_at < len(self.history) - 1:
                self.history_at += 1
                self.entry = self.history[self.history_at]
            else:
                self.history_at = len(self.history)
                self.entry = ""
            self.caret = len(self.entry)
        elif key == "backspace":
            if self.caret:
                self.entry = self.entry[:self.caret - 1] + self.entry[self.caret:]
                self.caret -= 1
        elif key == "delete":
            self.entry = self.entry[:self.caret] + self.entry[self.caret + 1:]
        elif key == "enter":
            self.submit()
        elif len(key) == 1:
            self.entry = self.entry[:self.caret] + key + self.entry[self.caret:]
            self.caret += 1

    def submit(self) -> None:
        line = self.entry.strip()
        if not line or self.working_since is not None:
            return
        self.entry, self.caret = "", 0
        self.history.append(line)
        self.history_at = len(self.history)
        self.write(line, role="you")

        if line.startswith("/"):
            self.last_role = None
            if not self.command(line, self.session):
                self.running = False
            return

        threading.Thread(target=self._work, args=(line,), daemon=True).start()

    def _work(self, line: str) -> None:
        try:
            self.session.ask(line)
        except Exception as exc:  # a worker that dies silently is a hang
            self.write(f"{type(exc).__name__}: {exc}", role="error")
        finally:
            self.busy(False)


def run(client: Client, root: Path, agent: bool, health: dict) -> int:
    return App(client, root, agent, health).run()
