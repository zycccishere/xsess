# xsess — one session index for Claude Code, Codex, Cursor, and Kimi

`xsess` builds a single searchable index over **all four** agents' transcripts, so any
one can find, read, and cite the others' history — and bundles as an agent skill, so
your agents can do this themselves (`/xsess` in Claude Code; works with Codex,
Cursor, and Kimi Code too via the [agent-skills](https://skills.sh) layout).

- **Claude Code**: `~/.claude/projects/**/*.jsonl` (plus `*/subagents/*.jsonl`) → refs `cc:<session-uuid>`
- **Codex**: `$CODEX_HOME/{sessions,archived_sessions}/**/rollout-*.jsonl`, with titles and
  metadata joined in from Codex's own `state_*.sqlite` and `session_index.jsonl` → refs `cx:<thread-uuid>`
- **Cursor**: `~/.cursor/projects/*/agent-transcripts/<sid>/<sid>.jsonl` → refs `cr:<session-uuid>`
- **Kimi Code**: `$KIMI_CODE_HOME/sessions/<wd>/<sessionId>/agents/<id>/wire.jsonl`
  (default `~/.kimi-code`) → refs `km:<session-uuid>`

Everything is stdlib Python in one file (`xsess.py`, ≥ 3.10), read-only with respect
to all four stores. Sessions imported from another machine are tagged `@<host>`.

## Install

As a skill (Claude Code / Codex / Cursor / Kimi Code, via [skills.sh](https://skills.sh)):

```bash
npx skills add zycccishere/xsess -g
```

…then, once, put the CLI on your PATH (the skill also reminds whoever invokes it):

```bash
curl -fsSL https://raw.githubusercontent.com/zycccishere/xsess/main/xsess.py \
  -o ~/.local/bin/xsess && chmod +x ~/.local/bin/xsess
```

Or plain git clone:

```bash
git clone https://github.com/zycccishere/xsess ~/.local/share/xsess
~/.local/share/xsess/install.sh     # symlinks xsess.py into ~/.local/bin
```

The first command you run builds the index (~30 s for ~1k files / ~10 GB); after
that every command parses only newly appended bytes, so a running session becomes
searchable within seconds.

## Try it

```bash
xsess list -n 20                          # recent sessions from all agents, with titles
xsess search "fully-async GRPO"           # full-text search, relevance ranked
xsess search 研究直觉 -a cx --role user    # CJK works; only the human's turns
xsess show cx:01a08c47 --around 412 -C 5  # read around a hit
xsess ref cx:01a08c47                     # one-line citation to paste anywhere
xsess grep 'CUDA out of memory' -a cx     # regex over the raw transcripts
xsess --help
```

Refs resolve like git shas: any **unique id prefix** works, and so does a distinctive
fragment of the natural-language **title** (`xsess show "autoresearch 研究直觉"`).
Ambiguity is reported with candidates, never guessed.

## Remote sessions (bundles)

`bundle` / `import` move sessions between machines over any pipe (usually ssh);
the other machine's transcripts are mirrored under the index dir and indexed
like local ones, tagged `@<host>` in `list` / `show` / `ref`. Both ends need the
same single-file `xsess.py`.

```bash
# laptop → this machine: push recent laptop sessions
xsess bundle --since 7d | ssh server 'xsess import -'
# this machine → laptop: pull specific sessions the other way
ssh server 'xsess bundle cc:9f2c cx:01a0b' | xsess import -
```

- A bundle is a tar.gz of the **raw transcript files** (store layout preserved,
  plus a leading `manifest.json` carrying the host tag and titles), so `search`,
  `show`, and even `grep` work on imported sessions, and re-importing a growing
  session parses only the new tail — the same freshness model as local files.
- Imported files land under `~/.local/state/session-bridge/remote/<host>/`,
  never inside the agents' own stores. A ref that already exists locally is
  skipped (local wins). To forget a machine, delete its mirror dir — the next
  sync drops its sessions automatically.
- `xsess bundle` takes refs / id prefixes / titles like `show`, or `--since 7d`
  (optionally `-a cx`); Claude subagent transcripts and their `.meta.json`
  sidecars travel along automatically; `--host` overrides the tag (default:
  hostname). `import` reads a file or stdin (`-`) and validates every member
  against the store layouts — path traversal and foreign files are refused.

## Layout

| what | where |
| --- | --- |
| CLI | `xsess.py` here; symlink/copy it as `~/.local/bin/xsess` (or run `install.sh`) |
| skill | this repo's root `SKILL.md` (installed by `npx skills add` into your agents' skill dirs) |
| index (derived, disposable) | `~/.local/state/session-bridge/index.db` (override with `$XSESS_DB`) |
| remote mirrors | `~/.local/state/session-bridge/remote/<host>/…` (bundles imported from other machines) |

The index deliberately lives on **local disk**: SQLite `fsync` on a network FS can
cost ~16 s per commit versus ~30 s for a *full* rebuild locally.

## How the index stays fresh

Transcripts are append-only, so every command does a cheap sync first: for each file it
compares size with `bytes_indexed` and parses only the new tail. A running
session becomes searchable within seconds, no daemon involved. `--no-sync` skips it;
`xsess index --full` rebuilds from scratch.

## Data model

`sessions` (one row per session/thread) + `messages` (one row per item, `seq` unique
within a session) + a contentless FTS5 index. Items carry a role:

| role | meaning |
| --- | --- |
| `user` | what the human typed (harness-injected pseudo-user blocks are demoted to `meta`) |
| `assistant` | agent reply text |
| `reasoning` | Claude thinking blocks / Codex reasoning summaries |
| `tool` | one summarised line per tool call (command, path, query…) |
| `tool_out` | tool output, truncated to ~600 chars |
| `summary` | compaction summaries |
| `agent_msg` | Codex inter-agent messages |
| `meta` | developer/system/injected context |

Search defaults to what was actually said (`user`, `assistant`, `summary`, `agent_msg`);
`--role reasoning` or `--role all` widens it. Since tool output is truncated, text that
only ever appeared in a long output is found with `xsess grep`, which scans the raw files
instead.

## Codex specifics worth knowing

- A rollout filename is `rollout-<ts>-<thread_id>[_<continuation>].jsonl`, and that
  **first** uuid is the key Codex's own state DB uses for the thread. The `id` inside
  `session_meta` is the conversation *root*, shared by every resume/fork of it — keying
  on it would collapse unrelated threads. The root is kept in `sessions.root` for
  cross-linking.
- A resumed thread can therefore span several files; `seq` numbering is per session and
  files are stored in chronological order.
- Titles come from `threads.name` → `threads.title` → `session_index.jsonl` →
  first user message (in that priority order). Claude titles come from the `ai-title`
  line, falling back to the subagent's `.meta.json` description, then the first prompt.
- Cursor has no title store: the first `<user_query>` becomes the title. Timestamps are
  parsed from `<timestamp>` tags on user turns and carried forward. `cwd` is recovered
  from Cursor's project-folder slug. Tool results are not persisted in these transcripts,
  so `tool_out` is usually empty.
- Kimi Code's event stream is `wire.jsonl`. User turns come from `turn.prompt`;
  assistant text / thinking / tools come from `context.append_loop_event`. Titles prefer
  `state.json.title` over the first prompt. The session id is the `session_<uuid>`
  directory name without the prefix; subagent wires are keyed by agent id and parented
  to that session.

## License

MIT — see [LICENSE](LICENSE).
