#!/usr/bin/env /usr/bin/python3
"""
CC Office - a live view of every Claude Code window on this Mac.

Reads only from disk, never writes:
  ~/.claude/sessions/<pid>.json   -> one file per live window (pid, sessionId, cwd, status)
  ~/.claude/projects/*/<sid>.jsonl -> that window's transcript

Pure stdlib. Run:  /usr/bin/python3 server.py
"""

import json
import os
import glob
import time
import re
import signal
import subprocess
import http.server
import socketserver
from urllib.parse import urlparse

PORT = int(os.environ.get("CC_OFFICE_PORT", "8910"))
HOME = os.path.expanduser("~")
SESSIONS_DIR = os.path.join(HOME, ".claude", "sessions")
PROJECTS_DIR = os.path.join(HOME, ".claude", "projects")
HERE = os.path.dirname(os.path.abspath(__file__))

# Read at most this many bytes from the end of a transcript. Transcripts reach
# 12MB+; everything we show lives in the last few exchanges.
TAIL_BYTES = 400_000

# Silence after which a "busy" window is assumed to be stuck on a prompt
# (permission dialog, confirmation) rather than actually working.
STALL_SECONDS = 75

AVATARS = ["🐧", "🦊", "🐢", "🐙", "🦉", "🐻", "🐳", "🦁", "🐸", "🦩", "🐺", "🦭"]

# subagent_type -> display name shown on the intern badge
AGENT_NAMES = {
    "kai": "Kai",
    "max": "Max",
    "explore": "侦察",
    "general-purpose": "打杂",
    "plan": "架构",
    "claude": "帮手",
    "claude-code-guide": "CC 向导",
    "statusline-setup": "配置",
}

_cache = {}  # path -> (mtime, size, parsed)

ROSTER_PATH = os.path.join(HERE, "roster.json")


def load_roster():
    """sessionId -> {"name": ..., "avatar": ...} for desks you renamed."""
    try:
        return json.load(open(ROSTER_PATH))
    except (OSError, ValueError):
        return {}


def save_roster(r):
    tmp = ROSTER_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(r, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ROSTER_PATH)


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def tail_lines(path, nbytes=TAIL_BYTES):
    """Return decoded lines from the last nbytes of a file."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size > nbytes:
            f.seek(size - nbytes)
            f.readline()  # discard the partial first line
        raw = f.read()
    return raw.decode("utf-8", errors="replace").split("\n")


def summarize_tool(name, inp):
    """One short human line describing a tool call in flight."""
    if not isinstance(inp, dict):
        return name
    if name == "Bash":
        return (inp.get("description") or inp.get("command") or "").strip()
    if name in ("Edit", "Write", "NotebookEdit"):
        return os.path.basename(inp.get("file_path", "") or "")
    if name == "Read":
        return os.path.basename(inp.get("file_path", "") or "")
    if name in ("Grep", "Glob"):
        return inp.get("pattern") or inp.get("path") or ""
    if name in ("WebFetch", "WebSearch"):
        return inp.get("url") or inp.get("query") or ""
    if name == "Agent":
        return inp.get("description") or ""
    if name == "Skill":
        return inp.get("skill") or ""
    return inp.get("description") or inp.get("prompt") or ""


def is_injected(raw):
    """True for text the harness fed in as a user turn: skill bodies, slash
    command payloads, tool reminders. None of it is something you typed."""
    t = (raw or "").lstrip()
    if not t:
        return True
    if t.startswith("<"):
        return True
    if "<command-name>" in t or "<local-command" in t:
        return True
    # A skill body arrives as a long markdown document with a leading heading.
    if t.startswith("#") and len(t) > 300:
        return True
    return False


def clean_text(t):
    """Strip system-reminder blocks and collapse whitespace."""
    if not t:
        return ""
    t = re.sub(r"<system-reminder>.*?</system-reminder>", "", t, flags=re.S)
    t = re.sub(r"\[Image:[^\]]*\]", "（图片）", t)
    t = re.sub(r"\[Pasted text[^\]]*\]", "（长文本）", t)
    t = re.sub(r"<[a-z-]+>.*?</[a-z-]+>", "", t, flags=re.S)
    t = re.sub(r"```.*?```", " (代码) ", t, flags=re.S)
    t = re.sub(r"[*#`>]", "", t)
    t = re.sub(r"\|", " ", t)          # markdown table pipes
    t = re.sub(r"-{3,}", " ", t)       # table rules and hr
    return re.sub(r"\s+", " ", t).strip()


def parse_transcript(path, nbytes=TAIL_BYTES):
    """Extract what this window was told to do and what it is doing now."""
    st = os.stat(path)
    key = (st.st_mtime, st.st_size, nbytes)
    hit = _cache.get(path)
    if hit and hit[0] == key:
        return hit[1]

    task = ""          # the last substantial thing you typed
    reply = ""         # your most recent line, however short ("ok", "1")
    last_say = ""      # the last thing the window said to you
    fallback_task = "" # from the transcript's own last-prompt record
    tool_calls = {}    # tool_use_id -> (name, summary, seq)
    done_ids = set()
    seq = 0

    for line in tail_lines(path, nbytes):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") == "last-prompt" and d.get("lastPrompt"):
            fallback_task = clean_text(str(d["lastPrompt"]))
        msg = d.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        typ = d.get("type")
        sidechain = d.get("isSidechain")

        if typ == "user" and not sidechain:
            texts = []
            if isinstance(content, str):
                if not content.lstrip().startswith("<"):
                    texts.append(content)
            elif isinstance(content, list):
                for b in content:
                    if b.get("type") == "text":
                        texts.append(b.get("text", ""))
                    elif b.get("type") == "tool_result":
                        done_ids.add(b.get("tool_use_id"))
            for raw in texts:
                if is_injected(raw):
                    continue
                c = clean_text(raw)
                if not c:
                    continue
                reply = c
                # "sure, go ahead" is the newest thing typed but says nothing
                # about which window this is. The title needs the last line
                # with enough substance to recognize the job by.
                if len(c) >= 12:
                    task = c

        elif typ == "assistant" and isinstance(content, list) and not sidechain:
            for b in content:
                if b.get("type") == "tool_use":
                    seq += 1
                    tool_calls[b.get("id")] = (
                        b.get("name", "?"),
                        summarize_tool(b.get("name", ""), b.get("input")),
                        seq,
                        b.get("input") if isinstance(b.get("input"), dict) else {},
                    )
                elif b.get("type") == "text":
                    c = clean_text(b.get("text", ""))
                    if c:
                        last_say = c

    pending = [
        (tid, v) for tid, v in tool_calls.items() if tid not in done_ids
    ]
    pending.sort(key=lambda kv: kv[1][2])

    # The newest tool call overall, pending or not, is what it last touched.
    newest = max(tool_calls.values(), key=lambda v: v[2]) if tool_calls else None

    interns = []
    for tid, (name, summ, _s, inp) in pending:
        if name == "Agent":
            stype = (inp.get("subagent_type") or "claude").lower()
            interns.append({
                "who": AGENT_NAMES.get(stype, stype),
                "doing": summ or inp.get("description") or "",
            })

    out = {
        "task": (task or fallback_task or reply)[:160],
        "reply": reply[:160],
        "last_say": last_say[:400],
        "pending": [{"tool": v[0], "what": v[1]} for _t, v in pending if v[0] != "Agent"],
        "newest_tool": {"tool": newest[0], "what": newest[1]} if newest else None,
        "interns": interns,
    }
    _cache[path] = (key, out)
    return out


def find_transcript(session_id):
    hits = glob.glob(os.path.join(PROJECTS_DIR, "*", session_id + ".jsonl"))
    return hits[0] if hits else None


def project_label(cwd):
    base = os.path.basename(cwd.rstrip("/")) or "/"
    if base == os.path.basename(HOME):
        return "~"
    return base


def collect():
    now = time.time()
    roster = load_roster()
    people = []
    taken = set()
    for f in sorted(glob.glob(os.path.join(SESSIONS_DIR, "*.json"))):
        try:
            s = json.load(open(f))
        except (ValueError, OSError):
            continue
        pid = s.get("pid")
        if not pid or not pid_alive(pid):
            continue

        sid = s.get("sessionId", "")
        cwd = s.get("cwd", "")
        tpath = find_transcript(sid)
        info = {"task": "", "reply": "", "last_say": "", "pending": [],
                "newest_tool": None, "interns": []}
        silence = 9999.0
        if tpath:
            try:
                silence = now - os.path.getmtime(tpath)
                info = parse_transcript(tpath)
                # A long-running window may have said nothing recent enough to
                # land in the tail. Only then is a deeper read worth it.
                if not info["task"]:
                    info = parse_transcript(tpath, 4_000_000)
            except OSError:
                pass

        raw_status = s.get("status", "")
        if raw_status == "idle":
            state, mood = "waiting", "等你回话"
        elif silence > STALL_SECONDS:
            state, mood = "stalled", "卡住了，可能在等你点确认"
        else:
            state, mood = "working", "干活中"

        # The speech bubble: what it is doing, or what it last said to her.
        if state == "working" and info["pending"]:
            p = info["pending"][-1]
            bubble = p["what"] or p["tool"]
            bubble_kind = "tool"
        elif state == "working" and info["newest_tool"]:
            bubble = info["newest_tool"]["what"] or info["newest_tool"]["tool"]
            bubble_kind = "tool"
        else:
            bubble = info["last_say"]
            bubble_kind = "say"

        custom = roster.get(sid, {})
        avatar = custom.get("avatar")
        if not avatar:
            # Stable per session, but nudged off collisions so two desks never
            # wear the same face in one office.
            start = sum(ord(c) for c in sid) % len(AVATARS) if sid else 0
            for i in range(len(AVATARS)):
                cand = AVATARS[(start + i) % len(AVATARS)]
                if cand not in taken:
                    avatar = cand
                    break
            avatar = avatar or AVATARS[start]
        taken.add(avatar)

        # A bubble is three lines wide at ~21 CJK glyphs per line. Cutting here
        # rather than in CSS keeps the clip on a character, not mid-glyph-row.
        if len(bubble) > 60:
            bubble = bubble[:60].rstrip() + "…"

        people.append({
            "pid": pid,
            "sid": sid,
            "short": sid[:4],
            "name": custom.get("name", ""),
            "avatar": avatar,
            "project": project_label(cwd),
            "cwd": cwd.replace(HOME, "~"),
            "state": state,
            "mood": mood,
            "task": info["task"],
            "reply": info["reply"],
            "bubble": bubble,
            "bubble_kind": bubble_kind,
            "interns": info["interns"],
            "silence": int(silence),
            "uptime": int(now - s.get("startedAt", now * 1000) / 1000),
        })

    order = {"waiting": 0, "stalled": 1, "working": 2}
    people.sort(key=lambda p: (order.get(p["state"], 3), -p["uptime"]))
    return {"now": int(now), "people": people}


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        if urlparse(self.path).path != "/api/name":
            self.send_error(404)
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            sid = body.get("sid")
            if not sid:
                raise ValueError("no sid")
            roster = load_roster()
            entry = roster.get(sid, {})
            if "name" in body:
                entry["name"] = str(body["name"])[:24].strip()
            if "avatar" in body:
                entry["avatar"] = str(body["avatar"])[:8].strip()
            entry = {k: v for k, v in entry.items() if v}
            if entry:
                roster[sid] = entry
            else:
                roster.pop(sid, None)
            save_roster(roster)
        except (ValueError, OSError) as e:
            self.send_error(400, str(e))
            return
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/office":
            body = json.dumps(collect(), ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path in ("/", "/index.html"):
            try:
                body = open(os.path.join(HERE, "index.html"), "rb").read()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    signal.signal(signal.SIGINT, lambda *a: os._exit(0))
    with Server(("127.0.0.1", PORT), Handler) as httpd:
        print("CC Office  ->  http://localhost:%d" % PORT)
        httpd.serve_forever()
