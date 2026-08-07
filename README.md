# CC Office

See every Claude Code window you have open at a glance: who's working, who's
waiting on you, who's stuck.

Each live window becomes a desk. Read-only, local-only, pure standard library.

## Run it

```sh
/usr/bin/python3 server.py
```

Then open http://localhost:8910

On macOS you can double-click `CCOffice.command` instead — it restarts the
server and opens the browser for you.

To stop it:

```sh
lsof -ti tcp:8910 | xargs kill
```

Set `CC_OFFICE_PORT` to use a different port.

## Three states

| Light | Meaning | How it's decided |
|---|---|---|
| Green (pulsing) | Working | Claude Code reports `busy` and the transcript is still growing |
| Amber | Waiting on you | Claude Code reports `idle` — it has spoken and is waiting |
| Red | Probably stuck | Reports `busy` but nothing has moved for 75s — usually a permission prompt sitting unanswered |

Amber and red sort to the front, because those are the ones that need you.

## What a desk shows

- **Speech bubble** — the tool call in flight while working; the last thing it
  said to you once it stops
- **Name** — whatever you named it, or the most recent instruction with enough
  substance to recognize the job by
- **"You last said"** — your most recent line, however short
- **Blue badges** — subagents it dispatched that haven't come back yet

Click an avatar to rename it or change its face. Names are stored in
`roster.json` and survive restarts. That file is gitignored — it's yours. Copy
`roster.example.json` to start one.

## Where the data comes from

Two locations, read only, never written:

- `~/.claude/sessions/<pid>.json` — one file per live window, maintained by
  Claude Code itself (pid, sessionId, cwd, status)
- `~/.claude/projects/*/<sessionId>.jsonl` — that window's full transcript

Transcripts reach 12MB+, so only the last 400KB is read, cached by mtime. A
sweep over six windows takes about 30ms. Desks disappear when the window
closes — liveness is decided by whether the pid still exists, not by whether
the file is still there.

## What it can't do

It cannot send anything into a window. Watching only.

## Requirements

`/usr/bin/python3` (macOS system Python). Standard library only, no
dependencies, no network calls. Binds to `127.0.0.1`.

## License

MIT
