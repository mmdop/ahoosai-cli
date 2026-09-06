"""The part that makes this an agent rather than a chat window.

Nimbus has no tool-calling. `/v1/ask` takes one string and returns one string,
so the loop is built here: a chip teaches the model a block syntax, this module
finds those blocks in the answer, runs them, and feeds the results back as the
next turn.

WHY A CHIP AND NOT A LONGER PROMPT

Chip content is appended *after* the model's own system prompt. That ordering is
the whole point -- it refines a model that already knows its job instead of
replacing it. A protocol pasted at the top of a user message competes with the
model's identity; one appended as a chip sits under it.

WHAT IS ALLOWED WITHOUT ASKING

Reading and listing. Nothing else. Writing a file and running a command both
change the machine, so both stop and ask, and both show exactly what they are
about to do first. An agent that edits without showing you the edit is a diff
you did not review.

PATHS

Every path is resolved and then checked to be inside the workspace root. This
is done after resolution, not before, because "safe-looking" is a property of
the string and escaping is a property of the resolved path.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from client import Chip

MAX_READ_BYTES = 120_000
MAX_OUTPUT_CHARS = 8_000

PROTOCOL = """
You are working inside a terminal on a real project. You may act on the machine
by ending your answer with one or more action blocks. The tool executes them and
sends you the results, then you continue.

    ```nimbus:read path/to/file```

    ```nimbus:list path/to/dir```

    ```nimbus:write path/to/file
    ...the complete new contents of the file...
    ```

    ```nimbus:run
    a single shell command
    ```

Rules that matter:

- Paths are relative to the project root. Never use absolute paths or `..`.
- `write` replaces the whole file. Include every line you want to keep.
- Read a file before rewriting it. Editing a file you have not seen is guessing.
- One command per `run` block.
- Ask for what you need and then stop. Do not narrate the actions you are about
  to request; the person can see them.
- When the work is done, answer normally with no action blocks. That is how the
  loop ends.
"""


def protocol_chip(root: Path, listing: str) -> Chip:
    """The protocol, plus what is actually in front of us right now."""
    return Chip(
        name="terminal",
        content=f"{PROTOCOL}\nProject root: {root.name}\n\nTop level:\n{listing}\n",
    )


BLOCK = re.compile(
    r"```nimbus:(read|list|write|run)[ \t]*([^\n`]*)\n?(.*?)```",
    re.DOTALL,
)


@dataclass
class Action:
    kind: str
    target: str
    body: str

    @property
    def mutating(self) -> bool:
        return self.kind in ("write", "run")

    def describe(self) -> str:
        if self.kind == "run":
            return f"run: {self.body.strip()}"
        if self.kind == "write":
            return f"write: {self.target}  ({len(self.body.splitlines())} lines)"
        return f"{self.kind}: {self.target}"


def find(answer: str) -> list[Action]:
    """Every action block in an answer, in the order the model wrote them."""
    found = []
    for kind, target, body in BLOCK.findall(answer):
        target = target.strip()
        # A one-line read is written ```nimbus:read path``` with the path on the
        # info line and nothing in the body; a run puts its command in the body.
        if kind in ("read", "list") and not target and body.strip():
            target = body.strip().splitlines()[0].strip()
        found.append(Action(kind=kind, target=target, body=body))
    return found


def strip(answer: str) -> str:
    """The prose, without the blocks -- what is worth printing to the person."""
    return BLOCK.sub("", answer).strip()


class Workspace:
    """The directory the agent may touch, and the only one."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def resolve(self, raw: str) -> Path:
        candidate = (self.root / raw).resolve()
        # Checked after resolving: ".." and symlinks only show themselves here.
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError(f"{raw} is outside the project root")
        return candidate

    def listing(self, limit: int = 40) -> str:
        names = []
        for entry in sorted(self.root.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            if entry.name.startswith("."):
                continue
            names.append(entry.name + ("/" if entry.is_dir() else ""))
            if len(names) >= limit:
                names.append("...")
                break
        return "\n".join(names)

    # -- the four verbs -----------------------------------------------------

    def read(self, raw: str) -> str:
        path = self.resolve(raw)
        if not path.is_file():
            return f"no such file: {raw}"
        data = path.read_bytes()[:MAX_READ_BYTES]
        text = data.decode("utf-8", errors="replace")
        if path.stat().st_size > MAX_READ_BYTES:
            text += f"\n... [truncated at {MAX_READ_BYTES} bytes]"
        return text

    def list(self, raw: str) -> str:
        path = self.resolve(raw or ".")
        if not path.is_dir():
            return f"not a directory: {raw}"
        rows = []
        for entry in sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            if entry.name.startswith("."):
                continue
            rows.append(f"{entry.name}/" if entry.is_dir() else f"{entry.name}  {entry.stat().st_size}b")
        return "\n".join(rows) or "(empty)"

    def write(self, raw: str, body: str) -> str:
        path = self.resolve(raw)
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.is_file()
        path.write_text(body, encoding="utf-8", newline="\n")
        return f"{'replaced' if existed else 'created'} {raw} ({len(body.splitlines())} lines)"

    def run(self, command: str) -> str:
        try:
            done = subprocess.run(
                command, shell=True, cwd=self.root, capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=300,
            )
        except subprocess.TimeoutExpired:
            return "the command was still running after 300s and was stopped"
        out = (done.stdout or "") + (("\n" + done.stderr) if done.stderr else "")
        out = out.strip() or "(no output)"
        if len(out) > MAX_OUTPUT_CHARS:
            out = out[:MAX_OUTPUT_CHARS] + f"\n... [truncated, exit {done.returncode}]"
        return f"exit {done.returncode}\n{out}"

    def perform(self, action: Action) -> str:
        if action.kind == "read":
            return self.read(action.target)
        if action.kind == "list":
            return self.list(action.target)
        if action.kind == "write":
            return self.write(action.target, action.body)
        if action.kind == "run":
            return self.run(action.body.strip())
        return f"unknown action: {action.kind}"
