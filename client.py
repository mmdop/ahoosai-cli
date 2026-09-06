"""Talking to a Nimbus server from a terminal.

Standard library only, like the rest of the project. The whole client is one
POST and an SSE reader, so a dependency would buy nothing and cost a wheel on
every machine this has to run on.

WHY THE PROGRESS EVENTS MATTER HERE

`/v1/ask/stream` does not stream tokens. It streams *what the family is doing* --
the plan, each delegation, each result -- and then sends the finished answer in
one `done` event. A client that ignores those events shows a blank terminal for
thirty seconds and looks hung. So they are surfaced, not drained.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator


class NimbusError(RuntimeError):
    """A failure the person can act on, phrased as itself rather than as a code."""


def load_env(root: Path) -> None:
    """Read .env into the environment without overwriting what is already set.

    Real environment variables win. A shell export is a deliberate act for this
    one run; a line in a file is a default from some earlier day.
    """
    path = root / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class Config:
    url: str
    key: str = ""
    timeout: int = 600
    engine: str | None = None

    @classmethod
    def from_env(cls) -> "Config":
        url = os.environ.get("NIMBUS_URL", "http://127.0.0.1:8000").rstrip("/")
        return cls(
            url=url,
            key=os.environ.get("NIMBUS_API_KEY", ""),
            timeout=int(os.environ.get("NIMBUS_TIMEOUT", "600")),
            engine=os.environ.get("NIMBUS_ENGINE") or None,
        )


@dataclass
class Chip:
    name: str
    content: str

    def payload(self) -> dict[str, str]:
        # The server caps content at 8000 characters. Truncating here with a
        # visible marker beats a 422 that says nothing about which chip was long.
        content = self.content
        if len(content) > 8000:
            content = content[:7980] + "\n... [truncated]"
        return {"name": self.name[:80], "content": content}


@dataclass
class Client:
    config: Config
    _headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.config.key:
            # Bearer, because that is what require_key reads. An X-API-Key header
            # is not wrong-looking enough to notice: the server ignores it, the
            # request is simply unauthenticated, and 401 says "check your key"
            # about a key that was correct all along.
            self._headers["Authorization"] = f"Bearer {self.config.key}"

    # -- plumbing ----------------------------------------------------------

    def _request(self, method: str, path: str, body: dict | None = None) -> urllib.request.Request:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        return urllib.request.Request(
            self.config.url + path, data=data, method=method, headers=dict(self._headers)
        )

    def _call(self, method: str, path: str, body: dict | None = None) -> Any:
        try:
            with urllib.request.urlopen(self._request(method, path, body), timeout=self.config.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise NimbusError(self._explain(exc)) from exc
        except urllib.error.URLError as exc:
            raise NimbusError(f"cannot reach {self.config.url} - {exc.reason}") from exc

    @staticmethod
    def _explain(exc: urllib.error.HTTPError) -> str:
        """The reason is in the body, and the body is what gets thrown away.

        Every EngineError becomes a 502 whose body names the provider and the
        cause. Reading it is the difference between "502" and "openai says this
        request needs credit".
        """
        try:
            detail = json.loads(exc.read().decode("utf-8"))
            text = detail.get("detail") or detail.get("error") or ""
        except Exception:
            text = ""
        if exc.code == 401:
            return "the server rejected the key (401). Check NIMBUS_API_KEY."
        if exc.code == 404:
            return f"no such endpoint ({exc.code}). Is {exc.url} a Nimbus server?"
        return f"http {exc.code}: {text}" if text else f"http {exc.code}"

    # -- the endpoints this client uses ------------------------------------

    def health(self) -> dict:
        return self._call("GET", "/v1/health")

    def models(self) -> list[dict]:
        return self._call("GET", "/v1/models")

    def ask(
        self,
        prompt: str,
        *,
        chips: list[Chip] | None = None,
        language: str | None = None,
        on_event: Callable[[str, dict], None] | None = None,
    ) -> dict:
        """Run one request, reporting progress, and return the finished run."""
        body: dict[str, Any] = {"prompt": prompt}
        if chips:
            body["chips"] = [c.payload() for c in chips]
        if language:
            body["language"] = language
        if self.config.engine:
            body["engine"] = self.config.engine

        answer: dict | None = None
        for kind, data in self._sse("/v1/ask/stream", body):
            if kind == "error":
                raise NimbusError(data.get("detail") or data.get("error") or "the server reported an error")
            if kind == "done":
                answer = data
            elif on_event:
                on_event(kind, data)
        if answer is None:
            raise NimbusError("the stream ended before an answer arrived")
        return answer

    def _sse(self, path: str, body: dict) -> Iterator[tuple[str, dict]]:
        """Read server-sent events. One event is a set of lines, then a blank one."""
        request = self._request("POST", path, body)
        request.add_header("Accept", "text/event-stream")
        try:
            response = urllib.request.urlopen(request, timeout=self.config.timeout)
        except urllib.error.HTTPError as exc:
            raise NimbusError(self._explain(exc)) from exc
        except urllib.error.URLError as exc:
            raise NimbusError(f"cannot reach {self.config.url} - {exc.reason}") from exc

        kind = "message"
        data: list[str] = []
        with response:
            for raw in response:
                line = raw.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
                if not line:
                    if data:
                        try:
                            yield kind, json.loads("\n".join(data))
                        except json.JSONDecodeError:
                            pass
                    kind, data = "message", []
                    continue
                if line.startswith("event:"):
                    kind = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip())
