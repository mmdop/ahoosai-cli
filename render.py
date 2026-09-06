"""Putting an answer on a terminal without making it worse.

A model's answer is markdown, and markdown printed raw is asterisks and
backticks. Rendered too hard, it is a magazine in a shell. What is here is the
middle: headings and emphasis lose their punctuation, code keeps its frame, and
nothing is reflowed -- a wrapped code block is a broken code block.

WINDOWS

cmd.exe and PowerShell both understand ANSI, but only once virtual terminal
processing is switched on for the handle, which is off by default in older
console hosts. If that call fails, colour is dropped rather than printed as
escape sequences, because escape sequences in a log are worse than plain text.
"""

from __future__ import annotations

import os
import re
import shutil
import sys

# -- colour -----------------------------------------------------------------

COLOR = True


def _enable_windows_ansi() -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        for handle_id in (-11, -12):  # stdout, stderr
            handle = kernel32.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                continue
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return True
    except Exception:
        return False


def setup() -> None:
    """Called once at start: UTF-8 out, colour on if the terminal will take it."""
    global COLOR
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        COLOR = False
    else:
        COLOR = _enable_windows_ansi()


def c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLOR else text


def dim(t: str) -> str:      return c("2", t)
def bold(t: str) -> str:     return c("1", t)
def red(t: str) -> str:      return c("31", t)
def green(t: str) -> str:    return c("32", t)
def yellow(t: str) -> str:   return c("33", t)
def blue(t: str) -> str:     return c("34", t)
def cyan(t: str) -> str:     return c("36", t)
def grey(t: str) -> str:     return c("90", t)


def width() -> int:
    return max(40, min(shutil.get_terminal_size((100, 30)).columns, 110))


# -- markdown ---------------------------------------------------------------

FENCE = re.compile(r"^\s*```([^\n]*)$")
BOLD = re.compile(r"\*\*(.+?)\*\*")
ITALIC = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
TICK = re.compile(r"`([^`\n]+)`")
HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
BULLET = re.compile(r"^(\s*)([-*+])\s+(.*)$")


def _inline(text: str) -> str:
    text = BOLD.sub(lambda m: bold(m.group(1)), text)
    text = ITALIC.sub(lambda m: c("3", m.group(1)), text)
    text = TICK.sub(lambda m: cyan(m.group(1)), text)
    return text


def answer(text: str) -> str:
    """Render one answer. Code blocks are framed; prose is lightly styled."""
    out: list[str] = []
    in_code = False
    lang = ""
    body: list[str] = []

    for line in text.splitlines():
        fence = FENCE.match(line)
        if fence:
            if in_code:
                out.append(_code_block(lang, body))
                in_code, lang, body = False, "", []
            else:
                in_code, lang, body = True, fence.group(1).strip(), []
            continue
        if in_code:
            body.append(line)
            continue

        heading = HEADING.match(line)
        if heading:
            out.append("")
            out.append(bold(heading.group(2)))
            out.append(grey("─" * min(len(heading.group(2)), width())))
            continue
        bullet = BULLET.match(line)
        if bullet:
            out.append(f"{bullet.group(1)}{cyan('•')} {_inline(bullet.group(3))}")
            continue
        out.append(_inline(line))

    if in_code:  # a fence the model never closed
        out.append(_code_block(lang, body))
    return "\n".join(out)


def _code_block(lang: str, lines: list[str]) -> str:
    """A framed block. Never wrapped: a wrapped line of code is a wrong line."""
    label = lang or "text"
    inner = width() - 2
    top = grey("┌─ ") + yellow(label) + grey(" " + "─" * max(0, inner - len(label) - 4) + "┐")
    bottom = grey("└" + "─" * inner + "┘")
    body = [grey("│ ") + line for line in lines]
    return "\n".join(["", top, *body, bottom, ""])


# -- small pieces the REPL uses ---------------------------------------------

def rule(label: str = "") -> str:
    w = width()
    if not label:
        return grey("─" * w)
    return grey("── " + label + " " + "─" * max(0, w - len(label) - 4))


def banner(url: str, model_count: int) -> str:
    return "\n".join([
        bold("Nimbus") + grey("  a team of models, in your terminal"),
        grey(f"  {url}  ·  {model_count} models  ·  /help for commands"),
        "",
    ])
