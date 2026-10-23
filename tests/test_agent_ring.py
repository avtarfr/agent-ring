"""Tests for agent_ring.py. Standard library only: python3 -m unittest discover -s tests -v"""

from __future__ import annotations

import http.client
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "agent-ring" / "scripts" / "agent_ring.py"
spec = importlib.util.spec_from_file_location("agent_ring", SCRIPT)
ar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ar)


class RingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.srv = ar.bind(0, exact=True)
        self.srv.ring.out_dir = Path(self.tmp.name)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Content-Type": "application/json"} if method == "POST" else {}
        h.update(headers or {})
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        if method == "POST" and data is None:
            data = b""
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        payload = r.read()
        conn.close()
        if r.getheader("Content-Type", "").startswith("application/json"):
            return r.status, json.loads(payload)
        return r.status, payload.decode("utf-8")

    def post(self, **ev):
        return self.request("POST", "/event", ev)

    def test_health(self):
        code, h = self.request("GET", "/health")
        self.assertEqual(code, 200)
        self.assertEqual(h["app"], "agent-ring")
        self.assertEqual(h["port"], self.port)
        self.assertEqual(h["pid"], os.getpid())

    def test_post_and_board(self):
        self.assertEqual(self.post(**{"from": "skeptic", "kind": "join", "text": "doubts"})[0], 200)
        code, ev = self.post(**{"from": "skeptic", "to": "builder", "kind": "reply", "round": 2, "text": "no"})
        self.assertEqual(code, 200)
        self.assertEqual(ev["id"], 2)
        code, rows = self.request("GET", "/board")
        self.assertEqual([e["kind"] for e in rows], ["join", "reply"])
        code, rows = self.request("GET", "/board?round=2")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["to"], "builder")
        code, rows = self.request("GET", "/board?since=1")
        self.assertEqual([e["id"] for e in rows], [2])
        code, h = self.request("GET", "/health")
        self.assertEqual(h["members"], ["skeptic"])
        self.assertEqual(h["last_round"], 2)

    def test_bad_events(self):
        self.assertEqual(self.post(**{"to": "all", "text": "no sender"})[0], 400)
        self.assertEqual(self.post(**{"from": "all", "text": "x"})[0], 400)
        self.assertEqual(self.post(**{"from": "a", "kind": "shout"})[0], 400)
        self.assertEqual(self.post(**{"from": "a", "round": "one"})[0], 400)
        self.assertEqual(self.request("POST", "/event", raw=b"{not json")[0], 400)
        self.assertEqual(self.request("POST", "/event", raw=b"[1, 2]")[0], 400)

    def test_rejects_oversized(self):
        code, r = self.post(**{"from": "a", "kind": "idea", "text": "x" * (ar.MAX_TEXT + 1)})
        self.assertEqual(code, 413)
        code, r = self.request("POST", "/event", raw=json.dumps({"from": "a", "pad": "y" * ar.MAX_BODY}).encode())
        self.assertEqual(code, 413)
        self.assertEqual(self.request("GET", "/health")[1]["events"], 0)

    def test_caps_event_count(self):
        self.srv.ring.max_events = 3
        for i in range(3):
            self.assertEqual(self.post(**{"from": "a", "text": str(i)})[0], 200)
        self.assertEqual(self.post(**{"from": "a", "text": "one too many"})[0], 429)

    def test_wait_returns_when_n_agents_posted(self):
        def later():
            for name in ("a", "b", "c"):
                time.sleep(0.15)
                self.post(**{"from": name, "kind": "idea", "round": 1, "text": "hi"})
        threading.Thread(target=later, daemon=True).start()
        t0 = time.monotonic()
        code, r = self.request("GET", "/wait?round=1&n=3&timeout=20")
        self.assertEqual(code, 200)
        self.assertTrue(r["ready"])
        self.assertEqual(r["agents"], ["a", "b", "c"])
        self.assertLess(time.monotonic() - t0, 10)

    def test_wait_ignores_orchestrator_think_and_other_rounds(self):
        self.post(**{"from": "orchestrator", "kind": "say", "round": 1, "text": "go"})
        self.post(**{"from": "a", "kind": "think", "round": 1, "text": "reading"})
        self.post(**{"from": "b", "kind": "idea", "round": 2, "text": "early"})
        code, r = self.request("GET", "/wait?round=1&n=1&timeout=0.3")
        self.assertFalse(r["ready"])
        self.assertEqual(r["agents"], [])
        self.post(**{"from": "a", "kind": "done", "round": 1, "text": "done"})
        code, r = self.request("GET", "/wait?round=1&n=1&kinds=done&timeout=1")
        self.assertTrue(r["ready"])

    def test_export(self):
        self.post(**{"from": "builder", "kind": "join", "text": "makes things"})
        self.post(**{"from": "orchestrator", "kind": "say", "round": 0, "text": "Kick off"})
        self.post(**{"from": "builder", "kind": "idea", "round": 1, "text": "Ship a prototype"})
        code, md = self.request("GET", "/export")
        self.assertEqual(code, 200)
        self.assertIn("# Agent ring", md)
        self.assertIn("- **builder**: makes things", md)
        self.assertIn("## Round 1", md)
        self.assertIn("Ship a prototype", md)

    def test_save_and_reset(self):
        self.post(**{"from": "a", "kind": "idea", "round": 1, "text": "first panel"})
        code, r = self.request("POST", "/save", {})
        self.assertTrue(Path(r["saved"]).is_file())
        code, r = self.request("POST", "/reset", {"title": "Second topic"})
        self.assertEqual(code, 200)
        self.assertEqual(r["panel"], 2)
        self.assertIn("first panel", Path(r["saved"]).read_text(encoding="utf-8"))
        self.assertEqual(self.request("GET", "/board")[1], [])
        h = self.request("GET", "/health")[1]
        self.assertEqual((h["title"], h["panel"], h["last_round"]), ("Second topic", 2, 0))

    def test_reset_releases_waiters(self):
        threading.Timer(0.2, lambda: self.request("POST", "/reset", {})).start()
        code, r = self.request("GET", "/wait?round=1&n=2&timeout=20")
        self.assertFalse(r["ready"])
        self.assertTrue(r["reset"])

    def test_post_guards(self):
        self.assertEqual(self.request("POST", "/event", {"from": "a"}, {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/event", {"from": "a"}, {"Origin": "https://example.com"})[0], 403)
        self.assertEqual(self.request("POST", "/event", {"from": "a"}, {"Host": "evil.example:80"})[0], 403)
        self.assertEqual(self.request("GET", "/health", headers={"Host": "evil.example"})[0], 403)
        ok = {"Origin": f"http://127.0.0.1:{self.port}"}
        self.assertEqual(self.request("POST", "/event", {"from": "a"}, ok)[0], 200)

    def test_page_has_no_agent_text_and_strict_csp(self):
        self.post(**{"from": "<img src=x onerror=alert(1)>", "kind": "idea", "text": "<script>alert(1)</script>"})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/")
        r = conn.getresponse()
        page = r.read().decode()
        conn.close()
        self.assertEqual(r.status, 200)
        self.assertNotIn("alert(1)", page)
        self.assertNotIn("innerHTML", page)
        csp = r.getheader("Content-Security-Policy")
        self.assertIn("default-src 'none'", csp)
        self.assertIn("script-src 'sha256-", csp)

    def test_stream_replays(self):
        self.post(**{"from": "a", "kind": "idea", "round": 1, "text": "hello"})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/stream")
        r = conn.getresponse()
        self.assertEqual(r.getheader("Content-Type").split(";")[0], "text/event-stream")
        frames = []
        while len(frames) < 2:
            line = r.fp.readline().decode().strip()
            if line.startswith("data: "):
                frames.append(json.loads(line[6:]))
        conn.close()
        self.assertEqual(frames[0]["kind"], "hello")
        self.assertEqual(frames[1]["text"], "hello")
        self.assertTrue(frames[1]["replay"])

    def test_many_agents(self):
        names = [f"m{i}" for i in range(12)]
        for name in names:
            self.post(**{"from": name, "kind": "idea", "round": 1, "text": "x"})
        code, r = self.request("GET", "/wait?round=1&n=12&timeout=1")
        self.assertTrue(r["ready"])
        self.assertEqual(len(r["agents"]), 12)


class CliTest(unittest.TestCase):
    """Runs the script as a real process: serve, client commands, stop by PID."""

    def test_serve_and_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            ready = Path(tmp) / "ready.json"
            proc = subprocess.Popen([sys.executable, str(SCRIPT), "serve", "--port", "0", "--no-open", "--out", tmp,
                                     "--ready-file", str(ready)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            try:
                run = lambda *a, inp=None: subprocess.run([sys.executable, str(SCRIPT), *a], input=inp,
                                                         capture_output=True, timeout=30)
                h = run("health", "--ready-file", str(ready), "--wait", "10")
                self.assertEqual(h.returncode, 0, h.stderr)
                port = str(json.loads(h.stdout)["port"])
                p = run("post", "--port", port, "--from", "tester", "--kind", "idea", "--round", "1",
                        inp="It's \"quoted\" with $HOME and `ticks`\n".encode())
                self.assertEqual(p.returncode, 0, p.stderr)
                b = run("board", "--port", port, "--json")
                self.assertEqual(json.loads(b.stdout)[0]["text"], "It's \"quoted\" with $HOME and `ticks`")
                self.assertEqual(run("wait", "--port", port, "--round", "1", "--n", "1", "--timeout", "5").returncode, 0)
                self.assertEqual(run("wait", "--port", port, "--round", "1", "--n", "2", "--timeout", "0.5").returncode, 3)
                s = run("stop", "--port", port)
                self.assertEqual(s.returncode, 0, s.stderr)
                self.assertEqual(proc.wait(timeout=10), 0)
                self.assertEqual(len(list(Path(tmp).glob("agent-ring-*.md"))), 1)
                self.assertNotEqual(run("health", "--port", port).returncode, 0)
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=10)
                proc.stdout.close()


if __name__ == "__main__":
    unittest.main()
