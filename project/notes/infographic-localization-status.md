# Xiaoyuzhou / NotebookLM Infographic 本地化插入状态

更新时间：2026-05-20

## 目标

把 NotebookLM 生成的 infographic 自动插入到 Fast Note markdown，作为 `xiaoyuzhou_dl.py` 的后台流程一部分。

当前策略：
- 音频 source 上传确认后，立即触发 1 个 NotebookLM infographic；
- 不等待 infographic，继续执行原有 timeline / NotebookLM query / Markdown 保存逻辑；
- Markdown 保存完成后，再检查或等待 infographic artifact；
- 下载 PNG 后写入 Fast Note 附件库，并把 embed 插到开头引用综述下方、`## Shownotes` 上方；
- 日常入口仍是 dispatcher -> `projects/podcast2obsidian/scripts/run_profile.sh`，不需要 agent/sub-agent 介入。

---

## Fast Note 附件结构

以 `42章经 - 我们是如何定义 OpenClaw for Teams 新产品形态的｜对谈 Kuse&Junior 联创兼 CTO 宇豪`（note_id=5972）为参考：

- markdown 中的写法：
  - `![[Pasted image 20260426013123.png]]`
- 附件 DB：
  - `/opt/fast-note/storage/database/db_user_file_1.sqlite3`
- 附件记录：
  - `path = attachments/Pasted image 20260426013123.png`
  - `id = 9803`
- 实体文件：
  - `/opt/fast-note/storage/vault/u_1/file/f_9803/file.dat`

结论：
- Fast Note 不是把图片放在 note 同目录下；
- markdown 只保存 `![[filename]]`；
- 二进制文件实际保存在 `vault/u_1/file/f_<file_id>/file.dat`；
- `db_user_file_1.sqlite3` 负责逻辑路径 `attachments/<filename>` 与实体文件之间的映射。

---

## 2026-05-20 实测结果

测试 notebook：
- `4358ef4e-613b-4833-bf77-e3e27d1c1019`
- `AI智识录 - 北大光华院长对谈Kimi总裁张予彤：AI时代的边界探索与人才机遇`
- profile: `default` / `you@example.com`

结果：
- `studio status` 正常返回 completed infographic artifact：
  - `3dadb5a2-37a0-484f-a9cd-93e03720b01f`
- `download infographic` 成功下载 PNG：
  - `2752 x 1536`
  - 约 `5.3M`
- 已手工写入目标 Fast Note 笔记作为验证：
  - note_id: `7397`
  - attachment file_id: `10522`
  - DB path: `attachments/NotebookLM infographic 4358ef4e.png`
  - 实体文件：`/opt/fast-note/storage/vault/u_1/file/f_10522/file.dat`

---

## 当前代码能力

`skills/podcast2obsidian/scripts/xiaoyuzhou_dl.py` 现在包含：

1. **提前触发能力**
   - `trigger_default_infographics(...)`
   - 在音频 source 上传确认后立即触发 infographic create，不等待生成。

2. **artifact 观测能力**
   - `wait_for_new_infographic_artifact(...)`
   - `latest_completed_infographic_artifact(...)`
   - 优先通过 `studio status` 找 completed infographic artifact。

3. **下载与写入能力**
   - `poll_download_infographic(...)`
   - `save_fast_note_attachment(...)`
   - `attach_infographic_to_fast_note(...)`

4. **Markdown 插入能力**
   - `insert_infographic_embed_into_markdown(...)`
   - 优先插入在开头引用综述下方；
   - 兜底插入在 `## Shownotes` 上方。

5. **后台闭环**
   - `create_notebook_and_upload_to_notebooklm(...)`：
     1. 创建/复用 Notebook；
     2. 上传音频 source；
     3. 立即触发 infographic；
     4. 继续原有 query 和 Fast Note 保存；
     5. 保存后 finalize infographic 并写入 Markdown。

---

## 注意事项

- 如果 `studio status` 临时不可用，下载逻辑仍会尝试 `download infographic` 轮询兜底。
- 若 Fast Note 笔记里已经存在同名 embed，会跳过重复写入。
- `--no-infographic` 仍会关闭 infographic 创建和写入。
- 该流程运行在普通后台 wrapper 中，dispatcher 不需要改成 agent 路径。
