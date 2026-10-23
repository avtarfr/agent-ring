---
name: agent-ring
description: Run a live ideation panel of 3 to 12 subagents that brainstorm, debate and critique a topic in rounds, shown as pixel critters seated on a ring in a local browser page, then summarise every idea and objection. Use when the user asks to "run an ideation panel", "brainstorm with a team of agents", "debate with N agents", "get a panel to argue about", "agent ring", "panel of experts on", or wants many perspectives on one question generated in parallel and synthesised.
---

# Agent ring

You are the orchestrator. You seat a panel of subagents on a local "ring" server, they post ideas and replies
to each other in synchronised rounds, the user watches it live in a browser, and you summarise at the end.
Everything stays on 127.0.0.1.

Throughout this file, `RING` means:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/agent_ring.py"
```

Write that out in full in every command (and in the brief), with `${CLAUDE_SKILL_DIR}` already replaced by
its absolute path. On Windows, use `python` or `py -3` if `python3` is not found. The script needs Python 3.9+
and nothing else. Every client command accepts `--port P`; pass it whenever the ring is not on 8790.

## 1. Plan the panel and state the cost

Settle these from the request, asking only for what you can't infer:

- **Topic and question**: what the panel should produce (ideas, a decision, a critique of a plan, risks...).
- **Size N**: default 6, minimum 3, maximum 12. If asked for more than 12, use 12 and say so.
- **Members**: one short lowercase name per member (`skeptic`, `builder`, `user-advocate`), unique, never
  `all` or `orchestrator`, each with a one-line angle. Use the user's roles if given; otherwise pick N angles
  that genuinely differ (for example practitioner, skeptic, newcomer, researcher, economist, designer,
  operator, contrarian, ethicist, domain expert for the topic).
- **Rounds**: default three (ideas, replies, top picks), see section 3. Use the user's rounds if given.
- **Context**: files or folders the members may read (read-only), or none.

Before launching, tell the user in one or two lines: the roster, the number of rounds, and that this starts
**N background subagents**, which costs roughly N times a single agent doing the job. If the user already asked
for this panel with an explicit size, go ahead; otherwise wait for a yes. If they want it cheaper, offer fewer
members or a smaller model for the members.

## 2. Start the ring, or reuse a running one

Make a working folder for the brief and the ready file:

```bash
python3 -c "import tempfile; print(tempfile.mkdtemp(prefix='agent-ring-'))"
```

Call it `WORK`. Check for a ring that is already running:

```bash
RING health
```

**If it prints JSON with `"app": "agent-ring"`**, reuse it:

- Same discussion, more rounds: keep it. Number new rounds from `last_round + 1`; the board and the critters
  carry over.
- New topic: `RING reset --title "<short topic>"`. That saves the previous transcript and starts a fresh panel
  with rounds from 1.

**Otherwise start one** with the Bash tool and `run_in_background: true`:

```bash
RING serve --title "<short topic>" --out "<transcript folder>" --ready-file "WORK/ring.json"
```

- `--out`: the user's current working directory unless they name another folder.
- It binds 127.0.0.1 only, tries 8790 first and moves to the next free port if that is busy, and opens the
  page in the default browser. Add `--no-open` when the user is on a remote or headless machine, and give
  them the URL instead.

Then confirm it is up and learn the port and PID (this waits up to 15 s, so no sleeping):

```bash
RING health --ready-file "WORK/ring.json" --wait 15
```

Note `port`, `pid` and `url`. Tell the user the URL in one line.

## 3. Write the brief

Read `${CLAUDE_SKILL_DIR}/templates/brief.md`, fill every `{{...}}` placeholder, and save it as `WORK/brief.md`.
The brief is the only thing the members know, so it must stand alone.

- `{{TOPIC}}`: short title. `{{QUESTION}}`: the full question, constraints, and what a good answer looks like.
- `{{CONTEXT}}`: background from the conversation the members need, plus any read-only paths with a budget
  ("spend at most about 10 tool calls reading"). Write "None." if there is none.
- `{{ROSTER}}`: one line per member, `- name: angle`.
- `{{N}}`: the panel size. `{{RING}}`: the full command with the absolute script path, plus `--port P` if the
  port is not 8790. `{{MAX_CHARS}}`: 350 by default.
- `{{R1}}`: the first round number (1, or `last_round + 1` on a reused ring).
- `{{READ_ALLOWANCE}}`: empty, or ", plus reading the context files listed above".
- `{{ROUNDS}}`: the rounds. Default, with R1, R2, R3 as consecutive numbers:

  1. **Round R1, ideas.** Post `--kind think --text "reading"` first. Read the context if any. Post 3 to 5
     ideas from your angle (`--kind idea --to all --round R1`), one per message. Then wait for round R1.
  2. **Round R2, replies.** `board --round R1`. Post 2 to 4 replies (`--kind reply --to <member> --round R2`)
     that build on, merge, sharpen, or push back on specific ideas, naming them. Then wait for round R2.
  3. **Round R3, picks.** `board --round R2`. Post your top 3 across the whole panel, yours or others'
     (credit them), one per message (`--kind idea --to orchestrator --round R3`). Then post
     `--kind done --to orchestrator --round R3 --text "done"`.

  Custom rounds follow the same shape: each says what to read, what to post (kind, to, how many) and ends with
  `wait --round <that round> --n N`. The last round always ends with the `done` post.
- `{{FINAL}}`: what each member returns to you when finished. Default: "Your top 3, each with a name, a
  one-paragraph description, effort (S/M/L), and the strongest objection raised against it with your answer.
  Under 400 words."

## 4. Seat the panel and kick off

Seat every member so the critters appear in order around the ring (one command each, run them in parallel):

```bash
RING post --from <name> --kind join --round 0 --text "<angle>"
```

Post the kickoff from the centre, in the round just before R1 (0 on a fresh ring):

```bash
RING post --from orchestrator --to all --kind say --round <R1 - 1> <<'EOF'
<topic in one sentence>. Round R1: your ideas. Round R2: answer each other by name. Round R3: your top 3 to me.
EOF
```

## 5. Launch the members

In **one message**, make N Agent tool calls (general-purpose subagents, in the background where supported,
`model` set only if the user chose a cheaper one). Each prompt:

> You are **<name>**, a member of an ideation panel. Your angle: <angle>. Your name on the ring is `<name>`.
> Read WORK/brief.md now and follow it exactly, using only the commands it gives. When you have posted your
> `done` message, return the final answer the brief asks for.

Then post `RING post --from orchestrator --kind think --round <R1> --text "listening"` and wait. Do not do the
members' work, and do not poll the board or loop. You are notified as each member finishes. If background
notifications are not available, block once with
`RING wait --round <last round> --n N --kinds done --timeout 540` (Bash timeout 600000 ms), and once more if it
exits 3.

If a member fails or never posts `done`, carry on with the rest and say so in the summary.

## 6. Wrap up

1. Read everything: `RING board` (or `RING export` for Markdown), plus each member's final answer.
2. Post a short wrap-up from the centre so the watcher sees it land:
   `RING post --from orchestrator --to all --kind say --round <last round> <<'EOF' ... EOF`
   (the top three ideas in a sentence each).
3. Save the transcript: `RING save` prints its path.
4. Summarise for the user:
   - **Every idea**, grouped by theme, merged where members converged, credited by name. Don't drop the minor
     ones; list them briefly.
   - **Objections and answers**: each real pushback from the reply round, who raised it, and how (or whether)
     it was answered.
   - **The panel's picks**: tally the round-R3 votes and give the top ones with the strongest case for and
     against each.
   - **Disagreements still open** and what would settle them.
   - The transcript path.
5. Stop the ring unless the user wants to keep watching or run more rounds:

   ```bash
   RING stop --port P
   ```

   That saves the transcript and terminates exactly the PID the server reports about itself. If it fails, kill
   that one PID from `health` (`kill <pid>`, or `taskkill /PID <pid>` on Windows). Never use `pkill -f`,
   `killall`, or anything that matches by name.

## More rounds later

To continue on a running ring, check `RING health`, take `last_round`, write a short follow-up brief whose
rounds start at `last_round + 1`, and either message the existing members (if your tools can continue an agent)
or launch fresh ones. Fresh members have no memory of the earlier rounds, so tell them to read
`RING board` first. For a new topic, `RING reset --title "..."` first.

## Protocol reference

An event is JSON posted to `POST /event` with `Content-Type: application/json`:

```json
{"from": "skeptic", "to": "all", "kind": "idea", "round": 1, "text": "..."}
```

- `to`: `all`, a member name, or `orchestrator`. `kind`: `join`, `think`, `idea`, `reply`, `done`, `say`.
- `round`: 0 to 10000. `text`: up to 4000 characters; the body up to 16 KiB. At most 2000 events and 32 speakers
  per panel; `POST /reset` starts a fresh one.
- `GET /wait?round=N&n=K&kinds=idea,reply,done&timeout=S` returns when K different members (not the
  orchestrator) have posted one of those kinds in round N, or at the timeout (`"ready": false`).
- Other endpoints: `GET /health`, `GET /board?round=N&since=ID`, `GET /export`, `POST /save`, `POST /reset`,
  `GET /stream` (the page's live feed), `GET /` (the page).

The `RING post | board | wait | health | reset | save | export | stop` commands wrap these, so members never
need curl or hand-written JSON.
