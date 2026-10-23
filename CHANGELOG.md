# Changelog

## 0.1.0 (2026-10-23)

First release.

- `agent-ring` skill: runs an ideation panel of 3 to 12 subagents in synchronised rounds (ideas, replies by
  name, top picks), with custom roles and rounds, reuse of a running ring, and a full summary at the end.
- `agent_ring.py`: a standard-library Python 3.9+ server on 127.0.0.1 with a live page (pixel critters on a
  ring, pulses, speech bubbles, conversation log, light and dark), `/event`, `/board`, `/wait`, `/stream`,
  `/export`, `/save`, `/reset` and `/health`, plus client commands (`post`, `board`, `wait`, `health`, `reset`,
  `save`, `export`, `stop`) so members need neither curl nor hand-written JSON.
- Port fallback from 8790, `--title`, `--out`, `--no-open`, `--ready-file`; transcript saved on exit.
- Hardening: loopback only, Host and Origin checks, JSON-only POSTs, size and count caps, text-only rendering
  under a hash-based Content-Security-Policy.
- Packaged as a Claude Code plugin and a single-plugin marketplace; also installable by copying the skill folder.
