---
name: xsess
description: "跨 agent 会话检索与阅读：用一个本地索引 search / show / grep Claude Code、Codex、Cursor、Kimi Code 的全部历史 session，ref 形如 cc:/cx:/cr:/km:<uuid>，并支持跨机器 bundle/import 搬运会话。当用户要找或引用某次历史对话（'codex 上那个…'、'kimi 那边说的'）、问某个结论/报错是什么时候讨论的、按自然语言标题定位会话、或要在两台机器间共享 sessions 时使用。不负责当前会话的上下文压缩，也不替代读代码或读文件。"
---

# xsess — one index over all your agent sessions

`xsess` 是单文件 stdlib Python CLI，把四个 agent 的 transcript 全部索引进一个本地
SQLite+FTS5（~10 GB 原始文件 → 秒级全文检索），任何一边都能 list / search / show /
grep 另一边（或自己）的历史：

- **Claude Code** — `~/.claude/projects/**/*.jsonl` → refs `cc:<session-uuid>`
- **Codex** — `$CODEX_HOME/{sessions,archived_sessions}/**/rollout-*.jsonl` → `cx:<thread-uuid>`
- **Cursor** — `~/.cursor/projects/*/agent-transcripts/**/*.jsonl` → `cr:<session-uuid>`
- **Kimi Code** — `$KIMI_CODE_HOME/sessions/**/agents/*/wire.jsonl` → `km:<session-uuid>`

索引位于 `~/.local/state/session-bridge/index.db`（可用 `$XSESS_DB` 覆盖），对四个
store 只读，永不写入 agent 自己的目录。

## 安装（一次性）

`xsess.py` 与本 SKILL.md 同目录。若 `xsess` 还不在 PATH 上：

```bash
mkdir -p ~/.local/bin
ln -sf "<本 skill 所在目录>/xsess.py" ~/.local/bin/xsess
chmod +x ~/.local/bin/xsess
```

或直接从 GitHub 拉：

```bash
curl -fsSL https://raw.githubusercontent.com/zycccishere/xsess/main/xsess.py \
  -o ~/.local/bin/xsess && chmod +x ~/.local/bin/xsess
```

需要 python3 ≥ 3.10。首次执行任何命令时会自动建索引（一次性，~1k 文件约 30s）；
之后每条命令只增量解析新写入的字节，运行中的会话几秒内即可搜到（`--no-sync` 跳过）。

## Refs

`cc:` = Claude Code，`cx:` = Codex，`cr:` = Cursor，`km:` = Kimi Code。任何**唯一 id
前缀**都行（`cx:01a08c47`，像 git sha）；标题里有辨识度的**片段**也可以
（`xsess show "autoresearch"`）。有歧义时会列出候选，绝不瞎猜。

向用户汇报检索结果时引用 ref（如 `cx:01a08c47`）——这个字符串让对方或其他 agent
能直接跳过去。`xsess ref <ref>` 输出可直接粘贴的一行引用。

## Commands

```bash
xsess list -n 20                        # 最近会话，全部 agent，带标题
xsess list -a cx --since 3d             # 只看 Codex，最近 3 天   (-a cc / -a cr / -a km)
xsess list --title 训练 -l              # 按标题过滤；-l 附 cwd/model/完整 id

xsess search "fully-async GRPO"         # 全文检索，按相关性排序，中英文均可
xsess search 研究直觉 -a cx --role user  # 只搜人类说过的话
xsess search "OOM" --since 7d --recent  # 按时间排序
xsess search X --session cx:01a08c47    # 限定在某个会话内

xsess show cx:01a08c47                  # 读会话开头（user/assistant/tool 行）
xsess show cx:01a08c47 --tail 30        # 它是怎么结束的
xsess show cx:01a08c47 --around 412 -C 5  # 围绕某个 search 命中的 #seq 读上下文
xsess show <ref> --range 100:160 --role user,assistant
xsess show <ref> --grep vllm            # 只看包含某子串的条目
xsess show <ref> --full                 # 含 reasoning 与完整 tool 输出

xsess ref <ref>                         # 一行引用
xsess grep 'CUDA out of memory' -a cx   # 对原始 transcript 的正则深扫（慢）
xsess stats                             # 索引覆盖面；xsess which = 我自己在哪个会话
```

`list` / `search` / `show` 支持 `--json` 便于后处理。

## Remote sessions（跨机器）

`bundle` / `import` 通过任意管道（通常 ssh）在两台机器间搬运会话；对方的 transcript
被镜像到本机索引目录下、像本地会话一样被索引，`list` / `show` / `ref` 里带 `@<host>`
标记，ref 形式不变：

```bash
xsess bundle --since 7d | ssh server 'xsess import -'    # 本机会话 → 服务器
ssh server 'xsess bundle cc:9f2c' | xsess import -       # 服务器 → 本机
```

`bundle` 接受 refs / id 前缀 / 标题（同 `show`），或 `--since 7d`（可加 `-a cx`）；
Claude subagent transcript 与其 `.meta.json` 自动携带。重复导入只解析新增的尾部字节；
已存在于本机的 ref 会被跳过（本地优先）。删除
`~/.local/state/session-bridge/remote/<host>/` 即遗忘该机器。

## Roles

每条索引项带角色：`user`（人类输入；harness 注入的伪 user 块降级为 `meta`）、
`assistant`、`reasoning`、`tool`（每次调用一行摘要）、`tool_out`（截断到 ~600 字符）、
`summary`（压缩摘要）、`agent_msg`（Codex 跨 agent 消息）、`meta`。search 默认只搜
真正"说过"的内容（user/assistant/summary/agent_msg），`--role reasoning` / `--role all`
按需放宽。长 tool 输出里才出现的文本用 `xsess grep`（扫原始文件）。

## Working recipe

1. `xsess search <你记得的词>` —— 对话用什么语言就用什么语言搜。记下 ref 和最佳命中的 `#seq`。
2. `xsess show <ref> --around <seq> -C 6` —— 读上下文。
3. 不够再用 `--range`、`--role all`、`--full` 放宽。
4. 汇报时带上 ref，让结论可追溯。

用户按**内容**描述时用 `search`；按**时间**描述时（"昨天那个"、"上周和 codex 聊的"）用
`list`。subagent 会话默认不列出——工作由 subagent 完成时加 `--all-kinds`。

## Notes

- 索引是派生数据，可安全删除：`xsess index --full` 全量重建。
- 对四个 agent store 严格只读；镜像的远端会话放在索引目录下的 `remote/`，绝不写进
  `~/.claude` 等真实 store。
- 上游：<https://github.com/zycccishere/xsess>
