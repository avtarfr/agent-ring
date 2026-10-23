#!/usr/bin/env python3
"""agent-ring: watch a panel of AI agents talk, live, on a page served from this computer.

Each panel member is a small pixel critter seated on a ring; the orchestrator sits in the middle. Members post
what they say to this server; the page shows a pulse travelling from the speaker to whoever it is addressed to,
the words in a speech bubble, and a running conversation log. A board lets members read each other, and a wait
call lets them hold until everyone has posted in a round.

Server (Python 3.9+, standard library only, binds 127.0.0.1 only):

    python3 agent_ring.py serve [--port 8790] [--title "Topic"] [--out DIR] [--no-open] [--ready-file FILE]

Client helpers (same script, no curl needed; text comes from --text or stdin):

    python3 agent_ring.py post --from NAME [--to all|NAME|orchestrator] [--kind idea] [--round N] [--text TEXT]
    python3 agent_ring.py board [--round N] [--since ID] [--json]
    python3 agent_ring.py wait --round N --n COUNT [--kinds idea,reply,done] [--timeout 540]
    python3 agent_ring.py health [--ready-file FILE] [--wait SECONDS]
    python3 agent_ring.py reset [--title "New topic"]
    python3 agent_ring.py save | export
    python3 agent_ring.py stop

All client commands take --port (default 8790).

HTTP endpoints:

    GET  /            the page
    GET  /health      {"app": "agent-ring", "pid": ..., "port": ..., "last_round": ..., ...}
    POST /event       {"from", "to", "kind", "round", "text"}   (Content-Type: application/json)
    GET  /board       ?since=ID&round=N        events as a JSON list
    GET  /wait        ?round=N&n=COUNT&kinds=idea,reply,done&timeout=S
                      returns once COUNT different members (not the orchestrator) posted one of kinds in round N
    GET  /stream      server-sent events for the page
    GET  /export      the conversation as Markdown
    POST /save        writes the transcript to --out and returns its path
    POST /reset       saves the transcript, then starts a fresh panel; optional {"title": "..."}

Nothing leaves the machine. On exit (Ctrl+C or SIGTERM) the transcript is saved to --out if it changed.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import queue
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "0.1.0"
APP = "agent-ring"
HOST = "127.0.0.1"
DEFAULT_PORT = 8790
PORT_TRIES = 20
ORCH = "orchestrator"
KINDS = ("join", "think", "idea", "reply", "done", "say")
SPOKEN = ("idea", "reply", "done")
MAX_BODY = 16 * 1024  # bytes per POST
MAX_TEXT = 4000  # characters per message
MAX_NAME = 40
MAX_ROUND = 10_000
MAX_EVENTS = 2000  # per panel
MAX_AGENTS = 32  # distinct speakers per panel
MAX_LISTENERS = 16  # open pages
MAX_WAIT = 1800  # seconds
MAX_TITLE = 120


class EventError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def clean_name(value, default: str = "") -> str:
    s = "" if value is None else str(value)
    s = "".join(ch for ch in s if ch.isprintable())
    s = " ".join(s.split())[:MAX_NAME]
    return s or default


def clean_text(value) -> str:
    s = "" if value is None else str(value)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(ch for ch in s if ch in "\n\t" or ch.isprintable()).strip()


def clean_title(value, default: str) -> str:
    s = " ".join(clean_text(value).split())[:MAX_TITLE]
    return s or default


class Ring:
    """All panel state. Thread-safe; every method takes the condition lock."""

    def __init__(self, title: str = "Agent ring", out_dir: str = ".", max_events: int = MAX_EVENTS,
                 max_agents: int = MAX_AGENTS):
        self.cond = threading.Condition()
        self.title = clean_title(title, "Agent ring")
        self.out_dir = Path(out_dir or ".")
        self.max_events = max_events
        self.max_agents = max_agents
        self.panel = 1
        self.events: list = []
        self.listeners: list = []
        self.started = time.time()
        self.dirty = False
        self.port = 0

    # events

    def add(self, raw) -> dict:
        if not isinstance(raw, dict):
            raise EventError(400, "send a JSON object")
        frm = clean_name(raw.get("from"))
        if not frm:
            raise EventError(400, "'from' is required")
        if frm.lower() == "all":
            raise EventError(400, "'all' is not a member name")
        to = clean_name(raw.get("to"), "all")
        kind = str(raw.get("kind") or "say").strip().lower()
        if kind not in KINDS:
            raise EventError(400, "kind must be one of " + ", ".join(KINDS))
        try:
            rnd = int(raw.get("round") or 0)
        except (TypeError, ValueError):
            raise EventError(400, "'round' must be a whole number") from None
        if not 0 <= rnd <= MAX_ROUND:
            raise EventError(400, f"'round' must be between 0 and {MAX_ROUND}")
        text = raw.get("text")
        text = "" if text is None else str(text)
        if len(text) > MAX_TEXT:
            raise EventError(413, f"text is longer than {MAX_TEXT} characters")
        text = clean_text(text)
        with self.cond:
            if len(self.events) >= self.max_events:
                raise EventError(429, "this panel is full; POST /reset for a fresh one")
            names = self._names()
            if frm not in names and len(names) >= self.max_agents:
                raise EventError(429, f"at most {self.max_agents} members per panel")
            ev = {
                "id": len(self.events) + 1,
                "panel": self.panel,
                "ts": round(time.time(), 3),
                "from": frm,
                "to": to,
                "kind": kind,
                "round": rnd,
                "text": text,
            }
            self.events.append(ev)
            self.dirty = True
            self._broadcast(ev)
            self.cond.notify_all()
        return ev

    def _names(self) -> list:
        seen: dict = {}
        for e in self.events:
            seen.setdefault(e["from"], None)
        return list(seen)

    def _broadcast(self, ev: dict) -> None:
        for q in list(self.listeners):
            q.put(ev)

    def board(self, since: int = 0, rnd=None) -> list:
        with self.cond:
            return [dict(e) for e in self.events if e["id"] > since and (rnd is None or e["round"] == rnd)]

    def speakers(self, rnd: int, kinds=SPOKEN) -> list:
        return sorted({e["from"] for e in self.events
                       if e["round"] == rnd and e["kind"] in kinds and e["from"] != ORCH})

    def wait(self, rnd: int, n: int, timeout: float, kinds=SPOKEN) -> dict:
        end = time.monotonic() + timeout
        with self.cond:
            panel = self.panel
            while (len(self.speakers(rnd, kinds)) < n and self.panel == panel
                   and time.monotonic() < end):
                self.cond.wait(timeout=max(0.05, min(5.0, end - time.monotonic())))
            got = self.speakers(rnd, kinds)
            return {"round": rnd, "n": n, "ready": len(got) >= n, "agents": got,
                    "reset": self.panel != panel}

    def health(self) -> dict:
        with self.cond:
            members = [n for n in self._names() if n != ORCH]
            return {
                "app": APP,
                "version": VERSION,
                "ok": True,
                "title": self.title,
                "panel": self.panel,
                "pid": os.getpid(),
                "port": self.port,
                "url": f"http://{HOST}:{self.port}/",
                "events": len(self.events),
                "members": members,
                "last_round": max((e["round"] for e in self.events), default=0),
                "uptime": round(time.time() - self.started, 1),
            }

    # transcript

    def markdown(self) -> str:
        with self.cond:
            return self._markdown()

    def _markdown(self) -> str:
        members = [n for n in self._names() if n != ORCH]
        out = [f"# {self.title}", "",
               f"Agent ring panel {self.panel}, {dt.datetime.now():%Y-%m-%d %H:%M}. "
               f"{len(members)} member{'' if len(members) == 1 else 's'}: {', '.join(members) or 'none'}.", ""]
        seats = [e for e in self.events if e["kind"] == "join" and e["text"]]
        if seats:
            out += ["## Panel", ""] + [f"- **{e['from']}**: {e['text']}" for e in seats] + [""]
        current = None
        for e in self.events:
            if e["kind"] in ("join", "think"):
                continue
            if e["round"] != current:
                current = e["round"]
                out += [f"## Round {current}" if current else "## Kickoff", ""]
            t = dt.datetime.fromtimestamp(e["ts"]).strftime("%H:%M:%S")
            out += [f"**{e['from']} -> {e['to']}** ({e['kind']}, {t})", "", e["text"] or "(no text)", ""]
        return "\n".join(out).rstrip() + "\n"

    def save(self) -> str | None:
        with self.cond:
            return self._save_locked()

    def _save_locked(self) -> str | None:
        if not self.events:
            return None
        self.out_dir.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.fromtimestamp(self.started).strftime("%Y-%m-%d-%H%M%S")
        path = self.out_dir / f"agent-ring-{stamp}-panel{self.panel}.md"
        path.write_text(self._markdown(), encoding="utf-8")
        self.dirty = False
        return str(path.resolve())

    def reset(self, title=None) -> dict:
        with self.cond:
            saved = self._save_locked()
            self.events = []
            self.dirty = False
            self.panel += 1
            self.started = time.time()
            if title:
                self.title = clean_title(title, self.title)
            self._broadcast({"kind": "reset", "panel": self.panel, "title": self.title})
            self.cond.notify_all()
            return {"panel": self.panel, "title": self.title, "saved": saved}

    # page listeners

    def listen(self):
        with self.cond:
            if len(self.listeners) >= MAX_LISTENERS:
                return None, []
            q: queue.Queue = queue.Queue()
            self.listeners.append(q)
            hello = {"kind": "hello", "panel": self.panel, "title": self.title}
            return q, [hello] + [dict(e, replay=True) for e in self.events]

    def unlisten(self, q) -> None:
        with self.cond:
            if q in self.listeners:
                self.listeners.remove(q)


class RingServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"  # on Windows SO_REUSEADDR would let two servers share a port
    request_queue_size = 64

    def __init__(self, addr, ring: Ring):
        super().__init__(addr, Handler)
        self.ring = ring
        ring.port = self.server_address[1]


class Handler(BaseHTTPRequestHandler):
    server_version = f"{APP}/{VERSION}"
    sys_version = ""

    def log_message(self, *args):  # quiet
        pass

    @property
    def ring(self) -> Ring:
        return self.server.ring  # type: ignore[attr-defined]

    def _allowed_hosts(self) -> set:
        p = self.server.server_address[1]
        return {f"127.0.0.1:{p}", f"localhost:{p}"}

    def _host_ok(self) -> bool:
        return (self.headers.get("Host") or "").strip().lower() in self._allowed_hosts()

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        return not origin or origin.strip().lower() in {"http://" + h for h in self._allowed_hosts()}

    def _send(self, code: int, body: bytes, ctype: str = "application/json", extra=None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, code: int, message: str) -> None:
        self._json(code, {"error": message})

    def do_GET(self):
        if not self._host_ok():
            return self._error(403, "use http://127.0.0.1:<port>/")
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        try:
            if u.path == "/":
                return self._send(200, PAGE_BYTES, "text/html; charset=utf-8", {"Content-Security-Policy": CSP})
            if u.path == "/health":
                return self._json(200, self.ring.health())
            if u.path == "/board":
                since = int(q.get("since") or 0)
                rnd = int(q["round"]) if q.get("round") not in (None, "") else None
                return self._json(200, self.ring.board(since, rnd))
            if u.path == "/wait":
                rnd = int(q.get("round") or 1)
                n = max(1, min(MAX_AGENTS, int(q.get("n") or 1)))
                timeout = max(0.0, min(float(MAX_WAIT), float(q.get("timeout") or 600)))
                kinds = tuple(k for k in (q.get("kinds") or ",".join(SPOKEN)).split(",") if k in KINDS) or SPOKEN
                return self._json(200, self.ring.wait(rnd, n, timeout, kinds))
            if u.path == "/export":
                return self._send(200, self.ring.markdown().encode("utf-8"), "text/markdown; charset=utf-8")
            if u.path == "/stream":
                return self._stream()
            if u.path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
        except (ValueError, KeyError):
            return self._error(400, "bad query parameter")
        return self._error(404, "not found")

    def do_POST(self):
        if not self._host_ok() or not self._origin_ok():
            return self._error(403, "only this computer's own page and tools may post")
        path = urllib.parse.urlsplit(self.path).path
        if path not in ("/event", "/reset", "/save"):
            return self._error(404, "not found")
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return self._error(415, "send Content-Type: application/json")
        try:
            length = int(self.headers.get("Content-Length") or "x")
        except ValueError:
            return self._error(411, "Content-Length is required")
        if length < 0:
            return self._error(400, "bad Content-Length")
        if length > MAX_BODY:
            self.close_connection = True
            if length <= 16 * MAX_BODY:  # drain so the client sees the answer rather than a reset
                self.rfile.read(length)
            return self._error(413, f"event is larger than {MAX_BODY} bytes")
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except (ValueError, UnicodeDecodeError):
            return self._error(400, "body is not valid JSON")
        try:
            if path == "/event":
                return self._json(200, self.ring.add(body))
            if path == "/reset":
                title = body.get("title") if isinstance(body, dict) else None
                return self._json(200, self.ring.reset(title))
            return self._json(200, {"saved": self.ring.save()})
        except EventError as e:
            return self._error(e.code, e.message)
        except OSError as e:
            return self._error(500, f"could not write the transcript: {e.strerror or e}")

    def _stream(self) -> None:
        q, backlog = self.ring.listen()
        if q is None:
            return self._error(503, "too many open pages")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(b"retry: 2000\n\n")
            for e in backlog:
                self.wfile.write(f"data: {json.dumps(e)}\n\n".encode("utf-8"))
            self.wfile.flush()
            while True:
                try:
                    e = q.get(timeout=15)
                    self.wfile.write(f"data: {json.dumps(e)}\n\n".encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except OSError:
            pass
        finally:
            self.ring.unlisten(q)
            self.close_connection = True


STYLE = r"""
:root { color-scheme: light dark;
  --bg:#f4efe6; --panel:rgba(244,239,230,.9); --fg:#2b2620; --dim:#7a7064; --line:#d8cfc0;
  --clay:#c96442; --eye:#1d1915; --idea:#b85a3c; --reply:#4f7a3a; --done:#7d5a99; --say:#2f6f8f; }
@media (prefers-color-scheme: dark) { :root {
  --bg:#14120f; --panel:rgba(20,18,15,.86); --fg:#e9e1d4; --dim:#8c8476; --line:#3a332b;
  --clay:#d97757; --eye:#1d1915; --idea:#e08a68; --reply:#9fc28a; --done:#c9a6e0; --say:#8cc4de; } }
html, body { margin:0; height:100%; background:var(--bg); color:var(--fg);
  font:13px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace; overflow:hidden; }
canvas { position:fixed; inset:0; display:block; }
header { position:fixed; left:16px; top:12px; right:16px; pointer-events:none; }
header h1 { margin:0; font-size:15px; font-weight:600; max-width:60vw; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
header p { margin:2px 0 0; color:var(--dim); font-size:11px; }
#log { position:fixed; right:12px; top:12px; bottom:12px; width:min(380px, 34vw); overflow:auto; box-sizing:border-box;
  background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:10px; }
#log h2 { font-size:11px; letter-spacing:.12em; color:var(--dim); margin:0 0 8px; text-transform:uppercase; font-weight:600; }
.m { margin:0 0 10px; } .m .w { color:var(--dim); font-size:11px; } .m .t { white-space:pre-wrap; overflow-wrap:anywhere; }
.m .w b { color:var(--idea); } .m.reply .w b { color:var(--reply); } .m.done .w b { color:var(--done); } .m.say .w b { color:var(--say); }
#hint { position:fixed; left:16px; bottom:10px; color:var(--dim); font-size:11px; }
@media (max-width: 759px) {
  header h1 { max-width:none; }
  #log { left:12px; right:12px; top:auto; width:auto; height:38vh; }
  #hint { display:none; }
}
"""

SCRIPT = r"""
"use strict";
const cv = document.getElementById("c"), ctx = cv.getContext("2d");
const titleEl = document.getElementById("title"), metaEl = document.getElementById("meta");
const msgs = document.getElementById("msgs"), logEl = document.getElementById("log");
const ORCH = "orchestrator", MAX_LOG = 400;
let agents = new Map(), seats = [], pulses = [], lastRound = 0, W = 0, H = 0, live = false;
const pal = {};
function readPalette() {
  const s = getComputedStyle(document.documentElement);
  for (const k of ["bg", "fg", "dim", "line", "clay", "eye"]) pal[k] = s.getPropertyValue("--" + k).trim();
  pal.dark = matchMedia("(prefers-color-scheme: dark)").matches;
}
readPalette();
const mq = matchMedia("(prefers-color-scheme: dark)");
if (mq.addEventListener) mq.addEventListener("change", readPalette);
function fit() {
  const d = window.devicePixelRatio || 1; W = innerWidth; H = innerHeight;
  cv.width = Math.round(W * d); cv.height = Math.round(H * d); cv.style.width = W + "px"; cv.style.height = H + "px";
  ctx.setTransform(d, 0, 0, d, 0, 0);
}
addEventListener("resize", fit); fit();
function hueOf(s) { let h = 7; for (const c of s) h = (h * 31 + c.codePointAt(0)) % 360; return h; }
function agent(name) {
  if (!agents.has(name)) {
    const h = hueOf(name);
    agents.set(name, { name, said: "", saidAt: -100, mood: "idle", hue: (h % 40) - 2, shade: h % 12, n: 0, x: 0, y: 0 });
    if (name !== ORCH) seats.push(name);
  }
  return agents.get(name);
}
function clearAll(title) {
  agents = new Map(); seats = []; pulses = []; lastRound = 0;
  while (msgs.firstChild) msgs.removeChild(msgs.firstChild);
  if (title) { titleEl.textContent = title; document.title = title + " - agent ring"; }
  agent(ORCH); meta();
}
function meta() {
  metaEl.textContent = seats.length + (seats.length === 1 ? " member" : " members") +
    (lastRound ? " · round " + lastRound : "") + (live ? "" : " · connecting…");
}
// the critter: a small pixel creature (body, two eyes, legs) that bobs, blinks, and hops when it speaks
const SPRITE = ["..XXXXXX..", ".XXXXXXXX.", "XXEXXXXEXX", "XXXXXXXXXX", "XXXXXXXXXX", ".XXXXXXXX.", ".X.X..X.X."];
function critter(x, y, px, color, blink, hop) {
  y -= hop;
  for (let r = 0; r < SPRITE.length; r++) for (let c = 0; c < SPRITE[r].length; c++) {
    const ch = SPRITE[r][c]; if (ch === ".") continue;
    ctx.fillStyle = ch === "E" ? (blink ? color : pal.eye) : color;
    ctx.fillRect(x + (c - 5) * px, y + (r - 3.5) * px, px + 0.5, px + 0.5);
  }
}
function area() {
  const r = logEl.getBoundingClientRect(), wide = W >= 760;
  const x1 = wide ? r.left - 8 : W, y0 = 52, y1 = wide ? H - 24 : r.top - 8;
  return { x0: 0, x1, y0, y1, cx: x1 / 2, cy: (y0 + y1) / 2 };
}
function layout() {
  const A = area(), n = seats.length;
  const R = Math.max(50, Math.min(A.x1 - A.x0, A.y1 - A.y0) / 2 - 60);
  seats.forEach((name, i) => {
    const a = agents.get(name), t = -Math.PI / 2 + (i / Math.max(1, n)) * Math.PI * 2;
    a.x = A.cx + Math.cos(t) * R; a.y = A.cy + Math.sin(t) * R;
  });
  const o = agent(ORCH); o.x = A.cx; o.y = A.cy;
  return { A, R, n };
}
function colorOf(a) {
  if (a.name === ORCH) return pal.clay;
  return "hsl(" + a.hue + " 58% " + ((pal.dark ? 52 : 44) + a.shade) + "%)";
}
function wrap(text, width, maxLines) {
  const words = text.replace(/\s+/g, " ").trim().split(" "), lines = [];
  let cur = "";
  for (let w of words) {
    if (w.length > width) w = w.slice(0, width - 1) + "…";
    if (cur && (cur + " " + w).length > width) { lines.push(cur); cur = w; } else cur = cur ? cur + " " + w : w;
    if (lines.length >= maxLines) break;
  }
  if (cur && lines.length < maxLines) lines.push(cur);
  if (lines.length === maxLines && words.join(" ").length > lines.join(" ").length) lines[maxLines - 1] = lines[maxLines - 1].replace(/.?$/, "…");
  return lines;
}
function bubble(a, now, A) {
  const age = now - a.saidAt; if (!a.said || age > 12) return;
  ctx.globalAlpha = Math.max(0, Math.min(1, 1.5 - age / 8));
  const L = wrap(a.said, 26, 4), lh = 13;
  const bw = Math.max(...L.map((l) => ctx.measureText(l).width)) + 14, bh = L.length * lh + 10;
  let bx = a.x - bw / 2, by = a.y - 44 - bh;
  if (by < A.y0) by = a.y + 40;
  bx = Math.max(A.x0 + 4, Math.min(A.x1 - bw - 4, bx));
  ctx.fillStyle = pal.bg; ctx.strokeStyle = colorOf(a); ctx.lineWidth = 1;
  ctx.beginPath();
  if (ctx.roundRect) ctx.roundRect(bx, by, bw, bh, 7); else ctx.rect(bx, by, bw, bh);
  ctx.fill(); ctx.stroke();
  ctx.fillStyle = pal.fg;
  L.forEach((l, i) => ctx.fillText(l, bx + 7, by + 16 + i * lh));
  ctx.globalAlpha = 1;
}
function draw(t) {
  const now = t / 1000;
  ctx.clearRect(0, 0, W, H);
  const { A, R, n } = layout();
  ctx.font = "11px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"; ctx.textAlign = "left";
  ctx.strokeStyle = pal.line; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.arc(A.cx, A.cy, R, 0, Math.PI * 2); ctx.stroke();
  for (const name of seats) { const a = agents.get(name); ctx.beginPath(); ctx.moveTo(A.cx, A.cy); ctx.lineTo(a.x, a.y); ctx.stroke(); }
  for (let i = pulses.length - 1; i >= 0; i--) {
    const p = pulses[i], k = (now - p.t0) / p.dur;
    if (k > 1) { pulses.splice(i, 1); continue; }
    if (k < 0) continue;
    const a = agents.get(p.from), b = agents.get(p.to); if (!a || !b) continue;
    const e = 1 - (1 - k) ** 2;
    const mx = (a.x + b.x) / 2 + (A.cy - (a.y + b.y) / 2) * 0.25, my = (a.y + b.y) / 2 + ((a.x + b.x) / 2 - A.cx) * 0.25;
    const x = (1 - e) ** 2 * a.x + 2 * (1 - e) * e * mx + e * e * b.x, y = (1 - e) ** 2 * a.y + 2 * (1 - e) * e * my + e * e * b.y;
    ctx.strokeStyle = "hsla(" + p.hue + ",55%,58%," + (0.4 * (1 - k)) + ")"; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.quadraticCurveTo(mx, my, b.x, b.y); ctx.stroke();
    ctx.fillStyle = "hsl(" + p.hue + " 65% 58%)"; ctx.beginPath(); ctx.arc(x, y, 4, 0, Math.PI * 2); ctx.fill();
  }
  const small = n <= 8 ? 3.6 : Math.max(2.4, 3.6 * Math.sqrt(8 / n));
  for (const a of agents.values()) {
    const big = a.name === ORCH, px = big ? 5 : small, since = now - a.saidAt;
    const hop = since < 0.6 ? Math.sin(since / 0.6 * Math.PI) * 10 : Math.sin(now * 2 + a.hue) * 1.5;
    const blink = Math.sin(now * 1.3 + a.hue) > 0.985;
    if (a.mood === "think") { ctx.fillStyle = pal.dim; ctx.fillText(".".repeat(1 + Math.floor(now * 2) % 3), a.x + px * 6, a.y - px * 4); }
    critter(a.x, a.y, px, colorOf(a), blink, hop);
    ctx.fillStyle = pal.fg; ctx.textAlign = "center";
    ctx.fillText(a.name + (a.n ? "  " + a.n : ""), a.x, a.y + px * 4 + 14); ctx.textAlign = "left";
  }
  [...agents.values()].sort((p, q) => p.saidAt - q.saidAt).forEach((a) => bubble(a, now, A));
  requestAnimationFrame(draw);
}
function logEntry(e) {
  const d = document.createElement("div"); d.className = "m " + e.kind;
  const w = document.createElement("div"); w.className = "w";
  const b = document.createElement("b"); b.textContent = e.from;
  const i = document.createElement("i"); i.textContent = " · " + e.kind + (e.round ? " · round " + e.round : "");
  w.append(b, document.createTextNode(" → " + e.to), i);
  const t = document.createElement("div"); t.className = "t"; t.textContent = e.text;
  d.append(w, t); msgs.prepend(d);
  while (msgs.childNodes.length > MAX_LOG) msgs.removeChild(msgs.lastChild);
}
function show(e) {
  if (e.kind === "hello" || e.kind === "reset") { clearAll(e.title); return; }
  if (typeof e.from !== "string" || typeof e.text !== "string") return;
  const a = agent(e.from), now = performance.now() / 1000;
  if (e.round > lastRound) lastRound = e.round;
  if (e.kind === "think") { a.mood = "think"; meta(); return; }
  a.mood = "idle";
  if (e.kind === "join") { meta(); return; }
  a.said = e.text; a.saidAt = e.replay ? -100 : now; a.n++;
  if (!e.replay) {
    const targets = e.to === "all" ? [...agents.keys()].filter((x) => x !== e.from) : (agents.has(e.to) ? [e.to] : []);
    for (const to of targets) if (to !== e.from) pulses.push({ from: e.from, to, t0: now + Math.random() * 0.15, dur: 1.3, hue: a.hue });
  }
  logEntry(e); meta();
}
clearAll("");
requestAnimationFrame(draw);
const es = new EventSource("/stream");
es.onopen = () => { live = true; meta(); };
es.onerror = () => { live = false; meta(); };
es.onmessage = (m) => { try { show(JSON.parse(m.data)); } catch (err) { /* ignore a bad frame */ } };
"""


def _sha(s: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(s.encode("utf-8")).digest()).decode() + "'"


PAGE = ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>agent ring</title><style>" + STYLE + "</style></head><body>"
        "<canvas id=\"c\" aria-label=\"panel members seated on a ring\"></canvas>"
        "<header><h1 id=\"title\">agent ring</h1><p id=\"meta\"></p></header>"
        "<aside id=\"log\" aria-live=\"polite\"><h2>conversation</h2><div id=\"msgs\"></div></aside>"
        "<div id=\"hint\">agent ring · 127.0.0.1 only · /export for the whole conversation</div>"
        "<script>" + SCRIPT + "</script></body></html>\n")
PAGE_BYTES = PAGE.encode("utf-8")
CSP = ("default-src 'none'; script-src " + _sha(SCRIPT) + "; style-src " + _sha(STYLE) + "; "
       "connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


# server

def bind(port: int, exact: bool = False) -> RingServer:
    """Bind 127.0.0.1:port, or the next free port above it (then any free port) unless exact."""
    ports = [port] if exact or port == 0 else [port + i for i in range(PORT_TRIES)] + [0]
    last: OSError | None = None
    for p in ports:
        if not 0 <= p <= 65535:
            continue
        try:
            return RingServer((HOST, p), Ring())
        except OSError as e:
            last = e
    raise last or OSError("no free port")


def serve(a) -> int:
    try:
        srv = bind(a.port, a.exact_port)
    except OSError as e:
        print(f"agent-ring: could not listen on {HOST}:{a.port}: {e}", file=sys.stderr)
        return 1
    ring = srv.ring
    ring.title = clean_title(a.title, "Agent ring")
    ring.out_dir = Path(a.out or ".").expanduser()
    port = srv.server_address[1]
    url = f"http://{HOST}:{port}/"
    if a.ready_file:
        try:
            rf = Path(a.ready_file).expanduser()
            rf.parent.mkdir(parents=True, exist_ok=True)
            rf.write_text(json.dumps({"app": APP, "url": url, "port": port, "pid": os.getpid()}), encoding="utf-8")
        except OSError as e:
            print(f"agent-ring: could not write the ready file: {e}", file=sys.stderr)
    print(f"agent-ring ready url={url} port={port} pid={os.getpid()} out={ring.out_dir.resolve()}", flush=True)

    def stop(_signum, _frame):
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), stop)
            except (ValueError, OSError):
                pass
    if not a.no_open:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        if ring.dirty:
            try:
                path = ring.save()
                if path:
                    print(f"agent-ring saved {path}", flush=True)
            except OSError as e:
                print(f"agent-ring: could not save the transcript: {e}", file=sys.stderr)
    return 0


# client

class ClientError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def call(port: int, method: str, path: str, body=None, timeout: float = 15):
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"} if method == "POST" else {}
    req = urllib.request.Request(f"http://{HOST}:{port}{path}", data=data if method == "POST" else None,
                                 headers=headers, method=method)
    if method == "POST" and data is None:
        req.data = b""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if r.headers.get_content_type() == "application/json" else raw
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("error", detail)
        except (ValueError, AttributeError):
            pass
        raise ClientError(1, f"agent-ring: HTTP {e.code}: {detail}") from None
    except (urllib.error.URLError, OSError) as e:
        raise ClientError(2, f"agent-ring: no ring at {HOST}:{port} ({getattr(e, 'reason', e)})") from None


def out(obj) -> None:
    print(obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, indent=1), flush=True)


def read_text(a) -> str:
    if a.text is not None:
        return a.text
    if a.text_file:
        return Path(a.text_file).read_text(encoding="utf-8")
    if sys.stdin is None or sys.stdin.isatty():
        return ""
    return sys.stdin.buffer.read().decode("utf-8", "replace")


def port_from_ready(a) -> int:
    if not getattr(a, "ready_file", None):
        return a.port
    path, end = Path(a.ready_file).expanduser(), time.monotonic() + max(0.0, getattr(a, "wait", 0) or 0)
    while True:
        try:
            return int(json.loads(path.read_text(encoding="utf-8"))["port"])
        except (OSError, ValueError, KeyError):
            if time.monotonic() >= end:
                raise ClientError(2, f"agent-ring: no ready file at {path}") from None
            time.sleep(0.2)


def client(a) -> int:
    try:
        if a.cmd == "health":
            port = port_from_ready(a)
            end = time.monotonic() + max(0.0, a.wait or 0)
            while True:
                try:
                    h = call(port, "GET", "/health", timeout=3)
                    if not isinstance(h, dict) or h.get("app") != APP:
                        raise ClientError(2, f"agent-ring: something else is listening on port {port}")
                    out(h)
                    return 0
                except ClientError:
                    if time.monotonic() >= end:
                        raise
                    time.sleep(0.25)
        if a.cmd == "post":
            text = read_text(a)
            ev = call(a.port, "POST", "/event", {"from": a.from_, "to": a.to, "kind": a.kind,
                                                 "round": a.round, "text": text.strip()})
            out(f"posted #{ev['id']} {ev['from']} -> {ev['to']} ({ev['kind']}, round {ev['round']})")
            return 0
        if a.cmd == "board":
            q = {"since": a.since}
            if a.round is not None:
                q["round"] = a.round
            rows = call(a.port, "GET", "/board?" + urllib.parse.urlencode(q))
            if a.json:
                out(rows)
            else:
                for e in rows:
                    print(f"#{e['id']} round {e['round']} {e['from']} -> {e['to']} ({e['kind']}): {e['text']}")
                if not rows:
                    print("(nothing yet)")
            return 0
        if a.cmd == "wait":
            q = urllib.parse.urlencode({"round": a.round, "n": a.n, "kinds": a.kinds, "timeout": a.timeout})
            r = call(a.port, "GET", "/wait?" + q, timeout=a.timeout + 30)
            out(r)
            return 0 if r.get("ready") else 3
        if a.cmd == "reset":
            out(call(a.port, "POST", "/reset", {"title": a.title} if a.title else {}))
            return 0
        if a.cmd == "save":
            out(call(a.port, "POST", "/save", {}))
            return 0
        if a.cmd == "export":
            out(call(a.port, "GET", "/export"))
            return 0
        if a.cmd == "stop":
            h = call(a.port, "GET", "/health", timeout=3)
            if not isinstance(h, dict) or h.get("app") != APP:
                raise ClientError(2, f"agent-ring: port {a.port} is not an agent ring; not stopping anything")
            saved = call(a.port, "POST", "/save", {}).get("saved")
            pid = int(h["pid"])
            os.kill(pid, signal.SIGTERM)  # exactly this server's PID, as reported by the server itself
            out({"stopped": pid, "saved": saved})
            return 0
    except ClientError as e:
        print(str(e), file=sys.stderr)
        return e.code
    return 1


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="agent_ring.py", description="Local live view of a panel of AI agents.")
    ap.add_argument("--version", action="version", version=f"{APP} {VERSION}")
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="run the server (the default command)")
    s.add_argument("--port", type=int, default=DEFAULT_PORT, help="first port to try (default 8790; 0 = any)")
    s.add_argument("--exact-port", action="store_true", help="fail instead of trying the next free port")
    s.add_argument("--title", default="Agent ring", help="panel title shown on the page and in the transcript")
    s.add_argument("--out", default=".", help="folder for transcripts (default: current folder)")
    s.add_argument("--no-open", action="store_true", help="don't open a browser")
    s.add_argument("--ready-file", default="", help="write {url, port, pid} as JSON here once listening")

    def client_parser(name: str, help_: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_)
        p.add_argument("--port", type=int, default=DEFAULT_PORT)
        return p

    p = client_parser("post", "post one message")
    p.add_argument("--from", dest="from_", required=True)
    p.add_argument("--to", default="all")
    p.add_argument("--kind", default="say", choices=KINDS)
    p.add_argument("--round", type=int, default=0)
    p.add_argument("--text", default=None, help="message text (otherwise read from --text-file or stdin)")
    p.add_argument("--text-file", default=None)

    p = client_parser("board", "print the messages so far")
    p.add_argument("--round", type=int, default=None)
    p.add_argument("--since", type=int, default=0)
    p.add_argument("--json", action="store_true")

    p = client_parser("wait", "block until n members posted in a round (exit 0 ready, 3 timed out)")
    p.add_argument("--round", type=int, required=True)
    p.add_argument("--n", type=int, required=True)
    p.add_argument("--kinds", default=",".join(SPOKEN))
    p.add_argument("--timeout", type=float, default=540)

    p = client_parser("health", "check that a ring is running (exit 0) and print its state")
    p.add_argument("--ready-file", default="", help="read the port from the server's ready file")
    p.add_argument("--wait", type=float, default=0, help="keep trying for this many seconds")

    p = client_parser("reset", "save the transcript and start a fresh panel")
    p.add_argument("--title", default="")
    client_parser("save", "write the transcript to the server's --out folder")
    client_parser("export", "print the transcript as Markdown")
    client_parser("stop", "save the transcript and stop the server by its own PID")
    return ap


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].startswith("-") and argv[0] not in ("-h", "--help", "--version"):
        argv.insert(0, "serve")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    a = parser().parse_args(argv)
    if a.cmd == "serve":
        return serve(a)
    return client(a)


if __name__ == "__main__":
    raise SystemExit(main())
