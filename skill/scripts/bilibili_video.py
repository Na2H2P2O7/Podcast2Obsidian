#!/usr/bin/env python3
"""Bilibili video ingestion for core.

Modes:
- If usable Bilibili subtitles exist, upload the full transcript as a NotebookLM text source, summarize, then save metadata-rich Markdown to Fast Note with the transcript appended.
- If no usable subtitle exists, download full audio with `bili audio --no-split`, summarize in NotebookLM, then save the same metadata shell to Fast Note.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlparse, parse_qs

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import xiaoyuzhou_dl as core  # noqa: E402

BILI_UA = os.environ.get(
    'BILIBILI_UA',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/124.0 Safari/537.36',
)
DEFAULT_FAST_NOTE_ROOT = os.environ.get('BILIBILI_FAST_NOTE_ROOT', 'Video')


def run_curl_json(url: str, referer: str = 'https://www.bilibili.com/') -> dict[str, Any]:
    cmd = [
        'curl', '-fsSL',
        '--compressed',
        '-A', BILI_UA,
        '-e', referer,
        url,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, errors='replace', timeout=40)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or res.stdout.strip() or f'curl failed: {url}')
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f'JSON 解析失败: {url}: {exc}') from exc


def normalize_video_url(target: str) -> str:
    bvid = extract_bvid(target)
    if not bvid:
        return target
    page = extract_page_index(target)
    suffix = f'?p={page}' if page > 1 else ''
    return f'https://www.bilibili.com/video/{bvid}/{suffix}'


def extract_bvid(target: str) -> str:
    text = (target or '').strip()
    m = re.search(r'(BV[0-9A-Za-z]+)', text)
    return m.group(1) if m else ''


def extract_page_index(target: str) -> int:
    try:
        parsed = urlparse(target)
        qs = parse_qs(parsed.query)
        page = int((qs.get('p') or ['1'])[0] or '1')
        return page if page > 0 else 1
    except Exception:
        return 1


def format_date_from_ts(ts: Any) -> str:
    try:
        value = int(ts or 0)
    except Exception:
        return ''
    if value <= 0:
        return ''
    return datetime.fromtimestamp(value).strftime('%Y-%m-%d')


def fetch_video_meta(target: str) -> dict[str, Any]:
    bvid = extract_bvid(target)
    if not bvid:
        raise ValueError('无法解析 Bilibili BV 号')
    url = f'https://api.bilibili.com/x/web-interface/view?bvid={quote(bvid)}'
    payload = run_curl_json(url, referer=normalize_video_url(target))
    if payload.get('code') != 0:
        raise RuntimeError(payload.get('message') or '无法获取视频信息')
    data = payload.get('data') or {}
    pages = data.get('pages') if isinstance(data.get('pages'), list) else []
    return {
        'bvid': bvid,
        'aid': str(data.get('aid') or ''),
        'title': str(data.get('title') or '').strip(),
        'author': str((data.get('owner') or {}).get('name') or '').strip(),
        'author_mid': str((data.get('owner') or {}).get('mid') or '').strip(),
        'description': str(data.get('desc') or '').strip(),
        'pub_date': format_date_from_ts(data.get('pubdate')),
        'duration': int(data.get('duration') or 0),
        'cover_url': str(data.get('pic') or '').strip(),
        'default_cid': str(data.get('cid') or ''),
        'pages': [
            {
                'cid': str(item.get('cid') or ''),
                'page': int(item.get('page') or idx + 1),
                'part': str(item.get('part') or '').strip(),
                'duration': int(item.get('duration') or 0),
            }
            for idx, item in enumerate(pages)
        ],
    }


def pick_page(meta: dict[str, Any], requested_page: int) -> dict[str, Any]:
    pages = meta.get('pages') or []
    if pages:
        for item in pages:
            if int(item.get('page') or 0) == requested_page:
                return item
        idx = requested_page - 1
        if 0 <= idx < len(pages):
            return pages[idx]
        return pages[0]
    return {
        'cid': meta.get('default_cid') or '',
        'page': 1,
        'part': '',
        'duration': meta.get('duration') or 0,
    }


def normalize_subtitle_url(url: str) -> str:
    text = (url or '').strip()
    if not text:
        return ''
    if text.startswith('//'):
        return 'https:' + text
    if text.startswith('http://') or text.startswith('https://'):
        return text
    return 'https://' + text.lstrip('/')


def normalize_chapter_time(value: Any) -> float:
    try:
        num = float(value)
    except Exception:
        return 0.0
    if num < 0:
        return 0.0
    return num / 1000.0 if num > 60 * 60 * 24 else num


def normalize_chapters(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chapters = []
    seen = set()
    for item in raw or []:
        title = str(item.get('content') or item.get('title') or item.get('label') or '').strip()
        if not title:
            continue
        start = normalize_chapter_time(item.get('from', item.get('start', item.get('start_time'))))
        end = normalize_chapter_time(item.get('to', item.get('end', item.get('end_time'))))
        key = (round(start, 1), title.lower())
        if key in seen:
            continue
        seen.add(key)
        chapters.append({'title': title, 'from': start, 'to': end})
    return sorted(chapters, key=lambda item: item['from'])


def subtitle_priority(item: dict[str, Any]) -> tuple[int, str, str]:
    lan = str(item.get('lan') or '').lower()
    label = str(item.get('lan_doc') or item.get('lanDoc') or '').lower()
    if lan in {'zh-cn', 'zh-hans'}:
        p = 0
    elif lan == 'zh':
        p = 1
    elif 'zh' in lan or '中文' in label:
        p = 2
    elif lan in {'en', 'en-us', 'en-gb'}:
        p = 10
    elif 'en' in lan or '英文' in label or '英语' in label or 'english' in label:
        p = 11
    else:
        p = 50
    return (p, label, lan)


def fetch_subtitle_bundle(meta: dict[str, Any], page: dict[str, Any], video_url: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    bvid = meta.get('bvid') or ''
    aid = meta.get('aid') or ''
    cid = page.get('cid') or meta.get('default_cid') or ''
    urls = []
    if aid:
        urls.append(
            'https://api.bilibili.com/x/player/wbi/v2'
            f'?aid={quote(str(aid))}&cid={quote(str(cid))}&bvid={quote(str(bvid))}'
        )
    urls.append(
        'https://api.bilibili.com/x/player/v2'
        f'?bvid={quote(str(bvid))}&cid={quote(str(cid))}&aid={quote(str(aid))}'
    )

    last_error: Optional[Exception] = None
    for idx, url in enumerate(urls):
        try:
            payload = run_curl_json(url, referer=video_url)
            if payload.get('code') != 0:
                raise RuntimeError(payload.get('message') or '无法获取字幕列表')
            data = payload.get('data') or {}
            chapters = normalize_chapters(data.get('view_points') or [])
            raw_subs = ((data.get('subtitle') or {}).get('subtitles') or [])
            tracks = []
            for item in raw_subs:
                sub_url = normalize_subtitle_url(item.get('subtitle_url') or '')
                if not sub_url:
                    continue
                tracks.append({
                    'id': '' if item.get('id') is None else str(item.get('id')),
                    'lan': item.get('lan') or '',
                    'lan_doc': item.get('lan_doc') or '',
                    'subtitle_url': sub_url,
                    'source': 'player-wbi-v2' if idx == 0 and aid else 'player-v2',
                })
            tracks.sort(key=subtitle_priority)
            return tracks, chapters
        except Exception as exc:
            last_error = exc
            continue
    if last_error:
        raise last_error
    return [], []


def clean_subtitle_items(body: Any) -> list[dict[str, Any]]:
    if not isinstance(body, list):
        return []
    cleaned = []
    for item in body:
        text = str(item.get('content') or '').strip()
        if not text:
            continue
        try:
            start = float(item.get('from') or 0)
        except Exception:
            start = 0.0
        try:
            end = float(item.get('to') or start)
        except Exception:
            end = start
        cleaned.append({'from': start, 'to': end, 'content': text})
    return cleaned


def fetch_subtitle_body(track: dict[str, Any], video_url: str) -> list[dict[str, Any]]:
    if track.get('body') is not None:
        return clean_subtitle_items(track.get('body'))
    payload = run_curl_json(track['subtitle_url'], referer=video_url)
    body = payload.get('body') if isinstance(payload, dict) else None
    return clean_subtitle_items(body)


def parse_json_from_mixed_output(text: str) -> dict[str, Any]:
    raw = text or ''
    start = raw.find('{')
    end = raw.rfind('}')
    if start < 0 or end < start:
        raise RuntimeError('bili CLI 未返回 JSON')
    return json.loads(raw[start:end + 1])


def fetch_cli_subtitle_track(target: str) -> Optional[dict[str, Any]]:
    bili = shutil.which('bili')
    if not bili:
        return None
    cmd = [bili, 'video', target, '--subtitle', '--json']
    res = subprocess.run(cmd, capture_output=True, text=True, errors='replace', timeout=120)
    mixed = (res.stdout or '') + '\n' + (res.stderr or '')
    if res.returncode != 0:
        return None
    try:
        payload = parse_json_from_mixed_output(mixed)
    except Exception:
        return None
    data = payload.get('data') or {}
    subtitle = data.get('subtitle') or {}
    if not subtitle.get('available'):
        return None
    items = subtitle.get('items') or []
    body = clean_subtitle_items(items)
    if not body:
        text = str(subtitle.get('text') or '').strip()
        if text:
            body = [{'from': 0.0, 'to': 0.0, 'content': line.strip()} for line in text.splitlines() if line.strip()]
    if not body:
        return None
    return {
        'id': 'bili-cli',
        'lan': str(subtitle.get('language') or subtitle.get('lan') or 'unknown'),
        'lan_doc': str(subtitle.get('language_doc') or subtitle.get('lan_doc') or 'bili-cli'),
        'subtitle_url': '',
        'source': 'bili-cli',
        'body': body,
    }


def validate_subtitle_body(body: list[dict[str, Any]], duration: int) -> tuple[bool, str]:
    if not body:
        return False, 'empty'
    max_to = max(float(item.get('to') or item.get('from') or 0) for item in body)
    if duration > 0:
        upper_tolerance = max(12, duration * 0.15)
        if max_to > duration + upper_tolerance:
            return False, 'too_long'
        min_ratio = 0.0
        if duration >= 600:
            min_ratio = 0.18
        elif duration >= 300:
            min_ratio = 0.22
        elif duration >= 180:
            min_ratio = 0.25
        if min_ratio and max_to < duration * min_ratio:
            return False, 'too_short'
    return True, 'ok'


def format_timestamp(seconds: Any, with_hours: bool = False) -> str:
    try:
        total = max(0, int(float(seconds)))
    except Exception:
        total = 0
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if with_hours or h:
        return f'{h:02d}:{m:02d}:{s:02d}'
    return f'{m:02d}:{s:02d}'


def yaml_quote(value: str) -> str:
    return "'" + (value or '').replace("'", "''") + "'"


def build_chapter_lines(chapters: list[dict[str, Any]], with_hours: bool = False) -> list[str]:
    lines = []
    for chapter in chapters or []:
        title = (chapter.get('title') or '').strip()
        if not title:
            continue
        lines.append(f'- `{format_timestamp(chapter.get("from"), with_hours)}` {title}')
    return lines


def transcript_plain_text(body: list[dict[str, Any]]) -> str:
    return '\n'.join(str(item.get('content') or '').strip() for item in body or [] if str(item.get('content') or '').strip()).strip()


def transcript_with_timestamps(body: list[dict[str, Any]], duration: int = 0) -> str:
    with_hours = duration >= 3600 or any(float(item.get('from') or 0) >= 3600 for item in body or [])
    lines = []
    for item in body or []:
        text = str(item.get('content') or '').strip()
        if text:
            lines.append(f'[{format_timestamp(item.get("from"), with_hours)}] {text}')
    return '\n'.join(lines).strip()


def build_bilibili_prompt(chapters: list[dict[str, Any]]) -> str:
    chapter_titles = [(c.get('title') or '').strip() for c in chapters or [] if (c.get('title') or '').strip()]
    if chapter_titles:
        lines = [
            '请根据这个 Bilibili 视频内容进行结构化总结。以我提供的章节作为输出骨架：章节标题输出为二级标题，按原顺序展开；不要保留时间戳；不要照抄提纲，而是根据视频内容充实总结。不要输出导言、结语、编者按或“以下是总结”等套话，直接从第一个二级标题开始。',
            '章节结构如下：',
        ]
        for title in chapter_titles:
            lines.append(f'## {title}')
        return '\n'.join(lines)
    return '请根据这个 Bilibili 视频内容进行结构化总结，包含二级标题，按原顺序输出，必要时使用三级标题，三级标题下请写简洁要点，避免过长整段。不要写导言、结语、编者按或“以下是总结”等套话，直接从第一个二级标题开始。'


def build_bilibili_source_text(meta: dict[str, Any], page: dict[str, Any], video_url: str, body: list[dict[str, Any]], chapters: list[dict[str, Any]]) -> str:
    parts = [
        f"标题：{meta.get('title') or ''}",
        f"UP主：{meta.get('author') or ''}",
        f"URL：{video_url}",
    ]
    if meta.get('description'):
        parts.append('简介：\n' + str(meta.get('description') or '').strip())
    chapter_lines = build_chapter_lines(chapters, int(page.get('duration') or meta.get('duration') or 0) >= 3600)
    if chapter_lines:
        parts.append('章节：\n' + '\n'.join(chapter_lines))
    transcript = transcript_with_timestamps(body, int(page.get('duration') or meta.get('duration') or 0))
    if transcript:
        parts.append('字幕全文：\n' + transcript)
    return '\n\n'.join(part.strip() for part in parts if part and part.strip())


def build_bilibili_markdown(
    meta: dict[str, Any],
    page: dict[str, Any],
    video_url: str,
    *,
    subtitle_available: bool,
    answer: str = '',
    summary: str = '',
    transcript_body: Optional[list[dict[str, Any]]] = None,
    chapters: Optional[list[dict[str, Any]]] = None,
) -> str:
    title = meta.get('title') or 'Bilibili Video'
    author = meta.get('author') or 'unknown'
    pub_date = meta.get('pub_date') or ''
    bvid = meta.get('bvid') or ''
    cid = page.get('cid') or ''
    page_no = page.get('page') or 1
    duration = int(page.get('duration') or meta.get('duration') or 0)
    with_hours = duration >= 3600

    parts: list[str] = []
    parts.append('\n'.join([
        '---',
        'tags:',
        '  - Bilibili',
        f'up: {yaml_quote(author)}',
        f'title: {yaml_quote(f"[{title}]({video_url})")}',
        f'bvid: {yaml_quote(bvid)}',
        f'cid: {yaml_quote(cid)}',
        f'subtitle: {"yes" if subtitle_available else "no"}',
        *( [f'Release Date: {pub_date}'] if pub_date else [] ),
        '---',
    ]))

    cover = meta.get('cover_url') or ''
    if cover:
        parts.append(f'![]({cover})')

    parts.append(
        '<iframe src="https://player.bilibili.com/player.html?'
        f'aid={quote(str(meta.get("aid") or ""))}&bvid={quote(str(bvid))}&cid={quote(str(cid))}&page={page_no}&autoplay=0" '
        'scrolling="no" border="0" frameborder="no" framespacing="0" '
        'allow="fullscreen; picture-in-picture" allowfullscreen="true" '
        'style="height:100%;width:100%; aspect-ratio: 16 / 9;"> </iframe>'
    )

    desc = (meta.get('description') or '').strip()
    if desc:
        parts.append('## 简介\n\n' + desc)

    chapter_lines = build_chapter_lines(chapters or [], with_hours)
    if chapter_lines:
        parts.append('## 章节\n\n' + '\n'.join(chapter_lines))

    summary = (summary or '').strip()
    if summary:
        parts.append('> ' + summary)

    clean = core.clean_notebooklm_answer(answer).strip() if answer else ''
    if clean:
        parts.append(clean)

    transcript = transcript_plain_text(transcript_body or [])
    if transcript:
        parts.append('## 字幕全文\n\n' + transcript)

    return '\n\n'.join(part.strip() for part in parts if part and part.strip()) + '\n'


def note_title_for_video(meta: dict[str, Any], page: dict[str, Any]) -> str:
    title = meta.get('title') or 'Bilibili Video'
    if len(meta.get('pages') or []) > 1 and page.get('part'):
        title = f'{title} - P{page.get("page") or 1} {page.get("part")}'
    author = (meta.get('author') or '').strip()
    if author:
        title = f'{author} {title}'
    if meta.get('pub_date'):
        title = f'{meta.get("pub_date")} {title}'
    return core.normalize_markdown_note_title(title)


def save_subtitle_markdown(meta: dict[str, Any], page: dict[str, Any], markdown: str) -> dict[str, Any]:
    author = core.normalize_fast_note_segment(meta.get('author') or 'unknown')
    folder_path = f'{DEFAULT_FAST_NOTE_ROOT}/{author}'
    note_title = note_title_for_video(meta, page)
    result = core.upsert_fast_note_markdown(folder_path, note_title, markdown)
    result['clean_answer'] = markdown
    return result


def write_local_artifacts(out_dir: Path, meta: dict[str, Any], page: dict[str, Any], markdown: str, subtitle_body: list[dict[str, Any]] | None = None) -> Path:
    author = core.sanitize_filename(meta.get('author') or 'unknown')
    title = core.sanitize_filename(note_title_for_video(meta, page))
    target_dir = out_dir / 'Video' / author / title
    target_dir.mkdir(parents=True, exist_ok=True)
    md_path = target_dir / f'{title}.md'
    md_path.write_text(markdown, encoding='utf-8')
    (target_dir / 'metadata.json').write_text(json.dumps({'meta': meta, 'page': page}, ensure_ascii=False, indent=2), encoding='utf-8')
    if subtitle_body is not None:
        (target_dir / 'subtitle.json').write_text(json.dumps(subtitle_body, ensure_ascii=False, indent=2), encoding='utf-8')
    return md_path


def find_audio_file(root: Path) -> Path:
    candidates = []
    for ext in ('*.m4a', '*.mp3', '*.aac', '*.wav', '*.flac'):
        candidates.extend(root.rglob(ext))
    if not candidates:
        raise RuntimeError(f'bili audio 未产生音频文件: {root}')
    candidates.sort(key=lambda p: p.stat().st_size if p.exists() else 0, reverse=True)
    return candidates[0]


def download_audio_with_bili(target: str, out_dir: Path) -> Path:
    bili = shutil.which('bili')
    if not bili:
        raise RuntimeError('未找到 bili CLI')
    cmd = [bili, 'audio', target, '--no-split', '-o', str(out_dir)]
    print('🎧 下载 Bilibili 音频: ' + ' '.join(cmd), flush=True)
    res = subprocess.run(cmd, text=True, errors='replace')
    if res.returncode != 0:
        raise RuntimeError(f'bili audio 失败: exit={res.returncode}')
    audio = find_audio_file(out_dir)
    print(f'✅ 音频文件: {audio}')
    return audio


def podcast_name_for_bilibili(meta: dict[str, Any]) -> str:
    author = (meta.get('author') or '').strip()
    return f'Bilibili - {author}' if author else 'Bilibili'


def notebook_title_for_bilibili(meta: dict[str, Any], page: dict[str, Any]) -> str:
    title = note_title_for_video(meta, page)
    return core.build_episode_notebook_title(podcast_name_for_bilibili(meta), title, meta.get('pub_date') or '')


def build_note_context(meta: dict[str, Any], page: dict[str, Any], video_url: str, chapters: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        'podcast_name': podcast_name_for_bilibili(meta),
        'episode_id': f'{meta.get("bvid")}_p{page.get("page") or 1}',
        'episode_url': video_url,
        'pub_date': meta.get('pub_date') or '',
        'duration': page.get('duration') or meta.get('duration') or 0,
        'description': meta.get('description') or '',
        'shownotes': meta.get('description') or '',
        'episode_cover_url': meta.get('cover_url') or '',
        'bvid': meta.get('bvid') or '',
        'cid': page.get('cid') or '',
        'page': page.get('page') or 1,
        'author': meta.get('author') or '',
        'chapters': chapters or [],
    }



def ensure_bilibili_nlm_profile() -> None:
    os.environ['NLM_PROFILE'] = (os.environ.get('NLM_PROFILE') or 'secondary').strip() or 'secondary'


def create_or_reuse_notebook(notebook_title: str) -> Optional[str]:
    return core.create_or_reuse_notebook(notebook_title)


def source_title_for_bilibili(meta: dict[str, Any], page: dict[str, Any], suffix: str) -> str:
    return f'{note_title_for_video(meta, page)} - {suffix}'


def source_item_title(item: dict[str, Any]) -> str:
    for key in ('title', 'name', 'display_name'):
        value = item.get(key)
        if value:
            return str(value)
    return json.dumps(item, ensure_ascii=False)


def find_source_id_by_title(notebook_id: str, title: str) -> str:
    try:
        sources = core.list_notebook_sources(notebook_id)
    except Exception:
        return ''
    for item in sources or []:
        if source_item_title(item).strip() == title:
            extractor = getattr(core, '_extract_source_id_from_item', None)
            sid = extractor(item) if extractor else ''
            if sid:
                return sid
    return ''


def add_text_source_to_notebook(notebook_id: str, title: str, text: str) -> Optional[str]:
    existing = find_source_id_by_title(notebook_id, title)
    if existing:
        print(f'♻️ 命中已有 transcript text source: {existing}')
        return existing
    print(f'📄 上传 transcript text source: {title}')
    res = core.run_nlm_command(['source', 'add', notebook_id, '--text', text, '--title', title, '--wait'])
    if res.returncode != 0:
        print(f"❌ transcript text source 上传失败: {res.stderr.strip() or res.stdout.strip()}")
        return None
    sid = core.parse_source_id((res.stdout or '') + '\n' + (res.stderr or ''))
    if sid:
        print(f'✅ transcript source 已创建: {sid}')
    else:
        print('✅ transcript source 已上传（未解析到 source id）')
    return sid or ''


def save_bilibili_summary_to_fast_note(
    meta: dict[str, Any],
    page: dict[str, Any],
    video_url: str,
    *,
    subtitle_available: bool,
    answer: str,
    summary: str = '',
    transcript_body: Optional[list[dict[str, Any]]] = None,
    chapters: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    author = core.normalize_fast_note_segment(meta.get('author') or 'unknown')
    folder_path = f'{DEFAULT_FAST_NOTE_ROOT}/{author}'
    markdown = build_bilibili_markdown(
        meta,
        page,
        video_url,
        subtitle_available=subtitle_available,
        answer=answer,
        summary=summary,
        transcript_body=transcript_body,
        chapters=chapters,
    )
    result = core.upsert_fast_note_markdown(folder_path, note_title_for_video(meta, page), markdown)
    result['clean_answer'] = markdown
    return result


def run_bilibili_notebooklm_summary(
    notebook_id: str,
    prompt: str,
    meta: dict[str, Any],
    page: dict[str, Any],
    video_url: str,
    *,
    subtitle_available: bool,
    source_id: Optional[str] = None,
    transcript_body: Optional[list[dict[str, Any]]] = None,
    chapters: Optional[list[dict[str, Any]]] = None,
    no_infographic: bool = False,
) -> dict[str, Any]:
    result = {'ok': False, 'query_ok': False, 'infographic_ok': True, 'fast_note': None, 'answer': '', 'summary': ''}
    if not core.ensure_nlm_auth():
        result['infographic_ok'] = False
        return result

    print('🧠 配置 NotebookLM 回答长度: longer')
    cfg_res = core.run_nlm_command(['chat', 'configure', notebook_id, '--goal', 'default', '--response-length', 'longer'])
    if cfg_res.returncode != 0:
        print(f"⚠️ 配置回答长度失败: {cfg_res.stderr.strip() or cfg_res.stdout.strip()}")

    print(f'💬 触发 Bilibili 总结: {prompt.splitlines()[0]}')
    try:
        answer = core.run_notebook_query_with_recovery(notebook_id, prompt, audio_source_id=source_id or None, enforce_complete=True)
        result['answer'] = answer
        result['query_ok'] = True
        print('✅ 已拿到 NotebookLM 总结文本')
        try:
            summary_text = core.run_notebook_query_with_recovery(
                notebook_id,
                '请用一段 120-180 字的中文概括这个视频的核心内容，直接输出摘要正文，不要标题，不要项目符号。',
                audio_source_id=source_id or None,
                enforce_complete=False,
            )
            result['summary'] = core.clean_notebooklm_answer(summary_text).strip()
        except Exception as e:
            print(f'⚠️ 摘要生成失败（继续）: {e}')
        fast_note = save_bilibili_summary_to_fast_note(
            meta,
            page,
            video_url,
            subtitle_available=subtitle_available,
            answer=answer,
            summary=result['summary'],
            transcript_body=transcript_body,
            chapters=chapters,
        )
        result['fast_note'] = fast_note
        result['ok'] = True
        print(f"📝 已保存到 Fast Note: {fast_note['note_path']} (note_id={fast_note['note_id']})")
    except Exception as e:
        print(f'⚠️ Bilibili NotebookLM 总结失败: {e}')

    if not no_infographic:
        result['infographic_ok'] = core.create_default_infographics(notebook_id, result.get('fast_note'))
    return result


def upload_transcript_to_notebooklm(
    meta: dict[str, Any],
    page: dict[str, Any],
    video_url: str,
    body: list[dict[str, Any]],
    chapters: list[dict[str, Any]],
    *,
    no_infographic: bool = False,
) -> dict[str, Any]:
    ensure_bilibili_nlm_profile()
    notebook_id = create_or_reuse_notebook(notebook_title_for_bilibili(meta, page))
    result = {'ok': False, 'notebook_id': notebook_id, 'status': 'failed', 'source_id': None, 'summary': None, 'reason': ''}
    if not notebook_id:
        result['reason'] = 'notebook_create_failed'
        return result
    source_text = build_bilibili_source_text(meta, page, video_url, body, chapters)
    source_title = source_title_for_bilibili(meta, page, 'Transcript')
    source_id = add_text_source_to_notebook(notebook_id, source_title, source_text)
    result['source_id'] = source_id
    if source_id is None:
        result['reason'] = 'text_source_add_failed'
        return result
    prompt = build_bilibili_prompt(chapters)
    summary = run_bilibili_notebooklm_summary(
        notebook_id,
        prompt,
        meta,
        page,
        video_url,
        subtitle_available=True,
        source_id=source_id or None,
        transcript_body=body,
        chapters=chapters,
        no_infographic=no_infographic,
    )
    result['summary'] = summary
    result['ok'] = bool(summary.get('ok'))
    result['status'] = 'success' if result['ok'] else 'partial'
    return result

def ingest(target: str, *, local: bool = False, force_audio: bool = False, no_notebooklm: bool = False, no_infographic: bool = False, keep_temp: bool = False) -> str:
    video_url = normalize_video_url(target)
    requested_page = extract_page_index(target)
    print('📺 Bilibili → core')
    print(f'   URL: {video_url}')
    print('🔍 获取视频信息...')
    meta = fetch_video_meta(target)
    page = pick_page(meta, requested_page)
    if not page.get('cid'):
        raise RuntimeError('无法解析当前视频 CID')

    title = meta.get('title') or 'unknown_video'
    if len(meta.get('pages') or []) > 1 and page.get('part'):
        title_for_display = f'{title} / P{page.get("page")} {page.get("part")}'
    else:
        title_for_display = title
    print(f'   📻 节目: {podcast_name_for_bilibili(meta)}')
    print(f'   📝 标题: {title_for_display}')
    print(f'   👤 UP: {meta.get("author") or "unknown"}')
    print(f'   ⏱️  时长: {core.format_duration(page.get("duration") or meta.get("duration") or 0)}')
    print()

    tracks: list[dict[str, Any]] = []
    chapters: list[dict[str, Any]] = []
    if not force_audio:
        print('🔎 检查字幕轨...')
        tracks, chapters = fetch_subtitle_bundle(meta, page, video_url)
        if not tracks:
            cli_track = fetch_cli_subtitle_track(target)
            if cli_track:
                tracks = [cli_track]
        print(f'   字幕轨: {len(tracks)}')
    else:
        try:
            _, chapters = fetch_subtitle_bundle(meta, page, video_url)
        except Exception:
            chapters = []

    if tracks and not force_audio:
        last_reason = ''
        for track in tracks:
            label = track.get('lan_doc') or track.get('lan') or 'unknown'
            print(f'📝 尝试字幕: {label}')
            body = fetch_subtitle_body(track, video_url)
            ok, reason = validate_subtitle_body(body, int(page.get('duration') or meta.get('duration') or 0))
            if not ok:
                print(f'   ⚠️ 跳过该字幕: {reason}')
                last_reason = reason
                continue
            preview_markdown = build_bilibili_markdown(
                meta,
                page,
                video_url,
                subtitle_available=True,
                answer='',
                transcript_body=body,
                chapters=chapters,
            )
            if local:
                out = write_local_artifacts(Path.home() / 'Downloads', meta, page, preview_markdown, body)
                print(f'🎉 已保存本地 Markdown 预览: {out}')
                print(f'📣 RESULT status=success mode=subtitle-local notebook_id=none note_id=none note_path={out} bvid={meta.get("bvid")} cid={page.get("cid")} subtitle=yes')
                return 'subtitle_local'
            if no_notebooklm:
                print('⏭️ 已设置 --no-notebooklm：不上传 NotebookLM')
                print(f'📣 RESULT status=skipped mode=subtitle notebook_id=none note_id=none note_path=none reason=no_notebooklm bvid={meta.get("bvid")} cid={page.get("cid")} subtitle=yes')
                return 'subtitle_skipped'
            notebook_result = upload_transcript_to_notebooklm(meta, page, video_url, body, chapters, no_infographic=no_infographic)
            fn = (notebook_result.get('summary') or {}).get('fast_note') if notebook_result.get('summary') else None
            notebook_id = notebook_result.get('notebook_id') or 'none'
            if notebook_result.get('ok') and fn:
                print(f'📣 RESULT status=success mode=subtitle notebook_id={notebook_id} note_id={fn["note_id"]} note_path={fn["note_path"]} query_ok=1 fast_note_ok=1 infographic_ok={1 if (notebook_result.get("summary") or {}).get("infographic_ok") else 0} bvid={meta.get("bvid")} cid={page.get("cid")} subtitle=yes')
                return 'subtitle_notebooklm'
            reason = notebook_result.get('reason') or 'query_or_fast_note_incomplete'
            print(f'📣 RESULT status=partial mode=subtitle notebook_id={notebook_id} note_id=none note_path=none query_ok={1 if (notebook_result.get("summary") or {}).get("query_ok") else 0} fast_note_ok={1 if fn else 0} infographic_ok={1 if (notebook_result.get("summary") or {}).get("infographic_ok") else 0} reason={json.dumps(reason, ensure_ascii=False)} bvid={meta.get("bvid")} cid={page.get("cid")} subtitle=yes')
            return False
        print(f'⚠️ 找到字幕轨但均不可用: {last_reason or "unknown"}，转入音频模式')
    else:
        print('ℹ️ 未发现可用字幕轨，转入音频模式' if not force_audio else '🎧 已指定 force-audio，跳过字幕模式')

    if no_notebooklm:
        print('⏭️ 已设置 --no-notebooklm：不上传 NotebookLM')
        print(f'📣 RESULT status=skipped mode=audio notebook_id=none note_id=none note_path=none reason=no_notebooklm bvid={meta.get("bvid")} cid={page.get("cid")}')
        return 'audio_skipped'

    tmp_obj = tempfile.TemporaryDirectory(prefix='core-bilibili-')
    tmpdir = Path(tmp_obj.name)
    try:
        audio_path = download_audio_with_bili(target, tmpdir)
        if local:
            author = core.sanitize_filename(meta.get('author') or 'unknown')
            title_safe = core.sanitize_filename(note_title_for_video(meta, page))
            dest_dir = Path.home() / 'Downloads' / 'Video' / author / title_safe
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_audio = dest_dir / audio_path.name
            shutil.copy2(audio_path, dest_audio)
            (dest_dir / 'metadata.json').write_text(json.dumps({'meta': meta, 'page': page}, ensure_ascii=False, indent=2), encoding='utf-8')
            print(f'🎉 已保存本地音频: {dest_audio}')
            print(f'📣 RESULT status=success mode=audio-local notebook_id=none note_id=none note_path=none audio_path={dest_audio} bvid={meta.get("bvid")} cid={page.get("cid")}')
            return 'audio_local'

        ensure_bilibili_nlm_profile()
        print('📒 上传到 NotebookLM (Bilibili 音频)...')
        notebook_id = create_or_reuse_notebook(notebook_title_for_bilibili(meta, page))
        if not notebook_id:
            raise RuntimeError('NotebookLM notebook 创建失败')
        upload_result = core.guarded_add_audio_source(notebook_id, str(audio_path), max_wait_seconds=core.NLM_AUDIO_UPLOAD_MAX_WAIT_SECONDS)
        audio_source_id = upload_result.get('audio_source_id')
        if upload_result.get('status') != 'confirmed':
            reason = upload_result.get('reason') or 'notebooklm_audio_source_unconfirmed'
            print(f'📣 RESULT status=partial mode=audio notebook_id={notebook_id} note_id=none note_path=none reason={json.dumps(reason, ensure_ascii=False)} bvid={meta.get("bvid")} cid={page.get("cid")} subtitle=no')
            return False
        if audio_source_id:
            print(f'✅ 音频 source 已创建: {audio_source_id}')
        summary_result = run_bilibili_notebooklm_summary(
            notebook_id,
            build_bilibili_prompt(chapters),
            meta,
            page,
            video_url,
            subtitle_available=False,
            source_id=audio_source_id,
            transcript_body=None,
            chapters=chapters,
            no_infographic=no_infographic,
        )
        fn = summary_result.get('fast_note')
        if summary_result.get('ok') and fn:
            print(f'📣 RESULT status=success mode=audio notebook_id={notebook_id} note_id={fn["note_id"]} note_path={fn["note_path"]} query_ok=1 fast_note_ok=1 infographic_ok={1 if summary_result.get("infographic_ok") else 0} bvid={meta.get("bvid")} cid={page.get("cid")} subtitle=no')
            return 'audio_notebooklm'
        print(f'📣 RESULT status=partial mode=audio notebook_id={notebook_id} note_id=none note_path=none query_ok={1 if summary_result.get("query_ok") else 0} fast_note_ok={1 if fn else 0} infographic_ok={1 if summary_result.get("infographic_ok") else 0} reason="query_or_fast_note_incomplete" bvid={meta.get("bvid")} cid={page.get("cid")} subtitle=no')
        return False
    finally:
        if keep_temp:
            print(f'🧪 保留临时目录: {tmpdir}')
        else:
            tmp_obj.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description='Bilibili video → Fast Note / NotebookLM for core')
    parser.add_argument('target', help='Bilibili video URL or BV id')
    parser.add_argument('--local', action='store_true', help='Save artifacts locally under ~/Downloads; no Fast Note/NotebookLM writes')
    parser.add_argument('--force-audio', action='store_true', help='Skip subtitle mode and force audio → NotebookLM path')
    parser.add_argument('--no-notebooklm', action='store_true', help='In audio mode, skip NotebookLM upload')
    parser.add_argument('--no-infographic', action='store_true', help='Pass through to NotebookLM summary path')
    parser.add_argument('--keep-temp', action='store_true', help='Keep temporary audio download directory for debugging')
    args = parser.parse_args()

    try:
        ingest(
            args.target,
            local=args.local,
            force_audio=args.force_audio,
            no_notebooklm=args.no_notebooklm,
            no_infographic=args.no_infographic,
            keep_temp=args.keep_temp,
        )
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        print(f'❌ 错误: {exc}')
        sys.exit(1)


if __name__ == '__main__':
    main()
