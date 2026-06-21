# Podcast2Obsidian Project README

## Purpose

为小宇宙 / Apple Podcasts / Pocket Casts / Goodpods（仅已配置 resolver）/ RedCircle 播客下载归档提供项目级入口，统一 profile、wrapper、Drive 增量去重与历史重复清理流程；同时提供 Bilibili / YouTube 视频归档入口，统一经 NotebookLM 总结后保存到 Fast Note。

## Scope

### In
- 通过 `projects/podcast2obsidian/scripts/run_profile.sh` 运行绑定 profile
- 下载单集或全集到 Google Drive / 本地
- 增量前扫描 Google Drive 已有单集
- 按 `episode_id` / `episode_url` 判断是否已下载
- 扫描并清理 Drive 上的历史重复文件夹
- Goodpods 单集链接：当前支持已配置 resolver 的播客；`反派影评` 会从 Goodpods URL hint 映射到 QTFM `200050` 并解析音频直链。注意：这个 QTFM 频道只有 19 期节目，不是 Goodpods 泛用解析能力。
- Bilibili 视频：字幕存在则上传 transcript text source 到 NotebookLM；字幕不存在则下载音频；两者都保存 metadata-rich Markdown 到 Fast Note

### Out
- 不在本地长期保留播客音频（Drive 模式使用临时目录，完成后清理）
- 不在 profile/wrapper 中硬编码新的聊天目标
- 不做永久删除；重复清理仅移动到 Drive 垃圾桶

## Current Status

- 已修复 Podcast2Obsidian 增量逻辑：**不再依赖本地文件判断是否已下载**。
- 当前增量流程会先扫描 `Google Drive > Podcasts/<节目名>/...`。
- 去重键：
  - `episode_id`
  - `episode_url`（会做 URL 归一化，去掉 `utm_source`）
- 已新增历史重复处理入口：
  - `--scan-drive-duplicates`
  - `--dedupe-drive --apply`
- 示例：对某个节目目录执行扫描，确认 **0 重复**。

## Next Steps

- 如后续还有其它播客目录出现历史重复，可直接用 wrapper 的 dedupe 入口处理。
- 若小宇宙 sub-agent 后续固定跑全集订阅，可把扫描/清理前置到更明确的 SOP 或调度脚本里。

## Key Paths & Outputs

### Core paths
- Skill doc: `$HOME/.openclaw/workspace/skills/podcast2obsidian/SKILL.md`
- Main script: `$HOME/.openclaw/workspace/skills/podcast2obsidian/scripts/xiaoyuzhou_dl.py`
- Project root: `$HOME/.openclaw/workspace/projects/podcast2obsidian/`
- Wrapper: `$HOME/.openclaw/workspace/projects/podcast2obsidian/scripts/run_profile.sh`
- Canonical profile config: `$HOME/.openclaw/workspace/projects/podcast2obsidian/profiles/xiaoyuzhou/config.env`
- Legacy compatibility alias: `$HOME/.openclaw/workspace/projects/podcast2obsidian/profiles/podcaster -> xiaoyuzhou`
- NotebookLM profile default for 小宇宙 agent: `NLM_PROFILE="default"`
- Bilibili profile config: `$HOME/.openclaw/workspace/projects/podcast2obsidian/profiles/bilibili/config.env` (default `NLM_PROFILE="secondary"`)
- Bilibili script: `$HOME/.openclaw/workspace/skills/podcast2obsidian/scripts/bilibili_video.py`

### Wrapper usage

```bash
# 正常跑 profile
bash $HOME/.openclaw/workspace/projects/podcast2obsidian/scripts/run_profile.sh \
  --profile xiaoyuzhou

# 只读扫描 Drive 历史重复
bash $HOME/.openclaw/workspace/projects/podcast2obsidian/scripts/run_profile.sh \
  --profile xiaoyuzhou --scan-drive-duplicates

# 清理 Drive 历史重复（移到垃圾桶）
bash $HOME/.openclaw/workspace/projects/podcast2obsidian/scripts/run_profile.sh \
  --profile xiaoyuzhou --dedupe-drive --apply

# 兼容旧入口（仍可用）
bash $HOME/.openclaw/workspace/projects/podcast2obsidian/scripts/run_profile.sh \
  --profile podcaster

# Goodpods 单集：走 xiaoyuzhou profile
bash $HOME/.openclaw/workspace/projects/podcast2obsidian/scripts/run_profile.sh \
  --profile xiaoyuzhou --url 'https://goodpods.com/...'

# Bilibili：有字幕 → NotebookLM text source；无字幕 → 音频 → NotebookLM
bash $HOME/.openclaw/workspace/projects/podcast2obsidian/scripts/run_profile.sh \
  --profile bilibili --url 'https://www.bilibili.com/video/BV...'

# Bilibili 本地验证（不写 Fast Note / NotebookLM）
bash $HOME/.openclaw/workspace/projects/podcast2obsidian/scripts/run_profile.sh \
  --profile bilibili --url 'BV...' --local
```

### Direct script usage

```bash
# 只读扫描
python3 $HOME/.openclaw/workspace/skills/podcast2obsidian/scripts/xiaoyuzhou_dl.py \
  --scan-drive-duplicates '<Podcast Name>'

# 实际清理
python3 $HOME/.openclaw/workspace/skills/podcast2obsidian/scripts/xiaoyuzhou_dl.py \
  --dedupe-drive '<Podcast Name>' --apply
```

### Drive output layout

```text
Podcasts/<节目名>/<单集标题>/
├── <标题>.m4a
├── <单集链接>.txt
└── metadata.json
```

### Cleanup rule
- 默认保留同一 `episode_id` / `episode_url` 下 **最早那份** 文件夹
- 删除较新的重复项
- 删除动作进入 Google Drive 垃圾桶，可恢复
