---
name: podcast2obsidian
description: Podcast2Obsidian 是小宇宙 / Apple Podcasts / Pocket Casts / Goodpods（仅已配置 resolver）/ RedCircle / Bilibili / YouTube 的纯脚本归档流水线。日常入口不是 agent/sub-agent，而是本地 xiaoyuzhou-dispatcher 的 before_dispatch hook 自动拦截 Telegram 群里的受支持 URL 或 BV 号，直接后台调用 projects/podcast2obsidian/scripts/run_profile.sh；runner 负责日志、NotebookLM/Fast Note/Drive/通知。Use when maintaining/debugging Podcast2Obsidian scripts, dispatcher, profiles, or manually running the wrapper.
---

# Podcast2Obsidian — 播客下载器 + Bilibili/YouTube 视频归档

Podcast2Obsidian 是纯脚本归档流水线：小宇宙 / Apple Podcasts / Pocket Casts / Goodpods（仅已配置 resolver）/ RedCircle / Bilibili / YouTube 的日常处理由本地 dispatcher 直接触发 `run_profile.sh` 后台执行，不需要 agent/sub-agent 介入。脚本内部负责 NotebookLM、Fast Note、Drive、日志和完成通知。

## Runtime architecture (script-first)

- Dispatcher plugin: `~/.openclaw/extensions/xiaoyuzhou-dispatcher/index.js`
- Runtime hook: `before_dispatch`
- Telegram group: `-100XXXXXXXXXX`
- Project root: `$HOME/.openclaw/workspace/projects/podcast2obsidian/`
- Profile config: `projects/podcast2obsidian/profiles/<profile>/config.env`
- Runner/wrapper: `projects/podcast2obsidian/scripts/run_profile.sh`
- Log helper: `projects/podcast2obsidian/scripts/send_log.sh`

Daily user flow:
1. User sends a supported podcast URL, Bilibili URL/BV id, or YouTube URL in the configured Telegram group.
2. `xiaoyuzhou-dispatcher` intercepts the message before agent dispatch.
3. It spawns `run_profile.sh --profile <profile> --url <target> --notify-chat-id ...` in the background.
4. `run_profile.sh` writes logs, parses `📣 RESULT`, and sends the final completion notification.

Manual/debug entry only:

```bash
bash $HOME/.openclaw/workspace/projects/podcast2obsidian/scripts/run_profile.sh \
  --profile <profile> --url '<episode_or_video_url_or_BV>'
```

Dry-run / preflight:

```bash
bash $HOME/.openclaw/workspace/projects/podcast2obsidian/scripts/run_profile.sh \
  --profile <profile> --dry-run
```

Wrapper maintenance shortcuts:

```bash
# 只读扫描 Drive 历史重复
bash $HOME/.openclaw/workspace/projects/podcast2obsidian/scripts/run_profile.sh \
  --profile <profile> --scan-drive-duplicates

# 清理 Drive 历史重复（移到垃圾桶）
bash $HOME/.openclaw/workspace/projects/podcast2obsidian/scripts/run_profile.sh \
  --profile <profile> --dedupe-drive --apply
```

Canonical Xiaoyuzhou profile:
- `projects/podcast2obsidian/profiles/xiaoyuzhou/config.env`
- legacy alias kept for compatibility: `projects/podcast2obsidian/profiles/podcaster -> xiaoyuzhou`
- supported podcast URL sources currently routed through this profile: Xiaoyuzhou, Apple Podcasts single episodes, Pocket Casts, Goodpods with configured resolvers only, and RedCircle audio direct links
- Goodpods is not generic support. As of 2026-06-04, the only configured Goodpods mirror is `反派影评 -> QTFM 200050`, and that QTFM channel has only 19 programs.

NotebookLM profile routing:
- Route Podcast2Obsidian jobs by editing `NLM_PROFILE` in `projects/podcast2obsidian/profiles/<profile>/config.env`; do **not** change the `nlm` CLI `default` profile unless explicitly requested.
- `run_profile.sh` exports sourced `config.env` variables (`set -a`), so launchd / dispatcher / manual wrapper runs all inherit `NLM_PROFILE`.
- Current default route: `projects/podcast2obsidian/profiles/xiaoyuzhou/config.env` uses `NLM_PROFILE="default"` (primary-nlm-account).
- Podcasts explicitly registered in `PODCAST_ROUTING[*].nlm_profile` keep their own profile, e.g. newsletter-style podcasts on `secondary`.
- `PODCAST_ROUTING` 现在是**外部 JSON 配置**，不再硬编码：默认读 `projects/podcast2obsidian/podcast_routing.json`（可用环境变量 `PODCAST_ROUTING_FILE` 覆盖），文件缺失则所有节目走默认行为。字段模板见 `projects/podcast2obsidian/podcast_routing.example.json`。短语定位（如 `'<节目名> 第12期'`）由 route 的 `phrase_patterns` 字段驱动。
- Verify routing before/after changes:
  ```bash
  grep '^NLM_PROFILE=' $HOME/.openclaw/workspace/projects/podcast2obsidian/profiles/xiaoyuzhou/config.env
  bash $HOME/.openclaw/workspace/projects/podcast2obsidian/scripts/run_profile.sh --profile xiaoyuzhou --url '<episode_url>' --dry-run
  ```
- Bilibili and YouTube configs already default to `secondary` unless changed in their own `config.env` files.

NotebookLM queue pruning:
- Podcast2Obsidian treats configured NotebookLM profiles as rolling queues: before creating a notebook, if the active profile is at/above its threshold, delete the oldest notebook first.
- `NLM_PRUNE_BEFORE_CREATE_PROFILES` defaults to `secondary,default`.
- Profile thresholds default to: `default` / primary-nlm-account = **485**; `secondary` = **85**.
- Override thresholds with `NLM_PRUNE_MAX_COUNT_BY_PROFILE="default:485,secondary:85"` or legacy `NLM_PRUNE_MAX_COUNT`.

Bilibili profile:
- `projects/podcast2obsidian/profiles/bilibili/config.env`
- preferred command:
  ```bash
  bash $HOME/.openclaw/workspace/projects/podcast2obsidian/scripts/run_profile.sh \
    --profile bilibili --url '<bilibili_video_url_or_BV>'
  ```
- behavior: always fetch metadata first; subtitle track present → upload full transcript as NotebookLM text source → summarize → save Markdown under `Video/<UP主>/`; no usable subtitle → `bili audio --no-split` → NotebookLM audio summary path. Default NotebookLM profile: `secondary`.
- debug/local:
  ```bash
  bash $HOME/.openclaw/workspace/projects/podcast2obsidian/scripts/run_profile.sh \
    --profile bilibili --url '<BV>' --local
  bash $HOME/.openclaw/workspace/projects/podcast2obsidian/scripts/run_profile.sh \
    --profile bilibili --url '<BV>' --force-audio --no-notebooklm
  ```

YouTube profile:
- `projects/podcast2obsidian/profiles/youtube/config.env`
- preferred command:
  ```bash
  bash $HOME/.openclaw/workspace/projects/podcast2obsidian/scripts/run_profile.sh \
    --profile youtube --url '<youtube_video_url_or_id>'
  ```
- behavior: fetch metadata; choose the best original-language/default transcript, preferring uploaded/manual subtitles over auto-generated for that language; upload transcript as NotebookLM text source → summarize in the original subtitle language → save Markdown under `Video/<Channel>/`. Default NotebookLM profile: `secondary`.
- filename/note title: `YYYY-MM-DD Channel 标题.md`.

## Agent boundary (mandatory)

Agents should not be in the daily execution path.

- For normal user-submitted supported podcast/Bilibili/YouTube targets, rely on the dispatcher + runner. Do not spawn a sub-agent just to process media.
- Use agent/tool work only for maintenance, debugging, code changes, validation, or explicit manual runs.
- Manual runs must use the project wrapper, not direct script paths:
  ```bash
  bash $HOME/.openclaw/workspace/projects/podcast2obsidian/scripts/run_profile.sh --profile <profile> --url '<target>'
  ```
- Direct `python ...xiaoyuzhou_dl.py` / `python ...bilibili_video.py` invocation is fallback/debug only.
- Runtime plugin validation must use:
  ```bash
  openclaw plugins inspect xiaoyuzhou-dispatcher --runtime --json
  ```
  Plain plugin listing may show metadata-only state and is not enough for hook registration.
- If a manual wrapper run is started, final reporting must quote `📣 RESULT` fields: `notebook_id`, `note_id`, `note_path`, and status-specific fields such as `subtitle=yes/no`.

## 依赖

- Python 3
- curl (系统自带)
- `gog` CLI (`~/.local/bin/gog`) — Drive 上传/回拉需要
- `nlm` CLI (`~/.local/bin/nlm`) — 单集默认上传 NotebookLM 需要
- `bili` CLI (`~/.local/bin/bili`) — Bilibili 无字幕视频的音频下载、字幕 CLI 兜底需要

## 用法

### 单集下载（手动/调试）

```bash
# 默认：通过 wrapper 跑 xiaoyuzhou profile
bash $HOME/.openclaw/workspace/projects/podcast2obsidian/scripts/run_profile.sh \
  --profile xiaoyuzhou --url '<episode_url>'

# 仅下载到本地
bash $HOME/.openclaw/workspace/projects/podcast2obsidian/scripts/run_profile.sh \
  --profile xiaoyuzhou --url '<episode_url>' --local
```

### Bilibili 视频

```bash
# 手动/调试：有字幕 → transcript text source → NotebookLM → Fast Note；无字幕 → 音频 → NotebookLM → Fast Note
bash $HOME/.openclaw/workspace/projects/podcast2obsidian/scripts/run_profile.sh \
  --profile bilibili --url 'https://www.bilibili.com/video/BV...'

# 只做本地验证，不写 Fast Note / NotebookLM
bash $HOME/.openclaw/workspace/projects/podcast2obsidian/scripts/run_profile.sh \
  --profile bilibili --url 'BV...' --local

# 强制走音频路径（调试用）
bash $HOME/.openclaw/workspace/projects/podcast2obsidian/scripts/run_profile.sh \
  --profile bilibili --url 'BV...' --force-audio
```

Bilibili Markdown 会保留 Clipper-style frontmatter、封面、iframe、`## 简介`、NotebookLM 总结；有字幕时末尾追加 `## 字幕全文`（正文不带逐行时间戳）。frontmatter 的 `subtitle` 只写 `yes` / `no`，不写 `page`。文件名/笔记名格式为 `YYYY-MM-DD UP主 标题.md`。

### YouTube 视频

```bash
# 手动/调试：best original-language transcript → NotebookLM → Fast Note
bash $HOME/.openclaw/workspace/projects/podcast2obsidian/scripts/run_profile.sh \
  --profile youtube --url 'https://www.youtube.com/watch?v=...'

# 只做本地验证，不写 Fast Note / NotebookLM
bash $HOME/.openclaw/workspace/projects/podcast2obsidian/scripts/run_profile.sh \
  --profile youtube --url 'https://www.youtube.com/watch?v=...' --local
```

YouTube Markdown 输出对齐 Bilibili：frontmatter、封面、iframe、`## 简介`、NotebookLM 总结、`## 字幕全文`。字幕选择保留原语言/default 语种，同语种优先 uploaded/manual，其次 auto-generated。

### 全集下载（手动/调试）

```bash
# 上传到 Google Drive (通过 RSS 获取完整列表)
bash $HOME/.openclaw/workspace/projects/podcast2obsidian/scripts/run_profile.sh \
  --profile xiaoyuzhou --url '<podcast_url>'

# 仅下载到本地
bash $HOME/.openclaw/workspace/projects/podcast2obsidian/scripts/run_profile.sh \
  --profile xiaoyuzhou --url '<podcast_url>' --local
```

> 全集下载会先询问是否确认下载；确认后仅走 Drive 流程，默认不上传 NotebookLM。

## 输出结构

```
Podcasts/<节目名>/<单集标题>/
├── <标题>.m4a        # 音频
├── <单集链接>.txt    # 小宇宙链接 (文件名为链接，已做文件名清理)
└── metadata.json     # 元数据 (标题、时长、发布日期等)
```

- **Drive 模式**: 上传到 `Podcasts/` 文件夹，本地临时文件自动清理
- **本地模式**: 保存到 `~/Downloads/Podcasts/`

## 功能

- 从网页 `__NEXT_DATA__` 提取元数据 (无需登录/API)
- **全集下载通过 RSS feed** 获取完整单集列表 (解决网页分页限制)
- 单集下载后默认 **新建 NotebookLM notebook 并仅上传音频 source**
- 音频 source 上传确认后，默认先触发 1 个 NotebookLM infographic：**clay style**；该生成不依赖后续 query，可与总结并行等待
- 上传完成后默认自动触发一次总结：**“帮我总结这期播客”**，回答长度设为 **longer**
- 默认把 `notebook query --json` 的 `value.answer` 落地到 Fast Note / Obsidian，结构为：一级标题（与文件同名，并链接到小宇宙原链接）→ 一段 NotebookLM 摘要 summary → `## Shownotes`（把时间线**上方**的原始 shownotes 以**结构保真**方式整段复制：保留原顺序、原链接、原图片、原列表、原引用、粗体等；不再拆成 `### 导语` / `### 图片`）→ clean 后的 Markdown 正文 → `## 补充 Shownotes`（从 timeline extractor 识别到的**最后一个时间线块之后的首个非时间线块**开始，以同样的结构保真方式整体复制；遇到第一条黑名单即截断。黑名单包含收听平台、投稿、问卷、订阅/推广、招聘、商务合作、品牌介绍、其他播客矩阵、联系方式，以及 `Honghub/鸿鹄汇` 等宣传词）
- clean 规则默认移除 NotebookLM 引用标记（支持 `[1]`、`[2, 3]`、`[21, 23-25]` 等），但保留 `**粗体**` 等 Markdown 结构
- Markdown 保存后会检查/等待已触发的 infographic，下载 PNG，写入 Fast Note 附件库，并把 `![[NotebookLM infographic <notebook_id前8位>.png]]` 插到开头引用综述下方、`## Shownotes` 上方
- `PODCAST_ROUTING[*].skip_infographic=true` 和 CLI `--no-infographic` 仍然是硬开关：命中后不会触发 infographic，也不会在 Markdown 中写入图片
- 上述 infographic 统一参数：**English (`en`) / landscape / standard**
- 每次触发 NotebookLM 前先做 `nlm login --check`；若会话过期则自动补跑一次 `nlm login`
- 默认按环境变量 `NLM_PROFILE` 选择 NotebookLM profile；未设置时回落到 `default`
- `nlm download ...` 子命令不接受 `--profile` 参数；下载类命令必须通过 `NLM_PROFILE` 环境变量选账号，不要把 profile 参数追加到命令行
- 小宇宙绑定 profile（`projects/podcast2obsidian/profiles/xiaoyuzhou/config.env`）当前默认：`NLM_PROFILE="default"`（primary-nlm-account）；不要改 `nlm` CLI 的 `default` profile。
- 若笔记“已写入但不可见 / 没落盘”，不要手改 DB；改用 `skills/fast-note-repair/scripts/repair_fast_note.sh` 做正规化重写修复
- 音频 `file source` 上传若首次失败，会自动刷新一次 NotebookLM 认证并重试 1 次
- 若该单集已在 Google Drive，则**优先从 Drive 回拉音频再上传 NotebookLM**，避免重复从源站下载
- 时间轴默认从 `shownotes` / `description` 中按层级结构脚本化提取：时间戳标题 → 二级标题；若该标题下 bullet ≤ 3 且每条不长，则转成三级标题写进 NotebookLM prompt；若超过阈值或是一整段说明，则不塞进 prompt，而是在最终 Markdown 中追加到对应二级标题下面
- 自动跳过已下载的单集 (skip-if-exists)
- 支持中文标题和特殊字符
- 顺序下载，每集间隔 2 秒 (避免触发反爬)

## 示例（手动/调试）

```bash
# 下载单集并上传到 Drive/NotebookLM/Fast Note（按 profile 行为）
bash $HOME/.openclaw/workspace/projects/podcast2obsidian/scripts/run_profile.sh \
  --profile xiaoyuzhou --url "https://www.xiaoyuzhoufm.com/episode/6268dc3667427058b84519da"

# 下载整个播客 (全集)
bash $HOME/.openclaw/workspace/projects/podcast2obsidian/scripts/run_profile.sh \
  --profile xiaoyuzhou --url "https://www.xiaoyuzhoufm.com/podcast/67e366fa1c465530de1f9d61"

# Bilibili 视频
bash $HOME/.openclaw/workspace/projects/podcast2obsidian/scripts/run_profile.sh \
  --profile bilibili --url "BV11o4y1s7VY"
```

## Newsletter watcher 运维规范

适用于：`skills/podcast2obsidian/scripts/news_podcasts_daily.py`

- **订阅源是外部配置，不再硬编码在脚本里**。解析优先级：
  1. `--source URL`（可重复，优先级最高）
  2. `--sources-file PATH`（每行一个小宇宙播客链接，`#` 之后为注释）
  3. 默认文件 `projects/podcast2obsidian/newsletter_sources.txt`（或环境变量 `NEWS_SOURCES_FILE`）
  - 起步模板：`projects/podcast2obsidian/newsletter_sources.example.txt`（例：`资讯早七点`）
- 日常 launchd 运行用默认 sources 文件即可；临时跑某几个源用 `--source`：
  ```bash
  python3 $HOME/.openclaw/workspace/skills/podcast2obsidian/scripts/news_podcasts_daily.py \
    --source 'https://www.xiaoyuzhoufm.com/podcast/<your_podcast_id>'
  ```
- 日常运行走默认模式，不要手改 `tmp/news_podcasts_daily_state.json` 的 `processed_urls`
- 单条补发统一走：
  ```bash
  python3 $HOME/.openclaw/workspace/skills/podcast2obsidian/scripts/news_podcasts_daily.py \
    --only-url '<episode_url>'
  ```
- `--only-url` 只允许补发已配置 newsletter 源里的单集，并绕过 dedupe；这是正式补发入口
- `state.json` 只作为去重状态，不应再被当作人工补记的操作面板；否则容易出现“state 已处理，但实际没发”
- 排障时先看 `tmp/news_podcasts_daily.log` / `tmp/news_podcasts_daily.out.log` 里的最终成功 run，再看 state，不要反过来
- 若需要补发，先用 `--dry-run --only-url '<episode_url>'` 验证命中目标，再决定是否正式执行

## 注意事项

- Drive 模式需要先配置 `gog` CLI 的 Google 认证
- 全集下载时会尝试通过 RSS 获取完整列表；若 RSS 不可用则 fallback 到网页数据 (可能不完整)
- 大文件上传可能需要几分钟
