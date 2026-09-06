# nimbus-cli

A terminal client for [Nimbus](https://ahoos-ai.site) — a family of models where
a manager reads your request, hands it to the specialist that owns the domain,
and assembles the answer.

It chats, and in agent mode it reads files, writes them and runs commands in the
directory you started it in.

```
> refactor the retry logic in client.py so a 429 backs off

  plan: nimbus-backend-rza-1.0
  -> nimbus-backend-rza-1.0
  <- nimbus-backend-rza-1.0 done
  assembling

The current loop retries every failure at the same interval, which turns a rate
limit into a faster rate limit. ...

── write ──────────────────────────────────────────────────
  client.py
  | import time
  | ...
───────────────────────────────────────────────────────────
  do it? [y/N]
```

## Installing

There is nothing to install. Python 3.11 or newer, standard library only.

```bash
git clone https://github.com/mmdop/nimbus-cli
cd nimbus-cli
cp .env.example .env      # then put your server URL and key in it
python nimbus.py
```

On Windows, `copy .env.example .env`.

## Using it

```bash
python nimbus.py                      # chat
python nimbus.py -a                   # agent mode, in the current directory
python nimbus.py "why is this slow"   # ask once and exit
```

| | |
|---|---|
| `/help` | the commands |
| `/models` | what the server has, and which one manages |
| `/agent [on\|off]` | let it read, write and run here |
| `/cd <path>` | change the directory it works in |
| `/new` | forget the conversation |
| `/exit` | leave |

## What agent mode actually does

Nimbus has no tool-calling. `/v1/ask` takes one string and returns one string.
So the loop is built on this side: a **chip** teaches the model a block syntax,
the client finds those blocks in the answer, runs them, and sends the results
back as the next turn.

A chip is the right place for it because chip content is appended *after* the
model's own system prompt. It refines a model that already knows its job rather
than competing with it — which is what a protocol pasted into a user message
ends up doing.

The model can ask for four things:

```
```nimbus:read path```          the contents of a file
```nimbus:list path```          what is in a directory
```nimbus:write path```         replace a file, contents in the block
```nimbus:run```                one shell command, in the block
```

**Reading and listing happen without asking. Writing and running always ask,**
every single time, and show you the command or the first twenty lines of the
file before you answer. There is no "yes to all", because the value of the
prompt is that you read it.

Every path is resolved and then checked to be inside the directory you started
in. That check is after resolution, not before: `..` and symlinks only reveal
themselves once the path is real.

The loop stops after 8 steps, or as soon as an answer arrives with no blocks in
it. Both are ways of ending; the second is the normal one.

## Why the progress lines

`/v1/ask/stream` does not stream tokens. It streams what the family is *doing* —
the plan, each delegation, each result — and then sends the finished answer in
one event. A client that drains those events shows nothing for thirty seconds
and looks hung, so this prints them. The routing is the interesting part anyway.

## Configuration

Read from the environment, or from a `.env` beside the script or in the current
directory. Real environment variables win: an export is a decision about this
run, a file is a default from some earlier day.

| | |
|---|---|
| `NIMBUS_URL` | the server. Defaults to `http://127.0.0.1:8000` |
| `NIMBUS_API_KEY` | required for asking; `/v1/models` is open |
| `NIMBUS_TIMEOUT` | seconds, default 600. A sleeping free-tier server needs most of a minute to wake |

## The files

| | |
|---|---|
| `nimbus.py` | the REPL, the conversation, the agent loop |
| `client.py` | HTTP and server-sent events, and turning an error body into a sentence |
| `actions.py` | the block protocol, the workspace, and what may run without asking |
| `render.py` | markdown on a terminal, and switching Windows consoles into ANSI |

## Known limits

- The answer arrives in one piece. That is the server's shape, not a choice here.
- History is folded into the prompt, so a long conversation costs tokens on
  every turn. `/new` when the subject changes.
- `write` replaces a whole file. There is no patch verb yet, so the model has to
  read a file before it can safely rewrite it — and it is told to.

## Licence

MIT.
