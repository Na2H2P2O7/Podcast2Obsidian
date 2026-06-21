#!/usr/bin/env python3
"""
小宇宙播客下载器 → NotebookLM + Google Drive

用法:
    python xiaoyuzhou_dl.py <episode_url>           # 单集下载（默认上传 NotebookLM + Drive）
    python xiaoyuzhou_dl.py <podcast_url>           # 全集下载 (via RSS, 默认仅 Drive)
    python xiaoyuzhou_dl.py <url> --local           # 仅下载到本地

示例:
    python xiaoyuzhou_dl.py https://www.xiaoyuzhoufm.com/episode/697c58562fc7f49d0902395d
    python xiaoyuzhou_dl.py https://www.xiaoyuzhoufm.com/podcast/67e366fa1c465530de1f9d61

功能:
    1. 单集: 从网页提取音频链接、标题、节目名
    2. 全集: 通过 Apple Podcasts 获取 RSS，解析完整单集列表
    3. 创建单集文件夹: 音频 (.m4a)、链接 (.txt)、metadata (.json)
    4. 单集默认新建 NotebookLM notebook 并上传音频
    5. 上传到 Google Drive: Podcasts/<节目名>/<单集标题>/
"""

import sys
import re
import os
import json
import sqlite3
import shutil
import subprocess
import tempfile
import time
import hashlib
import xml.etree.ElementTree as ET
from html import unescape
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import quote, unquote, urlparse, parse_qs
from typing import Optional
from collections import defaultdict

try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except AttributeError:
    pass


PODCAST_ROUTING_FILE = os.environ.get(
    'PODCAST_ROUTING_FILE',
    os.path.expanduser('~/.openclaw/workspace/projects/podcast2obsidian/podcast_routing.json'),
)


def _load_podcast_routing(path: str = PODCAST_ROUTING_FILE) -> dict:
    """Per-podcast routing/behavior overrides, loaded from an external JSON file.

    Keys are podcast display names; values are option dicts. See
    projects/podcast2obsidian/podcast_routing.example.json for the available fields. A missing
    or invalid file resolves to {} so every podcast falls back to default behavior.
    Override the location with the PODCAST_ROUTING_FILE environment variable.
    """
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


PODCAST_ROUTING = _load_podcast_routing()


def normalize_wrapped_label(text: str) -> str:
    """去掉两端对称包裹的横线/分隔符，例如 `-纵横说-` -> `纵横说`。"""
    text = (text or '').strip()
    if not text:
        return text

    wrapped = re.match(r'^[\s\-—–_·•]+(.+?)[\s\-—–_·•]+$', text)
    if wrapped:
        inner = wrapped.group(1).strip()
        if inner:
            return inner
    return text


def _timeline_blacklist_hit(text: str) -> bool:
    markers = [
        'Glossary', 'glossary', '正在研发', '目前在售', '在售平台', '搜索：', '搜索:',
        '提到的播客', '听友群', '商务合作', '加我的微信', '欢迎加我的微信', '开放嘉宾自荐',
        '如果你对', '或者恰巧在', '或者有任何你想要表达的观点',
        '延伸阅读', '关于我们:', '关于我们：',
        '收听平台', '关注我们', '联系我们', '投稿', '问卷', '加入我们', '品牌官网'
    ]
    return any(marker in (text or '') for marker in markers)


def _clean_timeline_text(text: str) -> str:
    text = normalize_shownotes_text(text or '')
    text = re.sub(r'^\*{1,2}\s*', '', text).strip()
    text = re.sub(r'^\[?(\d{1,2}:\d{2}(?::\d{2})?)\]?\s*[—–-]\s*\[?(\d{1,2}:\d{2}(?::\d{2})?)\]?(?!:\d)\s*', '', text).strip()
    text = re.sub(r'^\[?(\d{1,2}:\d{2}(?::\d{2})?)\]?(?!:\d)\s*', '', text).strip()
    text = re.sub(r'^\d{1,2}\s+', '', text)
    text = re.sub(r'^(?:\|\s*)+', '', text)
    text = re.sub(r'^(?:[-—–•·\s]+)', '', text)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = text.replace('**', '')
    if text.startswith('**') and text.endswith('**') and len(text) > 4:
        text = text[2:-2].strip()
    text = re.sub(r'\*{1,2}$', '', text).strip()
    text = re.sub(r'[📖🎙️🔍📚]+$', '', text).strip('，,;；。:： ')
    return text.strip()


def _is_appendix_start_text(text: str) -> bool:
    cleaned = (text or '').strip().strip('：:')
    if not cleaned:
        return False
    markers = [
        '【相关阅读】', '相关阅读', '相关图片', '本期书单', '本期书目', '延伸阅读',
        '嘉宾介绍', '相关链接', '参考资料', '延伸内容', '本期围读', '写在最后',
        '本期提到', '幕后制作', '关于人文清华播客'
    ]
    return cleaned in markers or any(cleaned.startswith(m + '：') or cleaned.startswith(m + ':') for m in markers)


def _blockquote_to_markdown(block: str) -> str:
    parts = []
    text = normalize_shownotes_text(re.sub(r'<ul>.*?</ul>', '', block, flags=re.S | re.I)).strip()
    if text:
        for line in text.splitlines():
            line = line.strip()
            if line:
                parts.append(f'> {line}')

    bullets = []
    for li in re.findall(r'<li[^>]*>(.*?)</li>', block, flags=re.S | re.I):
        bullet = _clean_timeline_text(li)
        if bullet:
            bullets.append(bullet)
    for bullet in bullets:
        parts.append(f'> - {bullet}')

    return '\n'.join(parts).strip()


def _block_is_emphasized_short_heading(block: str) -> bool:
    block = (block or '').strip()
    lower = block.lower()
    if not (lower.startswith('<p') and '</p>' in lower and '<strong' in lower):
        return False
    plain = normalize_shownotes_text(block).strip()
    if not plain or len(plain) > 30:
        return False
    if _is_timeline_label_text(plain) or _is_appendix_start_text(plain):
        return False
    if plain in {'主播', '写在前面', '写在最后', '本期围读'}:
        return False
    if plain.startswith('《') or '》' in plain:
        return False
    if _line_starts_with_timestamp(plain):
        return False
    if re.search(r'[。！？!?：:；;，,（）()\[\]/]', plain):
        return False
    return True


def _parse_chapter_heading(text: str, block: str = '') -> Optional[dict]:
    cleaned = normalize_shownotes_text(text or '').strip()
    if not cleaned:
        return None
    m = re.match(r'^([一二三四五六七八九十百零0-9]+[、.．])\s*(.+?)(?:\s+(\d{1,2}:\d{2}(?::\d{2})?))?$', cleaned)
    if m:
        title = (m.group(1) + ' ' + (m.group(2) or '').strip()).strip()
        if len(title) <= 120:
            return {'time': m.group(3) or '', 'title': title}
    if block and _block_is_emphasized_short_heading(block):
        return {'time': '', 'title': cleaned}
    return None


def _timestamp_to_seconds(ts: str) -> Optional[int]:
    ts = (ts or '').strip()
    if not ts:
        return None
    parts = ts.split(':')
    try:
        if len(parts) == 2:
            mm, ss = map(int, parts)
            return mm * 60 + ss
        if len(parts) == 3:
            hh, mm, ss = map(int, parts)
            return hh * 3600 + mm * 60 + ss
    except ValueError:
        return None
    return None


def _timeline_start_match(text: str) -> Optional[re.Match[str]]:
    return re.match(
        r'^\s*(?:[-—–•·*]|[🟢🔵🟡🟣🔴⚫⚪●○])?\s*[\[(]?(\d{1,2}:\d{2}(?::\d{2})?)[\])]?(?:\s*[—–-]\s*[\[(]?(\d{1,2}:\d{2}(?::\d{2})?)[\])]?)?(?!:\d)',
        text or '',
    )



def _is_ignorable_timeline_interruption(text: str) -> bool:
    line = (text or '').strip()
    if not line:
        return False
    lowered = line.lower()
    if re.match(r'^[（(\[].*[）)\]]$', line):
        return True
    if lowered.startswith(('勘误', '注：', '注:', '备注：', '备注:')):
        return True
    if re.match(r'^[（(\[].*?(勘误|注[:：]|备注[:：]).*[）)\]]$', line, flags=re.I):
        return True
    if re.match(r'^\*.+\*$', line):
        return True
    return False


def _extract_first_increasing_timeline_lines(text: str) -> list[str]:
    lines = [line.strip() for line in normalize_shownotes_text(text or '').splitlines() if line.strip()]
    if not lines:
        return []

    seq = []
    last_seconds = None
    started = False
    for line in lines:
        m = _timeline_start_match(line)
        if not m:
            if started and _is_ignorable_timeline_interruption(line):
                continue
            if started:
                break
            continue
        seconds = _timestamp_to_seconds(m.group(1))
        if seconds is None:
            if started:
                break
            continue
        if started and last_seconds is not None and seconds <= last_seconds:
            break
        seq.append(line)
        last_seconds = seconds
        started = True
    return seq


def _strip_timeline_prefix_markdown(text: str) -> str:
    text = (text or '').strip()
    if not text:
        return ''
    patterns = [
        r'^(?:>\s*)?(?:[-—–•·*]|[🟢🔵🟡🟣🔴⚫⚪●○])?\s*\*{0,2}[\[(]?\d{1,2}:\d{2}(?::\d{2})?[\])]?\s*[—–-]\s*[\[(]?\d{1,2}:\d{2}(?::\d{2})?[\])]?\*{0,2}\s*',
        r'^(?:>\s*)?(?:[-—–•·*]|[🟢🔵🟡🟣🔴⚫⚪●○])?\s*\*{0,2}[\[(]?\d{1,2}:\d{2}(?::\d{2})?[\])]?\*{0,2}\s*',
    ]
    for pattern in patterns:
        new_text = re.sub(pattern, '', text).strip()
        if new_text != text:
            return new_text
    return text



def _split_timeline_title_and_suffix(text: str) -> tuple[str, str]:
    text = _strip_timeline_prefix_markdown(text)
    if not text:
        return '', ''
    m = re.match(r'^(.*?)(\s+\*(?!\*)(.+?)\*(?:.*)?)$', text)
    if not m:
        return text, ''
    title = (m.group(1) or '').strip()
    suffix = (m.group(2) or '').strip()
    return title, suffix


def _block_plain_markdown_pairs(block: str) -> list[tuple[str, str]]:
    block = block or ''
    lower = block.lower()

    pairs = []
    if '<ul' in lower or '<ol' in lower:
        for li in re.findall(r'<li[^>]*>(.*?)</li>', block, flags=re.I | re.S):
            plain_line = normalize_shownotes_text(li).strip()
            markdown_line = _html_inline_to_markdown(li).strip()
            if plain_line:
                pairs.append((plain_line, markdown_line or plain_line))
    else:
        plain_lines = [line.strip() for line in normalize_shownotes_text(block).splitlines() if line.strip()]
        markdown_block = _shownotes_block_to_markdown(block) if '<' in block and '>' in block else normalize_shownotes_text(block)
        markdown_lines = [line.strip() for line in markdown_block.splitlines() if line.strip()]
        if len(markdown_lines) != len(plain_lines):
            markdown_lines = plain_lines[:]
        for i, line in enumerate(plain_lines):
            md_line = markdown_lines[i] if i < len(markdown_lines) else line
            pairs.append((line, md_line))
    return pairs


def _extract_first_increasing_timeline_entries(block: str) -> list[dict]:
    pairs = _block_plain_markdown_pairs(block)
    if not pairs:
        return []

    seq = []
    last_seconds = None
    started = False
    for line, md_line in pairs:
        m = _timeline_start_match(line)
        if not m:
            if started and _is_ignorable_timeline_interruption(line):
                continue
            if started:
                break
            continue
        seconds = _timestamp_to_seconds(m.group(1))
        if seconds is None:
            if started:
                break
            continue
        if started and last_seconds is not None and seconds <= last_seconds:
            break
        seq.append({'time': m.group(1), 'plain_line': line, 'markdown_line': md_line})
        last_seconds = seconds
        started = True
    return seq


def _is_short_timeline_bridge_line(text: str) -> bool:
    line = normalize_shownotes_text(text or '').strip().strip('/')
    if not line:
        return True
    if _timeline_start_match(line):
        return False
    if _is_appendix_start_text(line) or _timeline_blacklist_hit(line):
        return False
    if len(line) > 40:
        return False
    if re.search(r'[。！？!?；;]$', line):
        return False
    if re.search(r'(http|www\.|@|邮箱|微信|商务|合作|投稿|订阅|收听|平台|关注|联系)', line, flags=re.I):
        return False
    return True


def _line_has_cjk(text: str) -> bool:
    return bool(re.search(r'[\u3400-\u9fff]', text or ''))


def _line_looks_ascii_dominant(text: str) -> bool:
    letters = re.findall(r'[A-Za-z]', text or '')
    cjk = re.findall(r'[\u3400-\u9fff]', text or '')
    return len(letters) > 0 and len(letters) >= len(cjk) * 2


def _prefer_duplicate_timeline_entry(existing: dict, candidate: dict) -> dict:
    existing_text = existing.get('plain_line') or existing.get('markdown_line') or ''
    candidate_text = candidate.get('plain_line') or candidate.get('markdown_line') or ''
    if _line_has_cjk(candidate_text) and not _line_has_cjk(existing_text):
        return candidate
    if _line_has_cjk(existing_text) and not _line_has_cjk(candidate_text):
        return existing
    if _line_looks_ascii_dominant(candidate_text) and not _line_looks_ascii_dominant(existing_text):
        return existing
    return existing


def _flatten_timeline_candidate_lines(blocks: list[str]) -> list[dict]:
    tokens = []
    for block_idx, block in enumerate(blocks):
        for line_idx, (plain_line, markdown_line) in enumerate(_block_plain_markdown_pairs(block)):
            plain_line = (plain_line or '').strip()
            markdown_line = (markdown_line or plain_line).strip()
            if not plain_line:
                continue
            tokens.append({
                'block_index': block_idx,
                'line_index': line_idx,
                'plain_line': plain_line,
                'markdown_line': markdown_line,
            })
    return tokens


def _extract_primary_timeline_run_entries_from_blocks(blocks: list[str], min_entries: int = 3) -> list[dict]:
    tokens = _flatten_timeline_candidate_lines(blocks)
    if not tokens:
        return []

    run = []
    last_seconds = None
    started = False

    def finish_or_reset(next_entry: Optional[dict] = None) -> Optional[list[dict]]:
        nonlocal run, last_seconds, started
        if len(run) >= min_entries:
            return run
        run = []
        last_seconds = None
        started = False
        if next_entry is not None:
            run = [next_entry]
            last_seconds = _timestamp_to_seconds(next_entry.get('time') or '')
            started = True
        return None

    for token in tokens:
        line = token.get('plain_line') or ''
        if _is_appendix_start_text(line) or _timeline_blacklist_hit(line):
            completed = finish_or_reset()
            return completed or []

        m = _timeline_start_match(line)
        if not m:
            if not started:
                continue
            if _is_ignorable_timeline_interruption(line) or _is_short_timeline_bridge_line(line):
                continue
            completed = finish_or_reset()
            if completed:
                return completed
            continue

        seconds = _timestamp_to_seconds(m.group(1))
        if seconds is None:
            if started:
                completed = finish_or_reset()
                if completed:
                    return completed
            continue

        entry = dict(token)
        entry['time'] = m.group(1)

        if not started:
            run = [entry]
            last_seconds = seconds
            started = True
            continue

        if last_seconds is not None and seconds > last_seconds:
            run.append(entry)
            last_seconds = seconds
            continue

        if last_seconds is not None and seconds == last_seconds and run:
            run[-1] = _prefer_duplicate_timeline_entry(run[-1], entry)
            continue

        completed = finish_or_reset(next_entry=entry)
        if completed:
            return completed

    return run if len(run) >= min_entries else []


def _primary_timeline_outline_from_entries(entries: list[dict]) -> list[dict]:
    outline = []
    for entry in entries or []:
        markdown_tail = _strip_timeline_prefix_markdown(entry.get('markdown_line') or '')
        markdown_tail = re.sub(r'^(?:[-—–•·]\s*)', '', markdown_tail)
        title_md, suffix_md = _split_timeline_title_and_suffix(markdown_tail)
        cleaned_title = _clean_timeline_text(title_md)
        if not cleaned_title or len(cleaned_title) > 200:
            continue
        item = {
            'time': entry.get('time') or '',
            'title': cleaned_title,
            'subtitles': [],
            'children': [],
            'overflow_markdown': suffix_md.strip(),
        }
        outline.append(item)
    if outline:
        outline[0]['_timeline_start_block_index'] = entries[0].get('block_index', -1)
        outline[-1]['_timeline_end_block_index'] = entries[-1].get('block_index', -1)
    return outline


def extract_primary_timeline_run_outline(text: str) -> list[dict]:
    raw = (text or '').strip()
    if not raw:
        return []
    blocks = _extract_shownotes_blocks(raw) if '<' in raw and '>' in raw else [line.strip() for line in unescape(raw).replace('\u00a0', ' ').splitlines() if line.strip()]
    entries = _extract_primary_timeline_run_entries_from_blocks(blocks)
    return _primary_timeline_outline_from_entries(entries)


def _timeline_entry_count(outline: list[dict]) -> int:
    stats = summarize_timeline_outline(outline)
    return stats.get('flattened_timeline_entries_total', 0)


def extract_timeline_outline(text: str) -> list[dict]:
    """提取层级化时间线：支持章节 H2 / 时间线 H3 / bullets H4。"""
    raw = (text or '').strip()
    if not raw:
        return []

    blocks = []
    html_mode = '<' in raw and '>' in raw
    if html_mode:
        blocks = _extract_shownotes_blocks(raw)
    else:
        normalized = unescape(raw).replace('\u00a0', ' ')
        for line in normalized.splitlines():
            line = line.strip()
            if line:
                blocks.append(line)

    outline = []
    current_h2 = None
    current_h3 = None
    first_timeline_block_index = -1
    last_timeline_block_index = -1
    last_timeline_seconds = None
    stop_after_timeline_reset = False

    def current_target():
        return current_h3 if current_h3 else current_h2

    def add_timeline_entry(ts: str, title: str, idx: int, overflow_markdown: str = '', allow_out_of_order: bool = False):
        nonlocal current_h2, current_h3, first_timeline_block_index, last_timeline_block_index, last_timeline_seconds
        cleaned_title = _clean_timeline_text(title)
        if not cleaned_title or len(cleaned_title) > 200:
            return False

        seconds = _timestamp_to_seconds(ts)
        if (not allow_out_of_order) and last_timeline_seconds is not None and seconds is not None and seconds < last_timeline_seconds:
            return 'reset'

        target = None
        if current_h2 and current_h2.get('_chapter_mode'):
            current_h3 = {
                'time': ts,
                'title': cleaned_title,
                'raw_bullets': [],
                'overflow_lines': [],
            }
            current_h2.setdefault('children', []).append(current_h3)
            target = current_h3
        else:
            current_h2 = {
                'time': ts,
                'title': cleaned_title,
                'raw_bullets': [],
                'overflow_lines': [],
                'children': [],
                '_chapter_mode': False,
            }
            outline.append(current_h2)
            current_h3 = None
            target = current_h2
        if overflow_markdown and target is not None:
            target.setdefault('overflow_lines', []).append(overflow_markdown.strip())
        if first_timeline_block_index < 0:
            first_timeline_block_index = idx
        last_timeline_block_index = idx
        if seconds is not None:
            last_timeline_seconds = seconds
        return True

    def should_tolerate_out_of_order(current_seconds: Optional[int], idx: int, future_seconds: Optional[list[int]] = None) -> bool:
        if current_seconds is None or last_timeline_seconds is None:
            return False
        if current_seconds >= last_timeline_seconds:
            return True
        candidates = []
        if future_seconds:
            candidates.extend([s for s in future_seconds if s is not None])
        for next_idx in range(idx + 1, min(len(blocks), idx + 3)):
            future_entries = _block_timeline_entries(blocks[next_idx])
            for entry in future_entries:
                seconds = _timestamp_to_seconds(entry.get('time') or '')
                if seconds is not None:
                    candidates.append(seconds)
        return any(seconds > last_timeline_seconds for seconds in candidates)

    for idx, block in enumerate(blocks):
        if stop_after_timeline_reset:
            break
        lower_block = block.lower()

        raw_text = normalize_shownotes_text(block)
        text_block = raw_text.strip()
        markdown_block = _shownotes_block_to_markdown(block).strip() if '<' in block and '>' in block else text_block
        if not text_block and not markdown_block:
            continue

        chapter = _parse_chapter_heading(text_block, block)
        if chapter:
            if _is_timeline_label_text(chapter['title']):
                if not _extract_first_increasing_timeline_entries(block):
                    continue
            else:
                if not outline and not _has_following_increasing_timeline_context(blocks, idx, required_entries=2, lookahead=4):
                    continue
                current_h2 = {
                    'time': chapter.get('time', ''),
                    'title': chapter['title'],
                    'raw_bullets': [],
                    'overflow_lines': [],
                    'children': [],
                    '_chapter_mode': True,
                }
                outline.append(current_h2)
                current_h3 = None
                if first_timeline_block_index < 0:
                    first_timeline_block_index = idx
                last_timeline_block_index = idx
                continue

        seq_entries = _extract_first_increasing_timeline_entries(block)
        if len(seq_entries) >= 2:
            if current_h2 and current_h2.get('_chapter_mode') and not current_h2.get('children') and len(outline) == 1 and outline[0] is current_h2:
                outline = []
                current_h2 = None
                current_h3 = None
                first_timeline_block_index = -1
                last_timeline_block_index = -1
            for entry_idx, entry in enumerate(seq_entries):
                markdown_tail = _strip_timeline_prefix_markdown(entry.get('markdown_line') or '')
                markdown_tail = re.sub(r'^(?:[-—–•·]\s*)', '', markdown_tail)
                title_md, suffix_md = _split_timeline_title_and_suffix(markdown_tail)
                status = add_timeline_entry(entry.get('time') or '', title_md, idx, suffix_md)
                if status == 'reset':
                    current_seconds = _timestamp_to_seconds(entry.get('time') or '')
                    future_seconds = [
                        _timestamp_to_seconds(next_entry.get('time') or '')
                        for next_entry in seq_entries[entry_idx + 1:]
                    ]
                    if should_tolerate_out_of_order(current_seconds, idx, future_seconds):
                        add_timeline_entry(entry.get('time') or '', title_md, idx, suffix_md, allow_out_of_order=True)
                        continue
                    stop_after_timeline_reset = True
                    break
            continue

        if '<ul' in lower_block:
            target = current_target()
            if not target:
                continue
            bullets = []
            for li in re.findall(r'<li[^>]*>(.*?)</li>', block, flags=re.S | re.I):
                bullet = _clean_timeline_text(li)
                if bullet:
                    bullets.append(bullet)
            if bullets:
                target.setdefault('raw_bullets', []).extend(bullets)
                last_timeline_block_index = idx
            continue

        line_start_match = _line_starts_with_timestamp(text_block)
        if line_start_match:
            can_start_single_line_timeline = bool(outline) or bool(current_h2 and current_h2.get('_chapter_mode')) or _has_following_increasing_timeline_context(blocks, idx, required_entries=2, lookahead=4)
            if can_start_single_line_timeline:
                markdown_tail = _strip_timeline_prefix_markdown(markdown_block)
                title_md, suffix_md = _split_timeline_title_and_suffix(markdown_tail)
                status = add_timeline_entry(line_start_match.group(1), title_md, idx, suffix_md)
                if status == 'reset':
                    current_seconds = _timestamp_to_seconds(line_start_match.group(1))
                    if should_tolerate_out_of_order(current_seconds, idx):
                        add_timeline_entry(line_start_match.group(1), title_md, idx, suffix_md, allow_out_of_order=True)
                        continue
                    stop_after_timeline_reset = True
                    break
                if status:
                    continue

        if '<blockquote' in lower_block:
            target = current_target()
            if target:
                quoted = _blockquote_to_markdown(block)
                if quoted:
                    target.setdefault('overflow_lines', []).append(quoted)
                    last_timeline_block_index = idx
            continue

        if _is_appendix_start_text(text_block):
            if outline:
                break
            continue

        if _timeline_blacklist_hit(block):
            if outline:
                break
            continue

        target = current_target()
        fallback_block = markdown_block or text_block
        if target and fallback_block:
            target.setdefault('overflow_lines', []).append(fallback_block)
            last_timeline_block_index = idx

    deduped = []
    seen_h2 = set()
    for item in outline:
        title = item['title']
        if title in seen_h2:
            continue
        seen_h2.add(title)

        children = []
        seen_h3 = set()
        for child in item.get('children', []):
            child_title = child['title']
            if child_title in seen_h3:
                continue
            seen_h3.add(child_title)
            children.append({
                'time': child.get('time', ''),
                'title': child_title,
                'subtitles': child.get('raw_bullets', []),
                'overflow_markdown': '\n\n'.join(child.get('overflow_lines', [])).strip(),
            })

        deduped.append({
            'time': item.get('time', ''),
            'title': title,
            'subtitles': item.get('raw_bullets', []),
            'children': children,
            'overflow_markdown': '\n\n'.join(item.get('overflow_lines', [])).strip(),
        })

    if deduped:
        deduped[0]['_timeline_start_block_index'] = first_timeline_block_index
        deduped[-1]['_timeline_end_block_index'] = last_timeline_block_index
    return deduped


def extract_timeline_titles(text: str) -> list[str]:
    return [item['title'] for item in extract_timeline_outline(text)]


def summarize_timeline_outline(outline: list[dict]) -> dict:
    outline = outline or []
    child_entries = sum(len(item.get('children') or []) for item in outline)
    top_level_timestamped = sum(1 for item in outline if item.get('time'))
    flattened_entries = child_entries or top_level_timestamped
    appendix_like_sections = sum(1 for item in outline if (not item.get('time')) and not (item.get('children') or []))
    return {
        'top_level_sections': len(outline),
        'top_level_timestamped_entries': top_level_timestamped,
        'child_entries_total': child_entries,
        'flattened_timeline_entries_total': flattened_entries,
        'appendix_like_sections': appendix_like_sections,
    }


def _quoteify_overflow_markdown(block: str) -> str:
    lines = (block or '').splitlines()
    if not lines:
        return ''

    out = []
    for line in lines:
        if line.strip():
            out.append(f'> {line.rstrip()}')
        else:
            if out and out[-1] != '>':
                out.append('>')

    while out and out[-1] == '>':
        out.pop()
    return '\n'.join(out).strip()


def inject_timeline_overflow(answer: str, timeline_outline: list[dict]) -> str:
    """把对应的 Shownotes 原文引用插到 H2/H3 标题正下方，再接 NotebookLM 总结。"""
    if not answer or not timeline_outline:
        return answer

    overflow_map = {}
    for item in timeline_outline:
        block = _quoteify_overflow_markdown(item.get('overflow_markdown', '').strip())
        if block:
            overflow_map[item['title']] = block
        for child in item.get('children', []):
            child_block = _quoteify_overflow_markdown(child.get('overflow_markdown', '').strip())
            if child_block:
                overflow_map[child['title']] = child_block

    if not overflow_map:
        return answer

    lines = answer.splitlines()
    out = []
    injected = set()

    for line in lines:
        if (re.match(r'^##\s+', line) or re.match(r'^###\s+', line)) and out and out[-1].strip() != '':
            out.append('')
        out.append(line)
        if re.match(r'^##\s+', line) or re.match(r'^###\s+', line):
            current_title = re.sub(r'^#+\s+', '', line).strip()
            block = overflow_map.get(current_title, '')
            if block and current_title not in injected:
                out.append('')
                out.extend(block.splitlines())
                out.append('')
                injected.add(current_title)

    return '\n'.join(out).strip() + '\n'


def build_timeline_summary_prompt(info: dict) -> str:
    """构造基于 shownotes 时间轴层级结构的默认总结 prompt。"""
    candidates = [
        info.get('shownotes', ''),
        info.get('description', ''),
    ]
    timeline_outline = []
    for candidate in candidates:
        if not candidate:
            continue
        full_outline = extract_timeline_outline(candidate)
        primary_outline = extract_primary_timeline_run_outline(candidate)
        if primary_outline and _timeline_entry_count(primary_outline) > _timeline_entry_count(full_outline):
            timeline_outline = primary_outline
            break
        start_idx, end_idx = _find_first_timeline_cluster_bounds(candidate, full_outline)
        if start_idx >= 0 and end_idx >= start_idx:
            blocks = _extract_shownotes_blocks(candidate)
            cluster_raw = '\n'.join(blocks[start_idx:end_idx + 1])
            timeline_outline = extract_timeline_outline(cluster_raw)
            if timeline_outline:
                timeline_outline[0]['_timeline_start_block_index'] = start_idx
                timeline_outline[-1]['_timeline_end_block_index'] = end_idx
                break
        if full_outline:
            timeline_outline = full_outline
            break

    info['timeline_outline'] = timeline_outline
    info['timeline_titles'] = [item['title'] for item in timeline_outline]

    if not timeline_outline:
        return '请根据本期播客内容进行结构化总结，包含二级标题，按原顺序输出，必要时使用三级标题，三级标题下请写简洁要点，避免过长整段。不要写导言、结语、编者按或“以下是总结”等套话，直接从第一个二级标题开始。'

    has_nested_children = any(item.get('children') for item in timeline_outline)
    has_h3 = has_nested_children or any(item.get('subtitles') for item in timeline_outline)
    has_h4 = any(child.get('subtitles') for item in timeline_outline for child in item.get('children', []))

    if has_nested_children:
        lines = [
            '请根据本期播客内容进行结构化总结。以我提供的 shownotes 层级作为输出骨架：章节标题输出为二级标题；章节下面的标准时间线标题输出为三级标题；如果三级标题下面还有 bullet，就按原顺序输出为四级标题。不要保留时间戳。不要照抄提纲，而是根据播客内容充实总结。不要输出任何解释性前言、注释、编者按或“原提纲无三级标题”之类的说明，直接从标题结构开始写。',
            'shownotes 结构如下：'
        ]
    elif has_h4:
        lines = [
            '请根据本期播客内容进行结构化总结。以我提供的 shownotes 结构作为输出骨架：二级标题按时间轴主标题输出；如果某个二级标题下面给了三级标题，就在对应二级标题下按原顺序输出三级标题；如果三级标题下面还有 bullet，就按原顺序输出四级标题。不要保留时间戳。不要照抄提纲，而是根据播客内容充实总结。不要输出任何解释性前言、注释、编者按或“原提纲无三级标题”之类的说明，直接从标题结构开始写。',
            'shownotes 结构如下：'
        ]
    elif has_h3:
        lines = [
            '请根据本期播客内容进行结构化总结。以我提供的 shownotes 结构作为输出骨架：二级标题按时间轴主标题输出；如果某个二级标题下面给了三级标题，就在对应二级标题下按原顺序输出三级标题。不要保留时间戳。不要照抄提纲，而是根据播客内容充实总结。不要输出任何解释性前言、注释、编者按或“原提纲无三级标题”之类的说明，直接从标题结构开始写。',
            'shownotes 结构如下：'
        ]
    else:
        lines = [
            '请根据本期播客内容进行结构化总结。以我提供的 shownotes 时间轴标题作为二级标题，按原顺序输出；不要保留时间戳。不要输出任何解释性前言、注释、编者按或“原提纲无三级标题”之类的说明，直接从第一个二级标题开始写。',
            '时间轴标题如下：'
        ]

    for item in timeline_outline:
        lines.append(f'## {item["title"]}')
        if item.get('children'):
            for child in item.get('children', []):
                lines.append(f'### {child["title"]}')
                for subtitle in child.get('subtitles', []):
                    lines.append(f'#### {subtitle}')
        else:
            for subtitle in item.get('subtitles', []):
                lines.append(f'### {subtitle}')
    return '\n'.join(lines)


def parse_source_id(output: str) -> Optional[str]:
    """从 nlm source add 输出中解析 source id。"""
    for line in (output or '').splitlines():
        line = line.strip()
        if line.startswith('Source ID:'):
            return line.split(':', 1)[1].strip()
    return None


def _audio_upload_state_key(notebook_id: str, audio_path: str) -> str:
    audio_name = os.path.basename(audio_path or '').strip()
    try:
        audio_size = os.path.getsize(audio_path)
    except OSError:
        audio_size = -1
    raw = f'{notebook_id}\n{audio_name}\n{audio_size}'
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()


def _audio_upload_state_path(notebook_id: str, audio_path: str) -> str:
    os.makedirs(NLM_SINGLE_FLIGHT_DIR, exist_ok=True)
    key = _audio_upload_state_key(notebook_id, audio_path)
    return os.path.join(NLM_SINGLE_FLIGHT_DIR, f'{key}.json')


def _load_audio_upload_state(notebook_id: str, audio_path: str) -> dict:
    path = _audio_upload_state_path(notebook_id, audio_path)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_audio_upload_state(notebook_id: str, audio_path: str, payload: dict) -> str:
    path = _audio_upload_state_path(notebook_id, audio_path)
    tmp_path = f'{path}.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)
    return path


def _parse_iso_datetime(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat((value or '').strip())
    except Exception:
        return None


def _now_iso_utc() -> str:
    return datetime.now(ZoneInfo('UTC')).isoformat()


def is_explicit_retriable_upload_error(message: str) -> bool:
    msg = (message or '').strip().lower()
    if not msg:
        return False
    markers = [
        'source unavailable',
        'source is unavailable',
        'unsupported file',
        'invalid file',
        'file too large',
        'failed to process file',
        'cannot process file',
    ]
    return any(marker in msg for marker in markers)


def _extract_source_id_from_item(item: dict) -> Optional[str]:
    if not isinstance(item, dict):
        return None
    for key in ['id', 'sourceId', 'source_id']:
        value = (item.get(key) or '').strip() if isinstance(item.get(key), str) else item.get(key)
        if value:
            return str(value).strip()
    return None


def _source_item_matches_audio(item: dict, audio_path: str) -> bool:
    if not isinstance(item, dict):
        return False
    audio_name = os.path.basename(audio_path or '').strip().lower()
    audio_stem = os.path.splitext(audio_name)[0]
    haystacks = []
    for value in item.values():
        if isinstance(value, str):
            haystacks.append(value.lower())
        elif isinstance(value, list):
            haystacks.extend(str(v).lower() for v in value)
        elif isinstance(value, dict):
            haystacks.append(json.dumps(value, ensure_ascii=False).lower())
    blob = '\n'.join(haystacks)
    return bool(audio_name and (audio_name in blob or (audio_stem and audio_stem in blob)))


def get_source_ready_status(source_id: str) -> dict:
    """用 source get 验证 NotebookLM 是否已经产出可查询内容。"""
    source_id = (source_id or '').strip()
    status = {
        'ready': False,
        'char_count': 0,
        'title': '',
        'reason': 'missing_source_id',
    }
    if not source_id:
        return status

    res = run_nlm_command(['source', 'get', source_id, '--json'])
    if res.returncode != 0 or not (res.stdout or '').strip():
        status['reason'] = (res.stderr or res.stdout or 'source_get_failed').strip()[:500]
        return status

    try:
        payload = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        status['reason'] = f'source_get_json_decode_failed: {exc}'
        return status

    value = payload.get('value', payload) if isinstance(payload, dict) else {}
    if not isinstance(value, dict):
        status['reason'] = 'source_get_unexpected_payload'
        return status

    content = value.get('content') or ''
    try:
        char_count = int(value.get('char_count') or len(content or ''))
    except (TypeError, ValueError):
        char_count = len(content or '')

    status['char_count'] = char_count
    status['title'] = (value.get('title') or '').strip()
    if char_count > 0 or bool((content or '').strip()):
        status['ready'] = True
        status['reason'] = 'source_content_ready'
    else:
        status['reason'] = 'source_content_empty'
    return status


def reconcile_audio_source_after_upload(notebook_id: str, audio_path: str, before_sources: Optional[list[dict]] = None, attempts: int = 6, sleep_seconds: float = 5.0) -> tuple[Optional[str], list[dict]]:
    """重新查询 sources，判断音频是否其实已经创建。"""
    before_sources = before_sources or []
    before_ids = {sid for sid in (_extract_source_id_from_item(item) for item in before_sources) if sid}

    for attempt in range(1, attempts + 1):
        sources = list_notebook_sources(notebook_id)
        if sources:
            for item in sources:
                sid = _extract_source_id_from_item(item)
                if sid and sid not in before_ids:
                    return sid, sources
            for item in sources:
                if _source_item_matches_audio(item, audio_path):
                    return _extract_source_id_from_item(item), sources
            if not before_sources and len(sources) == 1:
                return _extract_source_id_from_item(sources[0]), sources
        if attempt < attempts:
            print(f"⏳ NotebookLM source 复查中 ({attempt}/{attempts})...")
            time.sleep(sleep_seconds)
    return None, list_notebook_sources(notebook_id)


def wait_for_source_content_ready(source_id: str, deadline_ts: float = 0, state: Optional[dict] = None) -> tuple[bool, dict]:
    """source id 出现后，继续确认 NotebookLM 已经暴露 transcript/content。"""
    attempt = 0
    last_status = {}
    while True:
        attempt += 1
        status = get_source_ready_status(source_id)
        last_status = status
        now_ts = time.time()
        if status.get('ready'):
            print(f"✅ NotebookLM source 内容已就绪: {source_id} char_count={status.get('char_count', 0)}")
            if state is not None:
                state['audio_source_id'] = source_id
                state['source_ready_at'] = _now_iso_utc()
                state['source_char_count'] = status.get('char_count', 0)
                state['updated_at'] = state['source_ready_at']
                _save_audio_upload_state(state.get('notebook_id', ''), state.get('audio_path', ''), state)
            return True, status

        if state is not None:
            state['audio_source_id'] = source_id
            state['last_checked_at'] = _now_iso_utc()
            state['source_ready_reason'] = status.get('reason', '')
            state['source_char_count'] = status.get('char_count', 0)
            state['updated_at'] = state['last_checked_at']
            _save_audio_upload_state(state.get('notebook_id', ''), state.get('audio_path', ''), state)

        if deadline_ts and now_ts >= deadline_ts:
            return False, last_status

        sleep_seconds = 10 if attempt <= 6 else 20 if attempt <= 12 else 30
        if deadline_ts:
            remaining = max(0, int(deadline_ts - now_ts))
            if remaining <= 0:
                return False, last_status
            sleep_seconds = min(sleep_seconds, remaining)
        print(f"⏳ NotebookLM source 已出现但内容未就绪（第 {attempt} 次，{status.get('reason', '')}），{sleep_seconds}s 后重试...")
        time.sleep(max(1, sleep_seconds))


def wait_for_audio_source_confirmation(notebook_id: str, audio_path: str, before_sources: Optional[list[dict]] = None, deadline_ts: float = 0, state: Optional[dict] = None) -> tuple[Optional[str], list[dict], str]:
    """在固定截止时间前仅通过 source list 轮询确认，不允许重复上传。允许补认证，但补完后只继续查状态。"""
    before_sources = before_sources or []
    attempt = 0
    while True:
        attempt += 1
        if not ensure_nlm_auth():
            if state is not None:
                state['last_checked_at'] = _now_iso_utc()
                state['updated_at'] = state['last_checked_at']
                state['reason'] = 'auth_recovery_failed_while_waiting'
                _save_audio_upload_state(notebook_id, audio_path, state)
            return None, [], 'pending_auth'
        matched_id, sources = reconcile_audio_source_after_upload(
            notebook_id,
            audio_path,
            before_sources=before_sources,
            attempts=1,
            sleep_seconds=0,
        )
        now_ts = time.time()
        if matched_id or sources:
            if matched_id:
                ready, ready_status = wait_for_source_content_ready(matched_id, deadline_ts=deadline_ts, state=state)
                if ready:
                    return matched_id, sources, 'confirmed'
                if deadline_ts and time.time() >= deadline_ts:
                    return None, sources, f"pending_source_content:{ready_status.get('reason', '')}"
            if not before_sources and len(sources) == 1:
                sid = _extract_source_id_from_item(sources[0])
                ready, ready_status = wait_for_source_content_ready(sid, deadline_ts=deadline_ts, state=state)
                if ready:
                    return sid, sources, 'confirmed'
                if deadline_ts and time.time() >= deadline_ts:
                    return None, sources, f"pending_source_content:{ready_status.get('reason', '')}"

        if state is not None:
            state['last_checked_at'] = _now_iso_utc()
            _save_audio_upload_state(notebook_id, audio_path, state)

        if deadline_ts and now_ts >= deadline_ts:
            return None, sources, 'pending_timeout'

        sleep_seconds = 20 if attempt <= 10 else 40 if attempt <= 20 else 60
        if deadline_ts:
            remaining = max(0, int(deadline_ts - now_ts))
            if remaining <= 0:
                return None, sources, 'pending_timeout'
            sleep_seconds = min(sleep_seconds, remaining)
        print(f"⏳ NotebookLM source 尚未确认，继续等待（第 {attempt} 次复查，{sleep_seconds}s 后重试）...")
        time.sleep(max(1, sleep_seconds))


def guarded_add_audio_source(notebook_id: str, audio_path: str, max_wait_seconds: Optional[int] = None) -> dict:
    """对同一 notebook+音频执行 single-flight 上传，最多等待固定时长，不自动二传。允许补认证，但补完后不重传。"""
    max_wait_seconds = int(max_wait_seconds or NLM_AUDIO_UPLOAD_MAX_WAIT_SECONDS)
    hard_wait_seconds = max(max_wait_seconds, NLM_AUDIO_UPLOAD_HARD_WAIT_SECONDS)
    result = {
        'status': 'failed-explicit',
        'audio_source_id': None,
        'reason': '',
        'state_path': _audio_upload_state_path(notebook_id, audio_path),
        'uploaded_now': False,
    }

    if not ensure_nlm_auth():
        result['status'] = 'pending'
        result['reason'] = 'auth_preflight_failed_before_source_add'
        return result

    existing_sources = list_notebook_sources(notebook_id)
    matched_id, matched_sources = reconcile_audio_source_after_upload(
        notebook_id,
        audio_path,
        before_sources=[] if not existing_sources else existing_sources,
        attempts=1,
        sleep_seconds=0,
    )
    if matched_id or matched_sources:
        sid = matched_id or _extract_source_id_from_item(matched_sources[0] if matched_sources else {})
        ready_status = get_source_ready_status(sid)
        if not ready_status.get('ready'):
            result['status'] = 'pending'
            result['audio_source_id'] = sid
            result['reason'] = f"pending_existing_source_content:{ready_status.get('reason', '')}"
            return result
        result['status'] = 'confirmed'
        result['audio_source_id'] = sid
        result['reason'] = 'source_already_exists_and_content_ready'
        state = {
            'status': 'confirmed',
            'notebook_id': notebook_id,
            'audio_path': audio_path,
            'audio_name': os.path.basename(audio_path or ''),
            'audio_source_id': sid,
            'updated_at': _now_iso_utc(),
            'reason': result['reason'],
            'source_char_count': ready_status.get('char_count', 0),
        }
        _save_audio_upload_state(notebook_id, audio_path, state)
        return result

    state = _load_audio_upload_state(notebook_id, audio_path)
    first_upload_at = _parse_iso_datetime(state.get('first_upload_at', '')) if state else None
    if state.get('status') == 'pending' and first_upload_at:
        deadline_ts = first_upload_at.timestamp() + hard_wait_seconds
        remaining = int(deadline_ts - time.time())
        if remaining > 0:
            print(f"⏳ 命中 single-flight pending 状态，不重复上传，继续等待最多 {remaining}s ...")
            sid, _, wait_status = wait_for_audio_source_confirmation(
                notebook_id,
                audio_path,
                before_sources=[],
                deadline_ts=deadline_ts,
                state=state,
            )
            if wait_status == 'confirmed':
                state['status'] = 'confirmed'
                state['audio_source_id'] = sid
                state['updated_at'] = _now_iso_utc()
                state['reason'] = 'source_confirmed_after_wait'
                _save_audio_upload_state(notebook_id, audio_path, state)
                result['status'] = 'confirmed'
                result['audio_source_id'] = sid
                result['reason'] = state['reason']
                return result
            state['status'] = 'pending'
            state['updated_at'] = _now_iso_utc()
            state['reason'] = 'pending_timeout_after_existing_single_flight'
            _save_audio_upload_state(notebook_id, audio_path, state)
            result['status'] = 'pending'
            result['reason'] = state['reason']
            return result

        print(f'⏳ 命中已有 pending 上传，但 {hard_wait_seconds // 60} 分钟确认预算已耗尽，本次不重复上传。')
        state['updated_at'] = _now_iso_utc()
        state['reason'] = 'pending_timeout_budget_exhausted'
        _save_audio_upload_state(notebook_id, audio_path, state)
        result['status'] = 'pending'
        result['reason'] = state['reason']
        return result

    print(f"📤 上传音频到 NotebookLM（single-flight）: {os.path.basename(audio_path)}")
    first_upload_at_iso = _now_iso_utc()
    state = {
        'status': 'pending',
        'notebook_id': notebook_id,
        'audio_path': audio_path,
        'audio_name': os.path.basename(audio_path or ''),
        'first_upload_at': first_upload_at_iso,
        'last_checked_at': first_upload_at_iso,
        'audio_source_id': None,
        'updated_at': first_upload_at_iso,
        'reason': 'upload_in_flight',
    }
    _save_audio_upload_state(notebook_id, audio_path, state)

    cmd = ['source', 'add', notebook_id, '--file', audio_path, '--wait']
    add_res = run_nlm_command(cmd)
    result['uploaded_now'] = True
    combined_message = '\n'.join(part for part in [add_res.stderr.strip(), add_res.stdout.strip()] if part).strip()
    audio_source_id = parse_source_id(add_res.stdout)
    if add_res.returncode == 0 and audio_source_id:
        started_ts = (_parse_iso_datetime(first_upload_at_iso) or datetime.now(ZoneInfo('UTC'))).timestamp()
        hard_deadline_ts = started_ts + hard_wait_seconds
        ready, ready_status = wait_for_source_content_ready(audio_source_id, deadline_ts=hard_deadline_ts, state=state)
        if not ready:
            state['status'] = 'pending'
            state['audio_source_id'] = audio_source_id
            state['updated_at'] = _now_iso_utc()
            state['reason'] = f"pending_source_content:{ready_status.get('reason', '')}"
            _save_audio_upload_state(notebook_id, audio_path, state)
            result['status'] = 'pending'
            result['audio_source_id'] = audio_source_id
            result['reason'] = state['reason']
            return result
        state['status'] = 'confirmed'
        state['audio_source_id'] = audio_source_id
        state['updated_at'] = _now_iso_utc()
        state['reason'] = 'source_add_returned_success_and_content_ready'
        _save_audio_upload_state(notebook_id, audio_path, state)
        result['status'] = 'confirmed'
        result['audio_source_id'] = audio_source_id
        result['reason'] = state['reason']
        return result

    first_upload_dt = _parse_iso_datetime(first_upload_at_iso) or datetime.now(ZoneInfo('UTC'))
    started_ts = first_upload_dt.timestamp()
    base_deadline_ts = started_ts + max_wait_seconds
    hard_deadline_ts = started_ts + hard_wait_seconds
    post_add_deadline_ts = time.time() + NLM_AUDIO_UPLOAD_POST_ADD_CONFIRM_SECONDS
    deadline_ts = min(hard_deadline_ts, max(base_deadline_ts, post_add_deadline_ts))
    remaining_after_add = max(0, int(deadline_ts - time.time()))
    print(f'🔎 上传返回未确认成功，进入 single-flight 确认等待，不重复上传（剩余最多 {remaining_after_add}s）...')
    reconciled_source_id, reconciled_sources, wait_status = wait_for_audio_source_confirmation(
        notebook_id,
        audio_path,
        before_sources=existing_sources,
        deadline_ts=deadline_ts,
        state=state,
    )
    if wait_status == 'confirmed':
        state['status'] = 'confirmed'
        state['audio_source_id'] = reconciled_source_id or audio_source_id
        state['updated_at'] = _now_iso_utc()
        state['reason'] = 'source_confirmed_after_wait'
        _save_audio_upload_state(notebook_id, audio_path, state)
        result['status'] = 'confirmed'
        result['audio_source_id'] = state['audio_source_id']
        result['reason'] = state['reason']
        return result

    if is_explicit_retriable_upload_error(combined_message) and not reconciled_sources:
        state['status'] = 'failed-explicit'
        state['updated_at'] = _now_iso_utc()
        state['reason'] = combined_message or 'explicit_upload_failure'
        _save_audio_upload_state(notebook_id, audio_path, state)
        result['status'] = 'failed-explicit'
        result['reason'] = state['reason']
        return result

    state['status'] = 'pending'
    state['updated_at'] = _now_iso_utc()
    state['reason'] = combined_message or 'pending_timeout_after_source_add'
    _save_audio_upload_state(notebook_id, audio_path, state)
    result['status'] = 'pending'
    result['audio_source_id'] = state.get('audio_source_id')
    result['reason'] = state['reason']
    return result


def find_existing_notebook_id_by_title(notebook_title: str) -> Optional[str]:
    """按确定性标题查找现有 notebook，命中则复用，避免重复创建。"""
    data = list_nlm_notebooks()
    if data is None:
        return None
    for item in data or []:
        if (item.get('title') or '').strip() == notebook_title:
            notebook_id = (item.get('id') or '').strip()
            if notebook_id:
                return notebook_id
    return None


def list_notebook_sources(notebook_id: str) -> list[dict]:
    """列出现有 notebook sources；失败时返回空列表。"""
    res = run_nlm_command(['source', 'list', notebook_id, '--json'])
    if res.returncode != 0 or not (res.stdout or '').strip():
        data = None
    else:
        try:
            data = json.loads(res.stdout)
        except json.JSONDecodeError:
            data = None
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ['sources', 'items', 'value']:
            if isinstance(data.get(key), list):
                return data.get(key) or []
    get_res = run_nlm_command(['notebook', 'get', notebook_id])
    if get_res.returncode == 0 and (get_res.stdout or '').strip():
        try:
            payload = json.loads(get_res.stdout)
        except json.JSONDecodeError:
            payload = {}
        value = payload.get('value', payload) if isinstance(payload, dict) else {}
        sources = value.get('sources') if isinstance(value, dict) else None
        if isinstance(sources, list):
            return sources
    return []


def notebook_has_any_sources(notebook_id: str) -> bool:
    return len(list_notebook_sources(notebook_id)) > 0

def fetch_page(url: str) -> str:
    """抓取网页内容 (使用 curl 避免 SSL 问题)"""
    result = subprocess.run(
        ['curl', '-sL', '-A', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36', url],
        capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        raise Exception(f"curl failed: {result.stderr}")
    return result.stdout

def get_rss_from_apple(podcast_name: str) -> str:
    """从 Apple Podcasts 搜索播客并获取 RSS URL"""
    # Search Apple Podcasts
    search_url = f"https://itunes.apple.com/search?term={quote(podcast_name)}&media=podcast&limit=5"
    result = subprocess.run(
        ['curl', '-sL', search_url],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode == 0 and result.stdout.strip():
        try:
            data = json.loads(result.stdout)
            results = data.get('results', [])
            for r in results:
                feed_url = r.get('feedUrl', '')
                if 'xyzfm.space' in feed_url or 'xiaoyuzhou' in feed_url.lower():
                    return feed_url
                # Also check if podcast name matches
                if podcast_name.lower() in r.get('collectionName', '').lower():
                    return feed_url
        except json.JSONDecodeError:
            pass
    return None

def get_rss_from_apple_page(apple_url: str) -> str:
    """从 Apple Podcasts 页面直接提取 RSS URL"""
    html = fetch_page(apple_url)
    match = re.search(r'"feedUrl":"([^"]+)"', html)
    if match:
        return match.group(1)
    return None


APPLE_SHOW_EPISODE_MAP_CACHE: dict[str, dict[str, dict]] = {}



def _extract_route_episode_number_token(title: str, route: Optional[dict] = None) -> str:
    title = normalize_wrapped_label((title or '').strip())
    route = route or {}
    custom_pattern = (route.get('apple_episode_number_regex') or '').strip()
    if custom_pattern:
        m = re.search(custom_pattern, title, flags=re.I)
        if m:
            return (m.group(1) or '').strip()
    return _extract_episode_number_token(title)



def _build_episode_map_from_apple_show(apple_show_url: str, route: Optional[dict] = None) -> dict[str, dict]:
    apple_show_url = (apple_show_url or '').strip()
    if not apple_show_url:
        return {}
    route = route or {}
    cache_key = f"{apple_show_url}||{route.get('apple_episode_number_regex') or ''}"
    if cache_key in APPLE_SHOW_EPISODE_MAP_CACHE:
        return APPLE_SHOW_EPISODE_MAP_CACHE[cache_key]

    rss_url = get_rss_from_apple_page(apple_show_url)
    if not rss_url:
        APPLE_SHOW_EPISODE_MAP_CACHE[cache_key] = {}
        return {}

    mapping = {}
    for ep in parse_rss_episodes(rss_url):
        token = _extract_route_episode_number_token(ep.get('title') or '', route=route)
        if token:
            mapping[token] = ep
    APPLE_SHOW_EPISODE_MAP_CACHE[cache_key] = mapping
    return mapping



def _find_episode_from_apple_show_by_number(apple_show_url: str, episode_token: str, route: Optional[dict] = None) -> Optional[dict]:
    episode_token = (episode_token or '').strip()
    if not episode_token:
        return None
    return _build_episode_map_from_apple_show(apple_show_url, route=route).get(episode_token)



def _json_unescape(text: str) -> str:
    if text is None:
        return ''
    try:
        return json.loads('"' + text + '"')
    except Exception:
        return unescape(text)


def infer_audio_extension(audio_url: str) -> str:
    path = urlparse(audio_url or '').path.lower()
    ext = os.path.splitext(path)[1]
    if ext in {'.mp3', '.m4a', '.aac', '.wav', '.ogg', '.mp4'}:
        return ext
    return '.m4a'


def is_apple_podcast_url(url: str) -> bool:
    parsed = urlparse(url or '')
    return 'podcasts.apple.com' in (parsed.netloc or '').lower()


def is_apple_episode_url(url: str) -> bool:
    if not is_apple_podcast_url(url):
        return False
    query = parse_qs(urlparse(url).query or '')
    return bool(query.get('i'))


def is_pocketcasts_episode_url(url: str) -> bool:
    parsed = urlparse(url or '')
    host = (parsed.netloc or '').lower()
    path = parsed.path or ''
    return (
        (host == 'pca.st' and path.startswith('/episode/'))
        or (host.endswith('pocketcasts.com') and '/podcast/' in path)
    )


def is_direct_audio_episode_url(url: str) -> bool:
    parsed = urlparse(url or '')
    host = (parsed.netloc or '').lower()
    path = (parsed.path or '').lower()
    if not parsed.scheme.startswith('http'):
        return False
    if not any(path.endswith(ext) for ext in ('.mp3', '.m4a', '.aac', '.wav', '.ogg')):
        return False
    return host.startswith('audio') and host.endswith('redcircle.com')


GOODPODS_QTFM_SHOW_MAP = {
    # Explicit, finite mirror mapping only. QTFM 200050 currently exposes 19 programs,
    # so this must not be treated as generic Goodpods support.
    '反派影评': {
        'qtfm_channel_id': '200050',
    },
}


def is_goodpods_episode_url(url: str) -> bool:
    parsed = urlparse(url or '')
    host = (parsed.netloc or '').lower()
    parts = [p for p in (parsed.path or '').split('/') if p]
    return host.endswith('goodpods.com') and 'podcasts' in parts and len(parts) >= parts.index('podcasts') + 3


def _split_slug_with_numeric_id(segment: str) -> tuple[str, str]:
    segment = unquote(segment or '').strip('/')
    m = re.match(r'(.+)-(\d+)$', segment)
    if not m:
        return segment.replace('-', ' ').strip(), ''
    return m.group(1).replace('-', ' ').strip(), m.group(2)


def parse_goodpods_url_hint(goodpods_url: str) -> dict:
    parsed = urlparse(goodpods_url or '')
    parts = [unquote(p) for p in (parsed.path or '').split('/') if p]
    try:
        idx = parts.index('podcasts')
    except ValueError:
        return {}
    if len(parts) <= idx + 2:
        return {}
    podcast_name, podcast_id = _split_slug_with_numeric_id(parts[idx + 1])
    episode_hint, episode_id = _split_slug_with_numeric_id(parts[idx + 2])
    return {
        'podcast_name': normalize_wrapped_label(podcast_name),
        'podcast_id': podcast_id,
        'episode_hint': normalize_wrapped_label(episode_hint),
        'episode_id': episode_id,
    }


def _compact_episode_match_text(text: str) -> str:
    text = re.sub(r'<[^>]+>', ' ', text or '')
    text = normalize_wrapped_label(unescape(text))
    text = re.sub(r'[（(][^）)]*?分[^）)]*[）)]', '', text)
    text = re.sub(r'\s+', '', text)
    return re.sub(r'[\W_]+', '', text, flags=re.U).lower()


def _extract_embedded_json_after(html: str, marker: str) -> Optional[dict]:
    idx = (html or '').find(marker)
    if idx < 0:
        return None
    start = idx + len(marker)
    payload = (html or '')[start:].lstrip()
    try:
        data, _ = json.JSONDecoder().raw_decode(payload)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _extract_qtfm_init_stores(html: str) -> dict:
    return _extract_embedded_json_after(html, 'window.__initStores=') or {}


def _collect_qtfm_program_items(channel_store: dict) -> list[dict]:
    items = []
    seen = set()

    def add(item: dict):
        if not isinstance(item, dict):
            return
        program_id = item.get('programId') or item.get('pid')
        if not program_id:
            return
        key = str(program_id)
        if key in seen:
            return
        seen.add(key)
        items.append(item)

    programs = channel_store.get('programs') or {}
    for item in programs.get('items') or []:
        add(item)
    for item in channel_store.get('currentList') or []:
        add(item)
    podcaster_info = channel_store.get('podcasterInfo') or {}
    for item in podcaster_info.get('programs') or []:
        add(item)
    return items


def _find_qtfm_program_by_goodpods_hint(channel_id: str, episode_hint: str) -> Optional[dict]:
    channel_html = fetch_page(f'https://m.qtfm.cn/vchannels/{channel_id}/')
    stores = _extract_qtfm_init_stores(channel_html)
    channel_store = stores.get('VChannelStore') or {}
    items = _collect_qtfm_program_items(channel_store)
    if not items:
        return None

    hint_key = _compact_episode_match_text(episode_hint)
    episode_token = _extract_episode_number_token(episode_hint)
    token_matches = []
    for item in items:
        title = item.get('title') or ''
        if episode_token and _extract_episode_number_token(title) == episode_token:
            token_matches.append(item)
        title_key = _compact_episode_match_text(title)
        if hint_key and title_key and (hint_key in title_key or title_key in hint_key):
            return item
    if len(token_matches) == 1:
        return token_matches[0]
    if token_matches and hint_key:
        for item in token_matches:
            title_key = _compact_episode_match_text(item.get('title') or '')
            if hint_key in title_key or title_key in hint_key:
                return item
    return None


def extract_qtfm_program_info(program_url: str, source_url: str = '', goodpods_episode_id: str = '') -> dict:
    html = fetch_page(program_url)
    stores = _extract_qtfm_init_stores(html)
    program_store = stores.get('ProgramStore') or {}
    channel = program_store.get('channelInfo') or {}
    program = program_store.get('programInfo') or {}

    cid = str(channel.get('id') or channel.get('channelId') or '')
    pid = str(program.get('programId') or '')
    canonical = _html_meta_content(html, 'og:url')
    if not canonical and cid and pid:
        canonical = f'https://m.qtfm.cn/vchannels/{cid}/programs/{pid}'

    title = normalize_wrapped_label(program.get('title') or _html_meta_content(html, 'og:title') or '')
    podcast_name = normalize_wrapped_label(channel.get('title') or '')
    description = normalize_wrapped_label(channel.get('description') or channel.get('desc') or _html_meta_content(html, 'description') or '')
    audio_url = (program.get('audioUrl') or '').strip()
    if not audio_url:
        m = re.search(r'"audioUrl":"((?:\\.|[^"])*)"', html or '', re.S)
        if m:
            audio_url = _json_unescape(m.group(1)).strip()

    info = {
        'source_url': source_url or program_url,
        'episode_url': canonical or program_url,
        'qtfm_url': canonical or program_url,
        'audio_url': audio_url,
        'episode_id': f"qtfm-{cid}-{pid}" if cid and pid else goodpods_episode_id,
        'title': title,
        'podcast_name': podcast_name,
        'description': description,
        'shownotes': description,
        'duration': int(program.get('duration') or 0),
        'play_count': channel.get('playCount') or 0,
        'podcast_id': cid,
    }
    if goodpods_episode_id:
        info['goodpods_episode_id'] = goodpods_episode_id
    if channel.get('cover'):
        info['podcast_cover_url'] = channel.get('cover')
    return info


def extract_goodpods_episode_info(html: str, goodpods_url: str) -> dict:
    """Resolve Goodpods episode pages. Goodpods often serves a Cloudflare shell, so use URL hints plus known public mirrors."""
    hint = parse_goodpods_url_hint(goodpods_url)
    podcast_name = hint.get('podcast_name') or ''

    audio_match = re.search(r'https?://[^\s"\'<>]+?\.(?:mp3|m4a|aac|wav|ogg)(?:\?[^\s"\'<>]*)?', html or '', re.I)
    if audio_match:
        title = _html_meta_content(html, 'og:title') or hint.get('episode_hint') or 'Goodpods episode'
        return {
            'source_url': goodpods_url,
            'goodpods_url': goodpods_url,
            'episode_url': goodpods_url,
            'audio_url': unescape(audio_match.group(0)).rstrip('.,)'),
            'episode_id': hint.get('episode_id', ''),
            'title': normalize_wrapped_label(title),
            'podcast_name': podcast_name or 'Goodpods',
            'description': _html_meta_content(html, 'description') or '',
            'shownotes': _html_meta_content(html, 'description') or '',
            'duration': 0,
        }

    route = GOODPODS_QTFM_SHOW_MAP.get(podcast_name)
    if route and route.get('qtfm_channel_id'):
        program = _find_qtfm_program_by_goodpods_hint(route['qtfm_channel_id'], hint.get('episode_hint') or '')
        if not program:
            raise RuntimeError(f"Goodpods resolver: 未在蜻蜓频道 {route['qtfm_channel_id']} 找到匹配节目")
        pid = program.get('programId') or program.get('pid')
        program_url = f"https://m.qtfm.cn/vchannels/{route['qtfm_channel_id']}/programs/{pid}"
        info = extract_qtfm_program_info(program_url, source_url=goodpods_url, goodpods_episode_id=hint.get('episode_id', ''))
        info['goodpods_url'] = goodpods_url
        return info

    raise RuntimeError(f"Goodpods resolver: 当前 Goodpods 页面不可直接解析，且未配置播客映射: {podcast_name or goodpods_url}")


def extract_direct_audio_episode_info(audio_url: str) -> dict:
    parsed = urlparse(audio_url or '')
    path = parsed.path or ''
    episode_id = ''
    m = re.search(r'/episodes/([0-9a-f-]{20,})/', path, re.I)
    if m:
        episode_id = m.group(1)
    short_id = episode_id[:8] if episode_id else sanitize_filename(os.path.basename(path) or 'audio')
    title = f'RedCircle audio {short_id}'.strip()
    return {
        'source_url': audio_url,
        'episode_url': audio_url,
        'audio_url': audio_url,
        'episode_id': episode_id,
        'title': title,
        'podcast_name': 'Pocket Casts Audio',
        'description': 'Audio direct link resolved from Pocket Casts / RedCircle.',
        'shownotes': '',
        'duration': 0,
    }


def _html_meta_content(html: str, key: str) -> str:
    patterns = [
        rf'<meta[^>]+property=["\']{re.escape(key)}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']{re.escape(key)}["\']',
        rf'<meta[^>]+name=["\']{re.escape(key)}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']{re.escape(key)}["\']',
    ]
    for pat in patterns:
        m = re.search(pat, html or '', re.I | re.S)
        if m:
            return normalize_wrapped_label(unescape(m.group(1)).strip())
    return ''


def extract_pocketcasts_episode_info(html: str, episode_url: str) -> dict:
    """Extract episode metadata from Pocket Casts / pca.st pages."""
    info = {
        'source_url': episode_url,
        'pocketcasts_url': episode_url,
        'episode_url': episode_url,
    }

    title = _html_meta_content(html, 'og:title') or _html_meta_content(html, 'twitter:title')
    if title:
        info['title'] = title

    desc = _html_meta_content(html, 'description') or _html_meta_content(html, 'og:description')
    if desc:
        info['description'] = desc

    canonical = ''
    m = re.search(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']', html or '', re.I | re.S)
    if m:
        canonical = unescape(m.group(1)).strip()
        info['episode_url'] = canonical
    og_url = _html_meta_content(html, 'og:url')
    if og_url:
        info['episode_url'] = og_url

    path_for_name = urlparse(info.get('episode_url') or canonical or episode_url).path
    parts = [unescape(p) for p in path_for_name.split('/') if p]
    if len(parts) >= 2 and parts[0] == 'podcast':
        info['podcast_name'] = parts[1]

    audio_match = re.search(r'https?://[^\s"\'<>]+?/stream\.(?:mp3|m4a|aac|wav|ogg)', html or '', re.I)
    if not audio_match:
        audio_match = re.search(r'https?://[^\s"\'<>]+?\.(?:mp3|m4a|aac|wav|ogg)(?:\?[^\s"\'<>]*)?', html or '', re.I)
    if audio_match:
        info['audio_url'] = unescape(audio_match.group(0)).rstrip('.,)')

    show_notes = ''
    m = re.search(r'<div[^>]+class=["\'][^"\']*showNotesWrapper[^"\']*["\'][^>]*>(.*?)</div>', html or '', re.I | re.S)
    if m:
        show_notes = m.group(1).strip()
    if not show_notes:
        # Fallback: Pocket Casts also embeds the shownotes as escaped HTML in app data.
        m = re.search(r'"description_html",\s*"((?:\\.|[^"\\])*)"', html or '', re.S)
        if m:
            show_notes = _json_unescape(m.group(1)).strip()
    if show_notes:
        info['shownotes'] = show_notes
        if not info.get('description'):
            info['description'] = normalize_shownotes_text(show_notes)

    duration_match = re.search(r'"duration",\s*(\d+)', html or '')
    if duration_match:
        try:
            info['duration'] = int(duration_match.group(1))
        except ValueError:
            pass

    episode_id = ''
    m = re.search(r'/episode/([0-9a-f-]{20,})', episode_url, re.I) or re.search(r'/([0-9a-f-]{20,})(?:[/?#]|$)', info.get('episode_url', ''), re.I)
    if m:
        episode_id = m.group(1)
    if episode_id:
        info['episode_id'] = episode_id

    return info


def extract_apple_episode_info(html: str, apple_url: str) -> dict:
    info = {
        'source_url': apple_url,
        'apple_url': apple_url,
        'episode_url': apple_url,
    }

    title_match = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html, re.I)
    if title_match:
        info['title'] = normalize_wrapped_label(unescape(title_match.group(1).strip()))

    podcast_match = re.search(r'"showOffer":\{"title":"((?:\\.|[^"])*)"', html, re.S)
    if podcast_match:
        info['podcast_name'] = normalize_wrapped_label(_json_unescape(podcast_match.group(1)).strip())

    audio_match = re.search(r'"streamUrl":"((?:\\.|[^"])*)"', html, re.S)
    if audio_match:
        info['audio_url'] = _json_unescape(audio_match.group(1)).strip()

    desc_match = re.search(r'"description":"((?:\\.|[^"])*)"', html, re.S)
    if desc_match:
        desc = _json_unescape(desc_match.group(1)).strip()
        info['description'] = desc
        info['shownotes'] = desc

    pub_match = re.search(r'"releaseDate":"((?:\\.|[^"])*)"', html, re.S)
    if pub_match:
        info['pub_date'] = _json_unescape(pub_match.group(1)).strip()

    duration_match = re.search(r'"duration":(\d+)', html)
    if duration_match:
        try:
            info['duration'] = int(duration_match.group(1))
        except ValueError:
            pass

    feed_match = re.search(r'"feedUrl":"((?:\\.|[^"])*)"', html, re.S)
    if feed_match:
        info['rss_url'] = _json_unescape(feed_match.group(1)).strip()

    webpage_match = re.search(r'href="([^"]+)"[^>]*>\s*<span[^>]*>Episode Webpage', html, re.I | re.S)
    if webpage_match:
        info['episode_webpage'] = unescape(webpage_match.group(1).strip())
        info['episode_url'] = info['episode_webpage']

    episode_id = (parse_qs(urlparse(apple_url).query or {}).get('i') or [''])[0]
    if episode_id:
        info['apple_episode_id'] = episode_id
        info['episode_id'] = episode_id

    episode_webpage = (info.get('episode_webpage') or '').strip()
    if 'xiaoyuzhoufm.com/episode/' in episode_webpage:
        try:
            xy_html = fetch_page(episode_webpage)
            xy_info = extract_episode_info(xy_html, episode_webpage)
            if xy_info:
                merged = dict(xy_info)
                merged['source_url'] = apple_url
                merged['apple_url'] = apple_url
                merged['episode_webpage'] = episode_webpage
                merged['episode_url'] = episode_webpage
                merged['rss_url'] = info.get('rss_url', '') or merged.get('rss_url', '')
                merged['apple_episode_id'] = info.get('apple_episode_id', '')
                if info.get('audio_url') and not merged.get('audio_url'):
                    merged['audio_url'] = info['audio_url']
                if info.get('pub_date') and not merged.get('pub_date'):
                    merged['pub_date'] = info['pub_date']
                if info.get('title') and not merged.get('title'):
                    merged['title'] = info['title']
                if info.get('podcast_name') and not merged.get('podcast_name'):
                    merged['podcast_name'] = info['podcast_name']
                return merged
        except Exception:
            pass

    return info


def parse_rss_episodes(rss_url: str) -> list:
    """解析 RSS feed，返回单集列表"""
    episodes = []
    content = fetch_page(rss_url)
    
    try:
        root = ET.fromstring(content)
        channel = root.find('channel')
        if channel is None:
            return episodes
        
        for item in channel.findall('item'):
            ep = {}
            
            # Title
            title_elem = item.find('title')
            if title_elem is not None:
                ep['title'] = title_elem.text or ''
            
            # Episode URL (xiaoyuzhou link)
            link_elem = item.find('link')
            if link_elem is not None:
                link = link_elem.text or ''
                # Remove utm params
                ep['url'] = re.sub(r'\?utm_source=.*$', '', link)
                # Extract eid
                eid_match = re.search(r'/episode/([a-f0-9]+)', link)
                if eid_match:
                    ep['eid'] = eid_match.group(1)
            
            # Audio URL from enclosure
            enclosure = item.find('enclosure')
            if enclosure is not None:
                ep['audio_url'] = enclosure.get('url', '')
            
            # Description
            desc_elem = item.find('description')
            if desc_elem is not None:
                ep['description'] = desc_elem.text or ''
            
            # Pub date
            pubdate_elem = item.find('pubDate')
            if pubdate_elem is not None:
                ep['pub_date'] = pubdate_elem.text or ''
            
            # Duration (itunes:duration)
            for elem in item:
                if 'duration' in elem.tag.lower():
                    ep['duration_str'] = elem.text or ''
            
            if ep.get('eid') or ep.get('audio_url'):
                episodes.append(ep)
    
    except ET.ParseError as e:
        print(f"⚠️ RSS 解析错误: {e}")
    
    return episodes

def extract_podcast_info(html: str) -> dict:
    """从播客主页提取信息"""
    info = {}
    json_match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(\{.+?\})</script>', html, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(1))
            podcast = data.get('props', {}).get('pageProps', {}).get('podcast', {})
            if podcast:
                info['title'] = normalize_wrapped_label(podcast.get('title', ''))
                info['author'] = podcast.get('author', '')
                info['pid'] = podcast.get('pid', '')
                info['episode_count'] = podcast.get('episodeCount', 0)
        except json.JSONDecodeError:
            pass
    return info

def extract_episode_info(html: str, episode_url: str) -> dict:
    """从网页提取单集信息 (使用 __NEXT_DATA__ JSON)"""
    info = {'episode_url': episode_url}
    
    json_match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(\{.+?\})</script>', html, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(1))
            episode = data.get('props', {}).get('pageProps', {}).get('episode', {})
            
            if episode:
                info['title'] = normalize_wrapped_label(episode.get('title', ''))
                info['description'] = episode.get('description', '')
                info['shownotes'] = episode.get('shownotes', '')
                info['duration'] = episode.get('duration', 0)
                info['pub_date'] = episode.get('pubDate', '')
                info['play_count'] = episode.get('playCount', 0)
                info['comment_count'] = episode.get('commentCount', 0)
                info['episode_id'] = episode.get('eid', '')
                
                podcast = episode.get('podcast', {})
                info['podcast_name'] = normalize_wrapped_label(podcast.get('title', ''))
                info['podcast_author'] = podcast.get('author', '')
                info['podcast_id'] = podcast.get('pid', '')
                
                image = episode.get('image', {}) if isinstance(episode.get('image', {}), dict) else {}
                if image.get('smallPicUrl'):
                    info['episode_cover_url'] = image['smallPicUrl']
                elif image.get('picUrl'):
                    info['episode_cover_url'] = image['picUrl']

                podcast_image = podcast.get('image', {}) if isinstance(podcast.get('image', {}), dict) else {}
                if podcast_image.get('smallPicUrl'):
                    info['podcast_cover_url'] = podcast_image['smallPicUrl']
                elif podcast_image.get('picUrl'):
                    info['podcast_cover_url'] = podcast_image['picUrl']

                enclosure = episode.get('enclosure', {})
                if enclosure.get('url'):
                    info['audio_url'] = enclosure['url']
                else:
                    media = episode.get('media', {})
                    source = media.get('source', {})
                    if source.get('url'):
                        info['audio_url'] = source['url']
        except json.JSONDecodeError:
            pass
    
    if 'audio_url' not in info:
        audio_match = re.search(r'(https://media\.xyzcdn\.net/[^"]+\.m4a)', html)
        if audio_match:
            info['audio_url'] = audio_match.group(1)
    
    if not info.get('title'):
        title_match = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html)
        if title_match:
            info['title'] = normalize_wrapped_label(unescape(title_match.group(1).strip()))
    
    return info

def sanitize_filename(name: str) -> str:
    """清理文件名，移除非法字符"""
    illegal = r'[<>:"/\\|?*]'
    name = re.sub(illegal, '_', name)
    name = name.strip(' .-_')
    if len(name) > 200:
        name = name[:200]
    return name

def format_duration(seconds: int) -> str:
    """格式化时长"""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    else:
        return f"{minutes}:{secs:02d}"

def download_audio(url: str, dest_path: str) -> bool:
    """下载音频文件"""
    print(f"📥 下载音频...")
    result = subprocess.run(
        ['curl', '-L', '-o', dest_path, '-#', 
         '-A', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
         url],
        timeout=600
    )
    if result.returncode != 0:
        print(f"❌ 下载失败")
        return False
    size_mb = os.path.getsize(dest_path) / (1024 * 1024)
    print(f"✅ 音频下载完成 ({size_mb:.1f} MB)")
    return True

def save_link_txt(url: str, dest_path: str):
    """保存网页链接为 .txt 文件"""
    with open(dest_path, 'w', encoding='utf-8') as f:
        f.write(url)
    print(f"✅ 链接文件保存完成")

def save_metadata(info: dict, dest_path: str):
    """保存 metadata 为 JSON 文件"""
    metadata = {
        "title": info.get('title', ''),
        "podcast_name": info.get('podcast_name', ''),
        "podcast_author": info.get('podcast_author', ''),
        "description": info.get('description', ''),
        "shownotes": info.get('shownotes', ''),
        "timeline_titles": info.get('timeline_titles', []),
        "timeline_outline": info.get('timeline_outline', []),
        "timeline_stats": summarize_timeline_outline(info.get('timeline_outline', [])),
        "duration": info.get('duration', 0),
        "duration_formatted": format_duration(info.get('duration', 0)) if isinstance(info.get('duration'), int) else info.get('duration_str', ''),
        "pub_date": info.get('pub_date', ''),
        "Release Date": format_release_date(info.get('pub_date', '')),
        "play_count": info.get('play_count', 0),
        "comment_count": info.get('comment_count', 0),
        "episode_id": info.get('episode_id', ''),
        "podcast_id": info.get('podcast_id', ''),
        "episode_url": info.get('episode_url', ''),
        "source_url": info.get('source_url', ''),
        "audio_url": info.get('audio_url', ''),
        "downloaded_at": datetime.now().isoformat(),
    }
    with open(dest_path, 'w', encoding='utf-8') as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)
    print(f"✅ Metadata 保存完成")

def drive_ls(parent_id: str, query: str = None, max_results: int = 1000) -> list:
    """列出 Drive 下的文件/文件夹"""
    gog = os.path.expanduser('~/.local/bin/gog')
    cmd = [gog, 'drive', 'ls', '--parent', parent_id, '--json', '--max', str(max_results)]
    if query:
        cmd += ['--query', query]
    result = subprocess.run(cmd, capture_output=True, text=True, errors='replace')
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        data = json.loads(result.stdout)
        return data.get('files', []) or []
    except json.JSONDecodeError:
        return []


def resolve_drive_folder_id(folder_path: str) -> Optional[str]:
    """解析 Drive 文件夹路径，返回 folder ID；不存在则返回 None"""
    parts = folder_path.strip('/').split('/')
    parent_id = 'root'
    
    for folder_name in parts:
        files = drive_ls(
            parent_id,
            f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        )
        if not files:
            return None
        parent_id = files[0]['id']
    
    return parent_id


def get_or_create_folder(folder_path: str) -> str:
    """获取或创建 Drive 文件夹，返回 folder ID"""
    parts = folder_path.strip('/').split('/')
    parent_id = 'root'
    gog = os.path.expanduser('~/.local/bin/gog')
    
    for folder_name in parts:
        folder_name = folder_name.strip('- ')
        files = drive_ls(
            parent_id,
            f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        )
        folder_id = files[0]['id'] if files else None
        
        if not folder_id:
            mkdir_result = subprocess.run(
                [gog, 'drive', 'mkdir', folder_name, '--parent', parent_id, '--json'],
                capture_output=True, text=True, errors='replace'
            )
            if mkdir_result.returncode == 0 and mkdir_result.stdout.strip():
                try:
                    data = json.loads(mkdir_result.stdout)
                    if 'folder' in data:
                        folder_id = data['folder'].get('id')
                    else:
                        folder_id = data.get('id')
                except json.JSONDecodeError:
                    pass
            
            if not folder_id:
                print(f"❌ 无法创建文件夹: {folder_name}")
                return None
        
        parent_id = folder_id
    
    return parent_id

def upload_to_drive(local_path: str, drive_folder: str, filename: str) -> bool:
    """上传到 Google Drive"""
    print(f"📤 上传: {filename}")
    gog = os.path.expanduser('~/.local/bin/gog')
    
    folder_id = get_or_create_folder(drive_folder)
    if not folder_id:
        return False
    
    upload_cmd = [gog, 'drive', 'upload', local_path, '--parent', folder_id, '--name', filename]
    result = subprocess.run(upload_cmd, capture_output=True, text=True, errors='replace')
    
    if result.returncode != 0:
        print(f"❌ 上传失败: {result.stderr}")
        return False
    return True


def transcode_audio_to_notebooklm_mp3(audio_path: str) -> Optional[str]:
    """把 NotebookLM 偶发无法处理的 m4a 转成保守 mp3，作为一次性 fallback。"""
    ffmpeg = shutil.which('ffmpeg') or '/usr/local/bin/ffmpeg'
    if not os.path.exists(ffmpeg):
        print('⚠️ ffmpeg 不可用，无法执行 mp3 fallback')
        return None
    base, _ = os.path.splitext(audio_path)
    out_path = f'{base}.notebooklm.mp3'
    cmd = [
        ffmpeg,
        '-y',
        '-i', audio_path,
        '-vn',
        '-ac', '1',
        '-ar', '24000',
        '-b:a', '64k',
        out_path,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, errors='replace')
    if res.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) <= 0:
        print(f"⚠️ mp3 fallback 转码失败: {(res.stderr or res.stdout or '').strip()[:500]}")
        return None
    original_mb = os.path.getsize(audio_path) / (1024 * 1024)
    fallback_mb = os.path.getsize(out_path) / (1024 * 1024)
    print(f"🔁 已生成 NotebookLM mp3 fallback: {os.path.basename(out_path)} ({original_mb:.1f} MB -> {fallback_mb:.1f} MB)")
    return out_path


def delete_notebooklm_source(source_id: str) -> bool:
    source_id = (source_id or '').strip()
    if not source_id:
        return False
    res = run_nlm_command(['source', 'delete', source_id, '--confirm'])
    if res.returncode != 0:
        print(f"⚠️ 删除失败 source 失败: {source_id} {(res.stderr or res.stdout or '').strip()[:500]}")
        return False
    print(f"🧹 已删除失败 source: {source_id}")
    return True


def should_retry_audio_upload_as_mp3(audio_path: str, upload_result: dict) -> bool:
    if os.path.splitext(audio_path)[1].lower() == '.mp3':
        return False
    reason = (upload_result or {}).get('reason') or ''
    markers = [
        'Could not add file source',
        'pending_source_content',
        'pending_existing_source_content',
        'source_content_empty',
    ]
    return any(marker in reason for marker in markers)


NLM_WRAPPER = os.path.expanduser('~/.openclaw/workspace/skills/nlm-cli/scripts/nlm.mjs')
NODE_BIN = shutil.which('node') or '/usr/local/bin/node'
NLM_SINGLE_FLIGHT_DIR = os.path.expanduser('~/.openclaw/workspace/tmp/notebooklm_source_add')
NLM_PROFILE_OVERRIDE_PATH = os.path.expanduser('~/.openclaw/workspace/tmp/nlm_profile_override.json')
NLM_CHROME_PORT_MAP_PATH = os.path.expanduser('~/.notebooklm-mcp-cli/chrome-port-map.json')
NLM_DEFAULT_PROFILE = 'default'
NLM_FALLBACK_PROFILE = 'secondary'
NLM_AUDIO_UPLOAD_MAX_WAIT_SECONDS = 20 * 60
NLM_AUDIO_UPLOAD_HARD_WAIT_SECONDS = 25 * 60
NLM_AUDIO_UPLOAD_POST_ADD_CONFIRM_SECONDS = 5 * 60
NLM_QUERY_TIMEOUT_SECONDS = 180
NLM_QUERY_MAX_ATTEMPTS = 6
NLM_QUERY_RETRY_SLEEP_SECONDS = 30
FAST_NOTE_FOLDER_DB = '/opt/fast-note/storage/database/db_user_folder_1.sqlite3'
FAST_NOTE_NOTE_DB = '/opt/fast-note/storage/database/db_user_1.sqlite3'
FAST_NOTE_NOTE_HISTORY_DB = '/opt/fast-note/storage/database/db_user_note_history_1.sqlite3'
FAST_NOTE_FILE_DB = '/opt/fast-note/storage/database/db_user_file_1.sqlite3'
FAST_NOTE_SYNC_LOG_DB = '/opt/fast-note/storage/database/db_user_sync_log_1.sqlite3'
FAST_NOTE_VAULT_ROOT = '/opt/fast-note/storage/vault/u_1/note'
FAST_NOTE_FILE_ROOT = '/opt/fast-note/storage/vault/u_1/file'
FAST_NOTE_DEFAULT_ROOT = 'Podcast'
FAST_NOTE_CLIENT_NAME = 'Win'
FAST_NOTE_CLIENT_TYPE = 'ObsidianPlugin'
FAST_NOTE_CLIENT_VERSION = '1.22.15'
FAST_NOTE_BACKUP_DIR = os.path.expanduser('~/.openclaw/workspace/.backups/fast-note')


def run_nlm_command(args: list[str], capture_output: bool = True) -> subprocess.CompletedProcess:
    """运行 nlm wrapper，并在未显式指定时自动注入 NLM_PROFILE。"""
    cmd = [NODE_BIN, NLM_WRAPPER] + list(args)
    profile = (os.environ.get('NLM_PROFILE') or '').strip()
    # Some nlm subcommands, notably `download infographic`, select the profile
    # from NLM_PROFILE but do not accept a --profile CLI option.
    supports_profile_option = not (args and args[0] == 'download')
    if profile and supports_profile_option and '--profile' not in cmd and '-p' not in cmd:
        cmd += ['--profile', profile]
    return subprocess.run(cmd, capture_output=capture_output, text=True, errors='replace')


def run_nlm_command_for_profile(profile: str, args: list[str], capture_output: bool = True) -> subprocess.CompletedProcess:
    """Run nlm for a specific profile without mutating the current process env."""
    profile = (profile or '').strip()
    env = os.environ.copy()
    if profile:
        env['NLM_PROFILE'] = profile
    cmd = [NODE_BIN, NLM_WRAPPER] + list(args)
    supports_profile_option = not (args and args[0] == 'download')
    if profile and supports_profile_option and '--profile' not in cmd and '-p' not in cmd:
        cmd += ['--profile', profile]
    return subprocess.run(cmd, capture_output=capture_output, text=True, errors='replace', env=env)


def current_nlm_profile() -> str:
    return (os.environ.get('NLM_PROFILE') or 'default').strip() or 'default'


def _now_local_iso() -> str:
    return datetime.now(ZoneInfo('America/New_York')).isoformat(timespec='seconds')


def load_nlm_profile_override() -> dict:
    try:
        if not os.path.exists(NLM_PROFILE_OVERRIDE_PATH):
            return {}
        with open(NLM_PROFILE_OVERRIDE_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f"⚠️ 读取 NLM profile override 失败，忽略: {exc}")
        return {}


def save_nlm_profile_override(active_profile: str, reason: str, from_profile: str = NLM_DEFAULT_PROFILE) -> None:
    data = {
        'active_profile': active_profile,
        'from_profile': from_profile,
        'reason': reason,
        'updated_at': _now_local_iso(),
    }
    existing = load_nlm_profile_override()
    if existing.get('created_at'):
        data['created_at'] = existing.get('created_at')
    else:
        data['created_at'] = data['updated_at']
    os.makedirs(os.path.dirname(NLM_PROFILE_OVERRIDE_PATH), exist_ok=True)
    with open(NLM_PROFILE_OVERRIDE_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def clear_nlm_profile_override(reason: str = '') -> None:
    if os.path.exists(NLM_PROFILE_OVERRIDE_PATH):
        os.remove(NLM_PROFILE_OVERRIDE_PATH)
        suffix = f": {reason}" if reason else ''
        print(f"✅ NLM profile override 已清除{suffix}")


def can_fallback_nlm_profile(profile: str) -> bool:
    if (os.environ.get('NLM_DISABLE_PROFILE_FALLBACK') or '').strip() in {'1', 'true', 'True', 'yes'}:
        return False
    return (profile or '').strip() == NLM_DEFAULT_PROFILE


def default_nlm_profile_auth_valid() -> bool:
    res = run_nlm_command_for_profile(NLM_DEFAULT_PROFILE, ['login', '--check'])
    return not nlm_auth_check_failed(res)


def nlm_cdp_port_for_profile(profile: str) -> str:
    try:
        with open(NLM_CHROME_PORT_MAP_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return ''
    if not isinstance(data, dict):
        return ''
    for port, item in data.items():
        if isinstance(item, dict) and (item.get('profile') or '').strip() == profile:
            return str(port).strip()
    return ''


def refresh_nlm_profile_from_cdp(profile: str) -> bool:
    port = nlm_cdp_port_for_profile(profile)
    if not port:
        return False
    cdp_url = f'http://127.0.0.1:{port}'
    print(f"🔐 NotebookLM profile={profile}: 尝试从 CDP 刷新认证 ({cdp_url})")
    login_res = run_nlm_command_for_profile(profile, ['login', '--provider', 'openclaw', '--cdp-url', cdp_url])
    if nlm_login_failed(login_res):
        detail = (login_res.stderr or login_res.stdout or '').strip()
        print(f"⚠️ CDP 刷新认证失败 profile={profile}: {detail[:500]}")
        return False

    check_res = run_nlm_command_for_profile(profile, ['login', '--check'])
    if nlm_auth_check_failed(check_res):
        detail = (check_res.stderr or check_res.stdout or '').strip()
        print(f"⚠️ CDP 刷新后校验仍失败 profile={profile}: {detail[:500]}")
        return False

    print(f"✅ CDP 刷新认证成功 profile={profile}")
    return True


def resolve_effective_nlm_profile(preferred_profile: str) -> str:
    preferred_profile = (preferred_profile or NLM_DEFAULT_PROFILE).strip() or NLM_DEFAULT_PROFILE
    if preferred_profile != NLM_DEFAULT_PROFILE:
        return preferred_profile

    override = load_nlm_profile_override()
    active = (override.get('active_profile') or '').strip()
    if not active:
        return preferred_profile

    if default_nlm_profile_auth_valid():
        clear_nlm_profile_override('default auth recovered')
        return NLM_DEFAULT_PROFILE

    print(f"🪪 NLM profile override active: {NLM_DEFAULT_PROFILE} -> {active} ({override.get('reason') or 'fallback'})")
    return active


def activate_nlm_profile_fallback(reason: str, from_profile: str = NLM_DEFAULT_PROFILE) -> bool:
    fallback_profile = (os.environ.get('NLM_FALLBACK_PROFILE') or NLM_FALLBACK_PROFILE).strip() or NLM_FALLBACK_PROFILE
    if fallback_profile == from_profile:
        return False

    print(f"🔁 NotebookLM profile fallback: {from_profile} -> {fallback_profile} ({reason})")
    previous = current_nlm_profile()
    os.environ['NLM_PROFILE'] = fallback_profile
    check_res = run_nlm_command(['login', '--check'])
    if not nlm_auth_check_failed(check_res):
        save_nlm_profile_override(fallback_profile, reason=reason, from_profile=from_profile)
        print(f"✅ NotebookLM fallback profile 可用: {fallback_profile}")
        return True

    if refresh_nlm_profile_from_cdp(fallback_profile):
        save_nlm_profile_override(fallback_profile, reason=f'{reason}; cdp_refreshed', from_profile=from_profile)
        print(f"✅ NotebookLM fallback profile 可用: {fallback_profile}")
        return True

    detail = (check_res.stderr or check_res.stdout or '').strip()
    print(f"⚠️ NotebookLM fallback profile 暂不可用: {fallback_profile}: {detail[:500]}")
    os.environ['NLM_PROFILE'] = previous
    return False


def nlm_auth_check_failed(res: subprocess.CompletedProcess) -> bool:
    """`nlm login --check` can print auth errors while still returning 0."""
    output = f"{res.stdout or ''}\n{res.stderr or ''}"
    failure_markers = [
        'Authentication Error',
        'Authentication expired',
        'Run nlm login',
        're-authenticate',
        'not authenticated',
        'login required',
    ]
    return res.returncode != 0 or any(marker.lower() in output.lower() for marker in failure_markers)


def nlm_login_needs_chrome_restart(res: subprocess.CompletedProcess) -> bool:
    output = f"{res.stdout or ''}\n{res.stderr or ''}"
    markers = [
        'WebSocketTimeoutException',
        'Connection timed out',
        'Cannot connect to browser',
        'Chrome DevTools Protocol',
    ]
    return any(marker.lower() in output.lower() for marker in markers)


def nlm_login_failed(res: subprocess.CompletedProcess) -> bool:
    """`nlm login` may report errors while still returning 0."""
    output = f"{res.stdout or ''}\n{res.stderr or ''}"
    failure_markers = [
        'Error:',
        'Authentication Error',
        'Cannot connect to browser',
        'WebSocketTimeoutException',
        'Connection timed out',
        'Run nlm login',
    ]
    success_markers = [
        'Authentication successful',
        'Successfully authenticated',
        'Saved authentication',
        'Login successful',
    ]
    has_failure = any(marker.lower() in output.lower() for marker in failure_markers)
    has_success = any(marker.lower() in output.lower() for marker in success_markers)
    return res.returncode != 0 or (has_failure and not has_success)


def cleanup_nlm_profile_chrome(profile: str) -> None:
    """Restart only the NotebookLM CLI Chrome profile, leaving normal Chrome alone."""
    profile_dir = os.path.expanduser(f"~/.notebooklm-mcp-cli/chrome-profiles/{profile}")
    try:
        ps_res = subprocess.run(
            ['ps', '-axo', 'pid=,command='],
            capture_output=True,
            text=True,
            errors='replace',
        )
    except Exception as exc:
        print(f"⚠️ 无法枚举 Chrome 进程，跳过重启: {exc}")
        return
    if ps_res.returncode != 0:
        print(f"⚠️ 无法枚举 Chrome 进程，跳过重启: {(ps_res.stderr or '').strip()[:300]}")
        return

    current_pid = os.getpid()
    pids: list[int] = []
    for line in (ps_res.stdout or '').splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, command = stripped.partition(' ')
        if profile_dir not in command:
            continue
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid != current_pid:
            pids.append(pid)
    if not pids:
        print(f"🧹 NotebookLM Chrome profile={profile}: 没有需要清理的进程")
        return

    print(f"🧹 NotebookLM Chrome profile={profile}: 清理 {len(pids)} 个 CLI Chrome 进程")
    for pid in pids:
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
        except Exception as exc:
            print(f"⚠️ 结束 Chrome 进程失败 pid={pid}: {exc}")
    time.sleep(2)
    for pid in pids:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
        except Exception as exc:
            print(f"⚠️ 强制结束 Chrome 进程失败 pid={pid}: {exc}")


def parse_notebook_id_from_create_output(output: str) -> str:
    for line in (output or '').splitlines():
        line = line.strip()
        if line.startswith('ID:'):
            return line.split(':', 1)[1].strip()
    return ''


def list_nlm_notebooks() -> Optional[list[dict]]:
    res = run_nlm_command(['notebook', 'list', '--json'])
    if res.returncode != 0 or not (res.stdout or '').strip():
        msg = (res.stderr or res.stdout or '').strip()
        print(f"❌ NotebookLM list 失败: {msg[:500]}")
        return None
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        print(f"❌ NotebookLM list JSON 解析失败: {exc}")
        return None
    return data if isinstance(data, list) else []


def should_prune_notebook_before_create(profile: str) -> bool:
    """对指定 profile 把 NotebookLM 当临时队列：新建前删最旧 notebook 腾位。"""
    if (os.environ.get('NLM_PRUNE_BEFORE_CREATE') or '1').strip() in {'0', 'false', 'False', 'no'}:
        return False
    profiles = (os.environ.get('NLM_PRUNE_BEFORE_CREATE_PROFILES') or 'secondary,default').strip()
    enabled = {p.strip() for p in profiles.split(',') if p.strip()}
    return profile in enabled


def nlm_prune_max_count_for_profile(profile: str) -> int:
    defaults = {
        'default': '485',  # primary-nlm-account: NotebookLM limit cleared; keep a larger rolling queue.
        'secondary': '85',
    }
    raw = ''
    profile_limits = (os.environ.get('NLM_PRUNE_MAX_COUNT_BY_PROFILE') or '').strip()
    if profile_limits:
        for item in profile_limits.split(','):
            name, sep, value = item.partition(':')
            if sep and name.strip() == profile:
                raw = value.strip()
                break
    if not raw:
        raw = (os.environ.get('NLM_PRUNE_MAX_COUNT') or defaults.get(profile) or '85').strip()
    try:
        return max(1, int(raw))
    except ValueError:
        fallback = int(defaults.get(profile) or '85')
        print(f"⚠️ NLM prune 阈值无效，profile={profile} 回落到 {fallback}: {raw}")
        return fallback


def prune_oldest_notebook_before_create(notebook_title: str) -> bool:
    profile = current_nlm_profile()
    if not should_prune_notebook_before_create(profile):
        return True

    notebooks = list_nlm_notebooks()
    if notebooks is None:
        return False
    limit = nlm_prune_max_count_for_profile(profile)
    if len(notebooks) < limit:
        print(f"🧹 NotebookLM prune: profile={profile} 当前 {len(notebooks)}/{limit}，未到阈值，跳过")
        return True
    if not notebooks:
        print(f"🧹 NotebookLM prune: profile={profile} 当前无 notebook，跳过")
        return True

    notebooks.sort(key=lambda item: item.get('updated_at') or '')
    victim = notebooks[0]
    victim_id = (victim.get('id') or '').strip()
    victim_title = (victim.get('title') or '').strip()
    if not victim_id:
        print(f"❌ NotebookLM prune: 最旧 notebook 无 id，无法删除: {victim}")
        return False

    print(f"🧹 NotebookLM prune: profile={profile} 新建前删除最旧 notebook: {victim_title} -> {victim_id}")
    res = run_nlm_command(['notebook', 'delete', victim_id, '--confirm'])
    if res.returncode != 0:
        msg = (res.stderr or res.stdout or '').strip()
        print(f"❌ NotebookLM prune 删除失败: {msg[:500]}")
        return False
    print(f"✅ NotebookLM prune 完成: {victim_id}")
    return True


def create_or_reuse_notebook(notebook_title: str) -> Optional[str]:
    if not ensure_nlm_auth(allow_fallback=True):
        return None
    notebook_id = find_existing_notebook_id_by_title(notebook_title)
    if notebook_id:
        print(f"♻️ 命中已有 NotebookLM notebook: {notebook_title} -> {notebook_id}")
        return notebook_id
    if not prune_oldest_notebook_before_create(notebook_title):
        print('❌ 新建前 NotebookLM prune 未完成，取消创建 notebook')
        return None
    print(f"📒 创建 NotebookLM notebook: {notebook_title}")
    create_res = run_nlm_command(['notebook', 'create', notebook_title])
    if create_res.returncode != 0:
        reason = create_res.stderr.strip() or create_res.stdout.strip() or 'notebook_create_failed'
        print(f"❌ 创建 notebook 失败: {reason}")
        return None
    notebook_id = parse_notebook_id_from_create_output(create_res.stdout or '')
    if not notebook_id:
        print(f"❌ 无法解析 notebook ID: {create_res.stdout.strip()}")
        return None
    return notebook_id


def ensure_nlm_auth(allow_fallback: bool = False) -> bool:
    """每次进入 NotebookLM 流程前都先做 auth preflight；失效时自动重新 login。"""
    profile = current_nlm_profile()
    print(f'🔐 NotebookLM auth preflight: login --check (profile={profile})')
    check_res = run_nlm_command(['login', '--check'])
    if not nlm_auth_check_failed(check_res):
        print('✅ NotebookLM auth preflight 通过')
        return True

    if allow_fallback and can_fallback_nlm_profile(profile):
        if activate_nlm_profile_fallback('auth_preflight_failed', from_profile=profile):
            return True

    if refresh_nlm_profile_from_cdp(profile):
        return True

    print('🔐 NotebookLM 认证失效，自动重新登录...')
    login_res = run_nlm_command(['login'])
    if nlm_login_failed(login_res):
        if nlm_login_needs_chrome_restart(login_res):
            cleanup_nlm_profile_chrome(current_nlm_profile())
            print('🔐 NotebookLM 自动登录重试...')
            login_res = run_nlm_command(['login'])
        if nlm_login_failed(login_res):
            if allow_fallback and can_fallback_nlm_profile(profile):
                if activate_nlm_profile_fallback('auto_login_failed', from_profile=profile):
                    return True
            print(f"❌ nlm login 失败: {(login_res.stderr or login_res.stdout or '').strip()[:800]}")
            return False

    print(f'🔐 NotebookLM auth preflight: 重新校验 login --check (profile={current_nlm_profile()})')
    check_res = run_nlm_command(['login', '--check'])
    if nlm_auth_check_failed(check_res):
        if allow_fallback and can_fallback_nlm_profile(profile):
            if activate_nlm_profile_fallback('auth_recheck_failed', from_profile=profile):
                return True
        print(f"❌ 重新登录后校验仍失败: {check_res.stderr.strip() or check_res.stdout.strip()}")
        return False

    print('✅ NotebookLM 认证恢复')
    return True


def java_string_hash(text: str) -> str:
    h = 0
    for ch in text:
        h = (31 * h + ord(ch)) & 0xFFFFFFFF
    if h >= 2**31:
        h -= 2**32
    return str(h)


def clean_notebooklm_answer(answer: str) -> str:
    """去掉 NotebookLM 引用标记，保留 Markdown 结构（含粗体）。"""
    clean = answer or ''
    # 支持 [1] / [2, 3] / [21, 23-25] / [4-6, 8, 10-12]
    clean = re.sub(r'\[(?:\d+(?:\s*-\s*\d+)?)(?:\s*,\s*\d+(?:\s*-\s*\d+)?)*\]', '', clean)
    clean = re.sub(r'\s+([，。！？：；])', r'\1', clean)
    clean = re.sub(r' {2,}', ' ', clean)
    clean = re.sub(r'\n{3,}', '\n\n', clean)
    return clean.strip() + '\n'


def normalize_shownotes_text(raw: str) -> str:
    text = raw or ''
    text = unescape(text)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.I)
    text = re.sub(r'</p\s*>', '\n\n', text, flags=re.I)
    text = re.sub(r'<p[^>]*>', '', text, flags=re.I)
    text = re.sub(r'<div[^>]*>', '', text, flags=re.I)
    text = re.sub(r'</div\s*>', '\n', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('\u00a0', ' ')
    text = re.sub(r'\r', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]+\n', '\n', text)
    text = re.sub(r'\n[ \t]+', '\n', text)
    return text.strip()


def fix_shownotes_bold_quotes(text: str) -> str:
    text = text or ''
    opening_quotes = '"\u2018\u300a\u3008\u300c\u300e'
    closing_quotes = '"\u2019\u300b\u3009\u300d\u300f'
    re_bold_open = re.compile(r'\*\*([' + opening_quotes + r'])')
    re_close_bold = re.compile(r'(?<=[^\*\s])([' + closing_quotes + r'])\*\*')

    for _ in range(5):
        changed = False

        text, n = re.subn(r'\*{4,}', '', text)
        if n:
            changed = True

        text, n = re_bold_open.subn(r'\1**', text)
        if n:
            changed = True

        text, n = re_close_bold.subn(r'**\1', text)
        if n:
            changed = True

        if not changed:
            break

    return text


def _extract_shownotes_blocks(raw: str) -> list[str]:
    raw = (raw or '').strip()
    if not raw:
        return []
    blocks = []
    pattern = r'(<blockquote\b.*?</blockquote>)|(<figure\b.*?</figure>)|(<h[1-6]\b.*?</h[1-6]>)|(<p\b.*?</p>)|(<ul\b.*?</ul>)|(<ol\b.*?</ol>)'
    for m in re.finditer(pattern, raw, flags=re.S | re.I):
        blocks.append(m.group(0))
    if blocks:
        return blocks
    normalized = unescape(raw).replace('\u00a0', ' ')
    return [line.strip() for line in normalized.splitlines() if line.strip()]


def _block_contains_timeline_lines(block: str) -> bool:
    text = normalize_shownotes_text(block or '')
    if not text:
        return False
    count = 0
    for line in text.splitlines():
        if _timeline_start_match(line.strip()):
            count += 1
    return count >= 2


def _block_is_timeline_entry(block: str) -> bool:
    text = normalize_shownotes_text(block or '')
    if not text:
        return False
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    return all(_timeline_start_match(line) for line in lines)


def _is_timeline_label_text(text: str) -> bool:
    plain = (text or '').strip().strip('/')
    if plain in ['主要话题', 'timeline', '时间戳', '【时间轴】', '【Timeline】']:
        return True
    return bool(re.fullmatch(r'/?timeline/?', plain, flags=re.I))


def _is_section_heading_text(text: str) -> bool:
    plain = (text or '').strip().strip('/')
    if not plain or len(plain) > 80:
        return False
    if re.fullmatch(r'[【\[].+?[】\]]', plain):
        return True
    if re.match(r'^[\U0001F300-\U0001FAFF][^\d]{0,60}$', plain):
        return True
    return False


def _line_starts_with_timestamp(text: str) -> Optional[re.Match[str]]:
    return _timeline_start_match(normalize_shownotes_text(text or ''))


def _has_following_increasing_timeline_context(blocks: list[str], start_idx: int, required_entries: int = 2, lookahead: int = 4) -> bool:
    """单行时间戳开局时，要求后续短窗口内还能看到递增时间戳，避免把赞助/正文里的孤立时间误判成 timeline。"""
    if start_idx < 0 or start_idx >= len(blocks):
        return False

    count = 0
    last_seconds = None
    examined = 0
    non_timestamp_bridges = 0
    for idx in range(start_idx, len(blocks)):
        block = blocks[idx]
        plain = normalize_shownotes_text(block).strip()
        if not plain:
            continue
        if idx > start_idx and (_is_appendix_start_text(plain) or _timeline_blacklist_hit(block)):
            break

        seq_entries = _extract_first_increasing_timeline_entries(block)
        if seq_entries:
            for entry in seq_entries:
                seconds = _timestamp_to_seconds(entry.get('time') or '')
                if seconds is None:
                    continue
                if last_seconds is not None and seconds <= last_seconds:
                    return count >= required_entries
                count += 1
                last_seconds = seconds
                if count >= required_entries:
                    return True
            examined += 1
            continue

        m = _line_starts_with_timestamp(plain)
        if m:
            seconds = _timestamp_to_seconds(m.group(1))
            if seconds is not None:
                if last_seconds is not None and seconds <= last_seconds:
                    return count >= required_entries
                count += 1
                last_seconds = seconds
                if count >= required_entries:
                    return True
            examined += 1
            continue

        if idx > start_idx and _parse_chapter_heading(plain, block):
            break

        if idx > start_idx:
            non_timestamp_bridges += 1
            if non_timestamp_bridges > lookahead:
                break

        if examined >= lookahead:
            break

    return False


def _block_timeline_entries(block: str) -> list[dict]:
    seq_entries = _extract_first_increasing_timeline_entries(block)
    if seq_entries:
        return seq_entries
    plain = normalize_shownotes_text(block or '').strip()
    m = _line_starts_with_timestamp(plain)
    if not m:
        return []
    return [{'time': m.group(1), 'plain_line': plain, 'markdown_line': plain}]



def _collect_increasing_timeline_run(blocks: list[str], start_idx: int, min_entries: int = 2, bridge_lookahead: int = 4) -> tuple[int, int, int]:
    start = -1
    end = -1
    entry_count = 0
    last_seconds = None
    idx = start_idx

    def find_future_timestamp(next_start: int, section_scan_limit: int = 24) -> tuple[bool, bool]:
        encountered_heading = False
        max_idx = min(len(blocks), next_start + section_scan_limit)
        for next_idx in range(next_start, max_idx):
            future_block = blocks[next_idx]
            future_plain = normalize_shownotes_text(future_block).strip()
            if not future_plain:
                continue
            if _is_appendix_start_text(future_plain) or _timeline_blacklist_hit(future_block):
                break
            if next_idx > next_start and _parse_chapter_heading(future_plain, future_block):
                encountered_heading = True
            future_entries = _block_timeline_entries(future_block)
            if not future_entries:
                continue
            future_seconds = _timestamp_to_seconds(future_entries[0].get('time') or '')
            if future_seconds is None:
                continue
            if last_seconds is not None and future_seconds < last_seconds:
                return False, encountered_heading
            return True, encountered_heading
        return False, encountered_heading

    while idx < len(blocks):
        block = blocks[idx]
        plain = normalize_shownotes_text(block).strip()
        if not plain:
            idx += 1
            continue

        entries = _block_timeline_entries(block)
        if entries:
            for entry in entries:
                seconds = _timestamp_to_seconds(entry.get('time') or '')
                if seconds is None:
                    continue
                if last_seconds is not None and seconds < last_seconds:
                    return start, end, entry_count
                if start < 0:
                    start = idx
                end = idx
                last_seconds = seconds
                entry_count += 1
            idx += 1
            continue

        if start < 0:
            break
        if _is_appendix_start_text(plain) or _timeline_blacklist_hit(block):
            break

        continued, crossed_section = find_future_timestamp(idx + 1)
        if continued:
            end = idx
            idx += 1
            continue
        if crossed_section:
            break

        short_continued = False
        for next_idx in range(idx + 1, min(len(blocks), idx + 1 + bridge_lookahead)):
            future_entries = _block_timeline_entries(blocks[next_idx])
            if not future_entries:
                continue
            future_seconds = _timestamp_to_seconds(future_entries[0].get('time') or '')
            if future_seconds is None:
                continue
            if last_seconds is not None and future_seconds < last_seconds:
                return start, end, entry_count
            short_continued = True
            break

        if short_continued:
            end = idx
            idx += 1
            continue
        break

    return start, end, entry_count



def _find_first_timeline_cluster_bounds(raw: str, timeline_outline: Optional[list[dict]] = None) -> tuple[int, int]:
    blocks = _extract_shownotes_blocks(raw)
    if not blocks:
        return -1, -1

    def expand_start(start: int) -> int:
        while start > 0:
            prev_block = blocks[start - 1]
            prev_plain = normalize_shownotes_text(prev_block).strip()
            if not prev_plain:
                start -= 1
                continue
            if _is_timeline_label_text(prev_plain) or _parse_chapter_heading(prev_plain, prev_block):
                start -= 1
                continue
            break
        return start

    best = (-1, -1, 0)
    for idx, block in enumerate(blocks):
        if not _block_timeline_entries(block):
            continue
        start, end, entry_count = _collect_increasing_timeline_run(blocks, idx)
        if start >= 0:
            start = expand_start(start)
        if entry_count >= 2:
            return start, end
        if entry_count > best[2]:
            best = (start, end, entry_count)

    if best[2] > 0:
        return best[0], best[1]
    return -1, -1


def _find_appendix_start_block_index(raw: str, timeline_outline: Optional[list[dict]] = None) -> int:
    blocks = _extract_shownotes_blocks(raw)
    if not blocks:
        return -1

    cluster_start_idx, cluster_end_idx = _find_first_timeline_cluster_bounds(raw, timeline_outline)
    if cluster_end_idx >= 0:
        return cluster_end_idx + 1 if cluster_end_idx + 1 < len(blocks) else -1

    outline = timeline_outline or extract_timeline_outline(raw)
    if outline:
        end_idx = outline[-1].get('_timeline_end_block_index', -1)
        if end_idx >= 0:
            return end_idx + 1 if end_idx + 1 < len(blocks) else -1

    last_timeline_like_idx = -1
    for idx, block in enumerate(blocks):
        if _block_contains_timeline_lines(block):
            last_timeline_like_idx = idx

    if last_timeline_like_idx < 0:
        return -1

    for idx in range(last_timeline_like_idx + 1, len(blocks)):
        plain = normalize_shownotes_text(blocks[idx]).strip().strip('/')
        has_img = bool(re.search(r'<img[^>]+src=["\']([^"\']+)["\']', blocks[idx], flags=re.I))
        if has_img:
            return idx
        if not plain:
            continue
        if plain in ['主要话题', 'timeline', '时间戳']:
            continue
        if re.fullmatch(r'/?timeline/?', plain, flags=re.I):
            continue
        return idx

    return -1


def _html_inline_to_markdown(text: str) -> str:
    text = text or ''
    text = text.replace('\u00a0', ' ')
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.I)

    def _img_sub(m):
        src = (m.group(1) or '').strip()
        return f'![]({src})\n' if src else ''

    text = re.sub(r'<img[^>]+src=["\']([^"\']+)["\'][^>]*>', _img_sub, text, flags=re.I)

    prev = None
    while text != prev:
        prev = text
        text = re.sub(
            r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
            lambda m: _anchor_to_markdown(m.group(1), m.group(2)),
            text,
            flags=re.I | re.S,
        )
        text = re.sub(r'<(?:strong|b)[^>]*>(.*?)</(?:strong|b)>', lambda m: f'**{_html_inline_to_markdown(m.group(1)).strip()}**', text, flags=re.I | re.S)
        text = re.sub(r'<(?:em|i)[^>]*>(.*?)</(?:em|i)>', lambda m: f'*{_html_inline_to_markdown(m.group(1)).strip()}*', text, flags=re.I | re.S)
        text = re.sub(r'<code[^>]*>(.*?)</code>', lambda m: f'`{normalize_shownotes_text(m.group(1)).strip()}`', text, flags=re.I | re.S)

    text = re.sub(r'</?(?:span|div|p|figure|figcaption|blockquote)[^>]*>', '', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    text = unescape(text)
    text = re.sub(r'\r', '', text)
    text = re.sub(r'(!\[\]\([^\)]+\))(\S)', r'\1\n\2', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]+\n', '\n', text)
    text = re.sub(r'\n[ \t]+', '\n', text)
    return fix_shownotes_bold_quotes(text.strip())


def _anchor_to_markdown(href: str, inner_html: str) -> str:
    href = (href or '').strip()
    inner = _html_inline_to_markdown(inner_html or '').strip()
    if not href:
        return inner
    if not inner and (
        re.search(r'\.(?:png|jpe?g|webp|gif)(?:$|[?#])', href, flags=re.I)
        or 'bts-image.xyzcdn.net/' in href
        or 'image.xyzcdn.net/' in href
    ):
        return f'![]({href})'
    if not inner:
        return href
    if inner.startswith('![]('):
        return inner
    return f'[{inner}]({href})'


def _shownotes_block_to_markdown(block: str) -> str:
    block = (block or '').strip()
    if not block:
        return ''
    lower = block.lower()
    if '<blockquote' in lower:
        body = _html_inline_to_markdown(re.sub(r'^<blockquote[^>]*>|</blockquote>$', '', block, flags=re.I | re.S)).strip()
        if not body:
            return ''
        return '\n'.join(f'> {line}' if line.strip() else '>' for line in body.splitlines())
    if '<ul' in lower or '<ol' in lower:
        items = []
        for li in re.findall(r'<li[^>]*>(.*?)</li>', block, flags=re.I | re.S):
            item = _html_inline_to_markdown(li).strip()
            if item:
                item = item.replace('\n', '\n  ')
                items.append(f'- {item}')
        return '\n'.join(items).strip()
    return _html_inline_to_markdown(block)


def _shownotes_block_to_markdown_for_route(block: str, route: Optional[dict] = None) -> str:
    route = route or {}
    block = (block or '').strip()
    if not block:
        return ''

    if route.get('frontmatter_preset') == 'history' and '<blockquote' in block.lower():
        inner = re.sub(r'^<blockquote[^>]*>|</blockquote>$', '', block, flags=re.I | re.S).strip()
        if not inner:
            return ''

        paragraphs = re.findall(r'<p\b[^>]*>(.*?)</p>', inner, flags=re.I | re.S)
        if paragraphs:
            parts = []
            for para in paragraphs:
                md = _html_inline_to_markdown(para).strip()
                if not md or md == '!':
                    continue
                parts.append(md)
            return '\n\n'.join(parts).strip()

        body = _html_inline_to_markdown(inner).strip()
        if not body:
            return ''
        lines = []
        for line in body.splitlines():
            line = line.strip()
            if not line or line == '!':
                continue
            lines.append(line)
        return '\n\n'.join(lines).strip()

    return _shownotes_block_to_markdown(block)


def _split_sentences_to_bullets(text: str) -> list[str]:
    text = (text or '').strip()
    if not text:
        return []
    text = text.replace('\n', ' ').strip()
    parts = re.split(r'(?<=[。！？!?])\s*', text)
    bullets = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        bullets.append(f'- {part}')
    return bullets



def _extract_preface_lines(info: Optional[dict]) -> list[str]:
    info = info or {}
    raw = (info.get('shownotes') or info.get('description') or '').strip()
    if not raw:
        return []
    route = get_podcast_route(info.get('podcast_name', ''))

    blocks = _extract_shownotes_blocks(raw)
    if not blocks:
        return []

    timeline_outline = info.get('timeline_outline') or extract_timeline_outline(raw)
    start_idx = timeline_outline[0].get('_timeline_start_block_index', -1) if timeline_outline else -1
    appendix_start_idx = _find_appendix_start_block_index(raw, timeline_outline)

    if start_idx > 0:
        pre_blocks = blocks[:start_idx]
    elif appendix_start_idx > 0:
        pre_blocks = blocks[:appendix_start_idx]
    elif not timeline_outline:
        pre_blocks = blocks
    else:
        pre_blocks = []

    lines = []
    for block in pre_blocks:
        md = _shownotes_block_to_markdown_for_route(block, route=route).strip().strip('/')
        if not md:
            continue
        if md in ['主要话题', 'timeline', '时间戳']:
            continue
        if re.fullmatch(r'/?timeline/?', md, flags=re.I):
            continue
        lines.append(md)
    return lines



def _split_research_preface_sections(lines: list[str]) -> tuple[list[str], list[str]]:
    body_lines = []
    ref_lines = []
    in_references = False

    for line in lines or []:
        stripped = (line or '').strip()
        if not stripped:
            continue
        lower = stripped.lower().rstrip(':：')
        if lower in {'references', 'reference'}:
            in_references = True
            ref_lines.append(stripped)
            continue
        if in_references:
            ref_lines.append(stripped)
            continue
        body_lines.append(stripped)
    return body_lines, ref_lines



def _format_research_body_bullets(lines: list[str]) -> str:
    formatted = []
    for line in lines or []:
        bullets = _split_sentences_to_bullets(line)
        if bullets:
            formatted.extend(bullets)
        elif line:
            formatted.append(f'- {line}')
    return '\n'.join(formatted).strip()



def _extract_episode_number_token(title: str) -> str:
    title = (title or '').strip()
    if not title:
        return ''
    episode, _ = extract_episode_and_title(title)
    return (episode or '').strip()



def _load_companion_episode_info(info: Optional[dict]) -> Optional[dict]:
    info = info or {}
    route = get_podcast_route(info.get('podcast_name', ''))
    if not route.get('merge_companion_shownotes'):
        return None
    if info.get('_companion_episode_checked'):
        return info.get('companion_episode_info')

    info['_companion_episode_checked'] = True
    apple_show_url = (route.get('companion_apple_show_url') or '').strip()
    episode_token = _extract_episode_number_token(info.get('title', ''))
    if not apple_show_url or not episode_token:
        return None

    try:
        ep = _find_episode_from_apple_show_by_number(apple_show_url, episode_token)
        if not ep:
            return None
        match_url = (ep.get('url') or '').strip()
        companion_info = {
            'title': normalize_wrapped_label((ep.get('title') or '').strip()),
            'description': ep.get('description') or '',
            'shownotes': ep.get('description') or '',
            'episode_url': match_url,
            'podcast_name': route.get('companion_podcast_name') or '',
            'pub_date': ep.get('pub_date') or '',
            'episode_id': ep.get('eid') or '',
        }
        if match_url:
            companion_html = fetch_page(match_url)
            richer = extract_episode_info(companion_html, match_url)
            if richer:
                companion_info.update(richer)
        info['companion_episode_info'] = companion_info
        return companion_info
    except Exception:
        return None



def _extract_shownotes_preface_markdown(info: Optional[dict]) -> str:
    info = info or {}
    lines = _extract_preface_lines(info)
    if not lines:
        return ''
    route = get_podcast_route(info.get('podcast_name', ''))
    if route.get('frontmatter_preset') == 'research':
        body_lines, ref_lines = _split_research_preface_sections(lines)
        parts = []
        body = _format_research_body_bullets(body_lines)
        if body:
            parts.append(body)

        companion_info = _load_companion_episode_info(info)
        companion_lines = _extract_preface_lines(companion_info)
        companion_body_lines, companion_ref_lines = _split_research_preface_sections(companion_lines)
        companion_body = _format_research_body_bullets(companion_body_lines)
        if companion_body:
            parts.append(companion_body)

        refs = ref_lines or companion_ref_lines
        if refs:
            parts.append('\n'.join(refs))
        return '\n\n'.join(part for part in parts if part and part.strip())
    if route.get('frontmatter_preset') == 'history':
        return '\n\n'.join(lines)
    if route.get('frontmatter_preset') == 'poem' or route.get('skip_notebooklm'):
        return '\n\n'.join(lines)
    return '## Shownotes\n\n' + '\n\n'.join(lines)


def extract_post_timeline_shownotes_markdown(info: Optional[dict]) -> str:
    """从时间线结束后的首个非时间线块开始，结构保真复制；遇到首条黑名单即截断。"""
    info = info or {}
    shownotes = (info.get('shownotes') or '').strip()
    if not shownotes:
        return ''
    route = get_podcast_route(info.get('podcast_name', ''))

    blocks = _extract_shownotes_blocks(shownotes)
    if not blocks:
        return ''

    timeline_outline = info.get('timeline_outline') or extract_timeline_outline(shownotes)
    start_idx = _find_appendix_start_block_index(shownotes, timeline_outline)
    if start_idx < 0:
        return ''

    blacklist = [
        '收听平台', '关注我们', '商务合作', '商业合作', '联系我们', '加入我们',
        '品牌官网', '来填写问卷吧',
        '投稿入口', '了解更多', 'bbpark@', 'business@', 'kexuan@', 'ting@',
        '小宇宙｜', '喜马拉雅｜', '苹果播客｜', 'Spotify｜', 'YouTube',
        '年度热门播客', '品牌青睐播客', '年度最佳播客', '年度语言播客', '年度品质播客',
        '日光派对', '这些年，我们曾获得过以下这些荣誉',
        '我们还有这些播客',
        'Knock Knock 世界', 'Honghub', '鸿鹄汇',
        '听友群', '加群',
        '【节目制作】', '【Logo设计】', '【互动方式】',
    ]

    lines = []
    for block in blocks[start_idx:]:
        md = _shownotes_block_to_markdown_for_route(block, route=route).strip().strip('/')
        plain = normalize_shownotes_text(block).strip().strip('/')
        if plain and any(bad in plain for bad in blacklist):
            continue
        if plain and (_is_appendix_start_text(plain) or plain in ['主要话题', 'timeline', '时间戳']):
            continue
        if re.fullmatch(r'/?timeline/?', plain, flags=re.I):
            continue
        if not md:
            continue
        lines.append(md)

    if not lines:
        return ''
    return '## 补充 Shownotes\n\n' + '\n\n'.join(lines)


def build_fast_note_markdown(answer: str, title: str, episode_url: str = '', summary: str = '', info: Optional[dict] = None) -> str:
    parts = []
    info = dict(info or {})
    safe_title = (title or '').strip()
    podcast_name = (info.get('podcast_name') or '').strip()
    parts.append(build_note_frontmatter(podcast_name, safe_title, episode_url, info=info))

    episode_cover_url = (info.get('episode_cover_url') or '').strip()
    podcast_cover_url = (info.get('podcast_cover_url') or '').strip()
    cover_url = episode_cover_url or podcast_cover_url
    if cover_url:
        parts.append(f'![]({cover_url})')

    summary = (summary or '').strip()
    if summary:
        parts.append('> ' + summary)

    preface = _extract_shownotes_preface_markdown(info)
    if preface.strip():
        parts.append(preface.strip())

    clean = clean_notebooklm_answer(answer).strip()
    timeline_outline = info.get('timeline_outline') or []
    if not timeline_outline:
        raw_timeline_source = (info.get('shownotes') or info.get('description') or '').strip()
        if raw_timeline_source:
            timeline_outline = extract_timeline_outline(raw_timeline_source)
            if timeline_outline:
                info['timeline_outline'] = timeline_outline
                info['timeline_titles'] = [item.get('title', '') for item in timeline_outline]
    clean = inject_timeline_overflow(clean, timeline_outline).strip() if clean else clean
    if clean:
        parts.append(clean)

    appendix = extract_post_timeline_shownotes_markdown(info)
    if appendix.strip():
        parts.append(appendix.strip())

    return '\n\n'.join(parts).strip() + '\n'


def backup_file_once(src_path: str) -> Optional[str]:
    if os.environ.get('FAST_NOTE_ENABLE_SQLITE_BACKUP') != '1':
        return None

    src = Path(src_path)
    if not src.exists():
        return None
    backup_dir = Path(FAST_NOTE_BACKUP_DIR)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    dest = backup_dir / f"{src.name}.{stamp}.bak"
    shutil.copy2(src, dest)
    return str(dest)


def normalize_fast_note_segment(name: str) -> str:
    return sanitize_filename((name or '').strip())


def ensure_fast_note_folder(folder_path: str) -> int:
    """确保 Fast Note 文件夹路径存在，返回最终 folder id。"""
    parts = [normalize_fast_note_segment(p) for p in folder_path.split('/') if p.strip()]
    if not parts:
        raise ValueError('folder_path 不能为空')

    now_ms = int(time.time() * 1000)
    now_dt = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    con = sqlite3.connect(FAST_NOTE_FOLDER_DB)
    cur = con.cursor()
    parent_id = 0
    current_path = ''

    try:
        for level, part in enumerate(parts, start=1):
            current_path = f'{current_path}/{part}' if current_path else part
            cur.execute(
                'select id from folder where vault_id=1 and path=? order by id desc limit 1',
                (current_path,),
            )
            row = cur.fetchone()
            if row:
                parent_id = int(row[0])
                continue

            cur.execute(
                "insert into folder (vault_id, action, path, path_hash, level, fid, ctime, mtime, updated_timestamp, created_at, updated_at) values (1, 'create', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (current_path, java_string_hash(current_path), level, parent_id, now_ms, now_ms, now_ms, now_dt, now_dt),
            )
            parent_id = int(cur.lastrowid)

        con.commit()
        return parent_id
    finally:
        con.close()


def upsert_fast_note_markdown(folder_path: str, note_title: str, content: str) -> dict:
    """将 Markdown 内容写入 Fast Note / Obsidian 存储，并写成 Fast Note UI 稳定可见的正规化形态。"""
    folder_backup = backup_file_once(FAST_NOTE_FOLDER_DB)
    note_backup = backup_file_once(FAST_NOTE_NOTE_DB)
    note_history_backup = backup_file_once(FAST_NOTE_NOTE_HISTORY_DB)

    folder_path = '/'.join(normalize_fast_note_segment(seg) for seg in folder_path.split('/') if seg.strip())
    folder_id = ensure_fast_note_folder(folder_path)
    safe_title = normalize_fast_note_segment(note_title)
    note_path = f'{folder_path}/{safe_title}.md'
    now_ms = int(time.time() * 1000)
    now_dt = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    path_hash = java_string_hash(note_path)
    size = len(content.encode('utf-8'))
    content_hash = java_string_hash(content)

    def write_note_files(note_id: int) -> None:
        note_dir = Path(FAST_NOTE_VAULT_ROOT) / f'n_{note_id}'
        note_dir.mkdir(parents=True, exist_ok=True)
        (note_dir / 'content.txt').write_text(content, encoding='utf-8')
        (note_dir / 'snapshot.txt').write_text(content, encoding='utf-8')

    def append_note_history(note_id: int, version: int, created_at: str) -> None:
        hist_con = sqlite3.connect(FAST_NOTE_NOTE_HISTORY_DB)
        hist_cur = hist_con.cursor()
        hist_cur.execute(
            "insert into note_history (note_id, vault_id, path, content, content_hash, diff_patch, client_name, version, created_at, updated_at) values (?, 1, ?, '', ?, '', ?, ?, ?, NULL)",
            (note_id, note_path, content_hash, FAST_NOTE_CLIENT_NAME, version, created_at),
        )
        hist_con.commit()
        hist_con.close()

    con = sqlite3.connect(FAST_NOTE_NOTE_DB)
    cur = con.cursor()
    cur.execute(
        'select id, version, ctime, created_at from note where vault_id=1 and path=? and rename=0 order by id desc limit 1',
        (note_path,),
    )
    row = cur.fetchone()
    created_new_note = False

    if row:
        note_id = int(row[0])
        previous_version = int(row[1] or 0)
        version = max(previous_version, 1) + 1
        ctime = int(row[2] or now_ms)
        created_at = row[3] or now_dt
        write_note_files(note_id)
        cur.execute(
            "update note set action='modify', fid=?, path_hash=?, content='', content_hash=?, content_last_snapshot='', content_last_snapshot_hash=?, version=?, client_name=?, size=?, mtime=?, updated_timestamp=?, updated_at=? where id=?",
            (folder_id, path_hash, content_hash, content_hash, version, FAST_NOTE_CLIENT_NAME, size, now_ms, now_ms, now_dt, note_id),
        )
    else:
        created_new_note = True
        version = 1
        ctime = now_ms
        created_at = now_dt
        cur.execute(
            "insert into note (vault_id, action, rename, fid, path, path_hash, content, content_hash, content_last_snapshot, content_last_snapshot_hash, version, client_name, size, ctime, mtime, updated_timestamp, created_at, updated_at) values (1, 'modify', 0, ?, ?, ?, '', ?, '', ?, 1, ?, ?, ?, ?, ?, ?, ?)",
            (folder_id, note_path, path_hash, content_hash, content_hash, FAST_NOTE_CLIENT_NAME, size, ctime, now_ms, now_ms, created_at, now_dt),
        )
        note_id = int(cur.lastrowid)
        write_note_files(note_id)

    cur.execute('delete from note_fts where note_id=?', (note_id,))
    cur.execute('insert into note_fts(note_id, path, content) values (?, ?, ?)', (note_id, note_path, content))
    con.commit()
    con.close()

    append_note_history(note_id, version, now_dt)

    normalized_second_pass = True
    second_now_ms = int(time.time() * 1000)
    second_now_dt = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    second_version = max(version, 1) + 1
    write_note_files(note_id)

    con = sqlite3.connect(FAST_NOTE_NOTE_DB)
    cur = con.cursor()
    cur.execute(
        "update note set action='modify', rename=0, fid=?, path_hash=?, content='', content_hash=?, content_last_snapshot='', content_last_snapshot_hash=?, version=?, client_name=?, size=?, mtime=?, updated_timestamp=?, updated_at=? where id=?",
        (folder_id, path_hash, content_hash, content_hash, second_version, FAST_NOTE_CLIENT_NAME, size, second_now_ms, second_now_ms, second_now_dt, note_id),
    )
    cur.execute('delete from note_fts where note_id=?', (note_id,))
    cur.execute('insert into note_fts(note_id, path, content) values (?, ?, ?)', (note_id, note_path, content))
    con.commit()
    con.close()

    append_note_history(note_id, second_version, second_now_dt)
    version = second_version

    verified_action = 'unknown'
    con = sqlite3.connect(FAST_NOTE_NOTE_DB)
    cur = con.cursor()
    cur.execute('select action, version from note where id=?', (note_id,))
    verify_row = cur.fetchone()
    if verify_row:
        verified_action = str(verify_row[0] or '')
        current_version = int(verify_row[1] or version or 0)
        if verified_action != 'modify':
            fix_now_ms = int(time.time() * 1000)
            fix_now_dt = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            fix_version = max(current_version, version, 1) + 1
            write_note_files(note_id)
            cur.execute(
                "update note set action='modify', rename=0, fid=?, path_hash=?, content='', content_hash=?, content_last_snapshot='', content_last_snapshot_hash=?, version=?, client_name=?, size=?, mtime=?, updated_timestamp=?, updated_at=? where id=?",
                (folder_id, path_hash, content_hash, content_hash, fix_version, FAST_NOTE_CLIENT_NAME, size, fix_now_ms, fix_now_ms, fix_now_dt, note_id),
            )
            cur.execute('delete from note_fts where note_id=?', (note_id,))
            cur.execute('insert into note_fts(note_id, path, content) values (?, ?, ?)', (note_id, note_path, content))
            con.commit()
            append_note_history(note_id, fix_version, fix_now_dt)
            version = fix_version
            verified_action = 'modify'
    con.close()

    return {
        'note_id': note_id,
        'note_path': note_path,
        'folder_id': folder_id,
        'note_backup': note_backup,
        'note_history_backup': note_history_backup,
        'folder_backup': folder_backup,
        'normalized_second_pass': normalized_second_pass,
        'final_version': version,
        'verified_action': verified_action,
    }


def normalize_news_output_title(podcast_name: str, title: str) -> str:
    podcast_name = (podcast_name or '').strip()
    title = (title or '').strip()
    route = get_podcast_route(podcast_name)
    if not route.get('title_date_prefix'):
        return title
    if '｜' in title:
        title = title.split('｜', 1)[1].strip()
    if route.get('keep_issue_in_news_title'):
        title = re.sub(r'^((?:[Vv]ol\.??\s*\d+))\s*[.、，：:\s丨｜|]\s*', r'\1 ', title)
    title = re.sub(r'^\d{4}\s+', '', title)
    title = re.sub(r'^\d+\s*[-—–]\s*', '', title)
    if route.get('strip_leading_mmdd'):
        title = re.sub(r'^(?:0?[1-9]|1[0-2])[./-](?:0?[1-9]|[12]\d|3[01])\s+', '', title)
    return title.strip()



def format_release_date(pub_date: str = '') -> str:
    raw = (pub_date or '').strip()
    if not raw:
        return ''
    try:
        dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        return dt.strftime('%Y-%m-%d')
    except Exception:
        m = re.match(r'^(\d{4})-(\d{2})-(\d{2})', raw)
        if m:
            return '-'.join(m.groups())
    return ''



def get_news_date_prefix(pub_date: str = '') -> str:
    china_tz = ZoneInfo('Asia/Shanghai')
    raw = (pub_date or '').strip()
    if raw:
        try:
            dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            return dt.astimezone(china_tz).strftime('%Y%m%d')
        except Exception:
            m = re.match(r'^(\d{4})-(\d{2})-(\d{2})', raw)
            if m:
                return ''.join(m.groups())
    return datetime.now(china_tz).strftime('%Y%m%d')



def normalize_markdown_note_title(title: str) -> str:
    title = (title or '').strip()
    title = re.sub(r'^#\s*(\d+)\b', r'No. \1', title)
    return title.strip()


HEAD_PATTERNS = [
    (r'^[^\w]*?((?:[Ee][Pp]\.??\s*\d+(?:-\d+)?))\s*[.、，：:\s｜|]?\s*', 'ep'),
    (r'^((?:[Vv]ol\.??\s*\d+))\s*[.、，：:\s丨｜|]?\s*', 'vol'),
    (r'^((?:No\.??\s*\d+))\s*[.、，：:\s｜|]?\s*', 'no'),
    (r'^【[^】]*?(\d+)】\s*', 'bracket'),
    (r'^(第\d+[集期])\s*[.、，：:\s｜|]?\s*', 'di'),
    (r'^#(\d+)\s+', 'hash'),
    (r'^(国别\s*\d+)\s*[.、，：:\s｜|]?\s*', 'guobie'),
    (r'^([\u4e00-\u9fff]+\d+)\s*[.、，：:｜|]\s*', 'cn_series'),
    (r'^(\d+)\s*[-—–.、，：:\s｜|]+\s*', 'bare_num'),
]

TAIL_PATTERNS = [
    (r'\s+(S\d+E\d+)\s*$', 'season_ep'),
    (r'[|｜]?\s*E\.(\d+)\s*$', 'e_dot'),
    (r'[|｜丨]?\s*([Vv]ol\.?\d+)\s*\.?\s*$', 'vol_tail'),
    (r'[|｜]\s*[\u4e00-\u9fff]+(\d+)期\s*$', 'cn_tail'),
]


def extract_episode_and_title(raw_title: str) -> tuple[str, str]:
    title = (raw_title or '').strip()
    podcast_prefix_match = re.match(r'^[\u4e00-\u9fffa-zA-Z\s]+\s*-\s*(?=EP|Ep|ep|Vol|vol|#|\d|【|第)', title)
    if podcast_prefix_match:
        title = title[podcast_prefix_match.end():]

    for pattern, ptype in HEAD_PATTERNS:
        m = re.match(pattern, title)
        if m:
            episode = m.group(1).strip().rstrip('.')
            if ptype == 'hash':
                episode = f"No.{episode}"
            clean = title[m.end():].strip()
            return episode, clean

    for pattern, ptype in TAIL_PATTERNS:
        m = re.search(pattern, title)
        if m:
            if ptype == 'e_dot':
                episode = f"E.{m.group(1)}"
            elif ptype == 'cn_tail':
                episode = m.group(1)
            else:
                episode = m.group(1).strip()
            clean = title[:m.start()].strip()
            return episode, clean

    return '', title


def extract_filename_prefix_and_title(raw_title: str) -> tuple[str, str]:
    title = (raw_title or '').strip()

    head_specs = [
        (r'^[^\w]*?((?:[Ee][Pp]\.??\s*\d+(?:-\d+)?))\s*[.、，：:\s｜|]?\s*(.*)$', lambda m: m.group(1).strip().rstrip('.'), lambda m: m.group(2).strip()),
        (r'^((?:[Vv]ol\.??\s*\d+))\s*[.、，：:\s丨｜|]?\s*(.*)$', lambda m: m.group(1).strip().rstrip('.'), lambda m: m.group(2).strip()),
        (r'^((?:No\.??\s*\d+))\s*[.、，：:\s｜|]?\s*(.*)$', lambda m: m.group(1).strip().rstrip('.'), lambda m: m.group(2).strip()),
        (r'^【[^】]*?(\d+)】\s*(.*)$', lambda m: f'【{m.group(1).strip()}】', lambda m: m.group(2).strip()),
        (r'^(第\d+[集期])\s*[.、，：:\s｜|]?\s*(.*)$', lambda m: m.group(1).strip(), lambda m: m.group(2).strip()),
        (r'^#(\d+)\s+(.*)$', lambda m: f'No.{m.group(1).strip()}', lambda m: m.group(2).strip()),
        (r'^(国别\s*\d+)\s*[.、，：:\s｜|]?\s*(.*)$', lambda m: m.group(1).strip(), lambda m: m.group(2).strip()),
        (r'^([\u4e00-\u9fff]+\d+)\s*[.、，：:｜|]\s*(.*)$', lambda m: re.search(r'(\d+)$', m.group(1)).group(1) if re.search(r'(\d+)$', m.group(1)) else m.group(1).strip(), lambda m: m.group(2).strip()),
        (r'^(\d+期)\s*[.、，：:\s｜|]?\s*(.*)$', lambda m: m.group(1).strip(), lambda m: m.group(2).strip()),
        (r'^(\d+)\s*[-—–.、，：:\s｜|]+\s*(.*)$', lambda m: m.group(1).strip(), lambda m: m.group(2).strip()),
    ]
    for pattern, prefix_fn, title_fn in head_specs:
        m = re.match(pattern, title)
        if m:
            prefix = prefix_fn(m).strip()
            clean = title_fn(m).strip()
            if prefix and clean:
                return prefix, clean

    tail_specs = [
        (r'^(.*?)[|｜]?\s*(E\.\d+)\s*$', lambda m: m.group(2).strip(), lambda m: m.group(1).strip()),
        (r'^(.*?)[|｜丨]?\s*([Vv]ol\.?\d+)\s*\.?\s*$', lambda m: m.group(2).strip(), lambda m: m.group(1).strip()),
        (r'^(.*?)[|｜]\s*[\u4e00-\u9fff]+(\d+期)\s*$', lambda m: m.group(2).strip(), lambda m: m.group(1).strip()),
    ]
    for pattern, prefix_fn, title_fn in tail_specs:
        m = re.match(pattern, title)
        if m:
            prefix = prefix_fn(m).strip()
            clean = title_fn(m).strip(' ｜|')
            if prefix and clean:
                return prefix, clean

    return '', title


def make_tag_name(podcast_name: str) -> str:
    result = (podcast_name or '').strip()
    result = re.sub(r'[—–]+', '-', result)
    result = re.sub(r"[\s｜|''`：:，,·]", '-', result)
    result = re.sub(r'-+', '-', result)
    return result.strip('-')


def _yaml_single_quote(value: str) -> str:
    return (value or '').replace("'", "''")


def extract_poem_author(title: str) -> str:
    text = (title or '').strip()
    if not text:
        return ''
    _, clean_title = extract_episode_and_title(text)
    candidate = clean_title or text
    m = re.search(r'[—–-]{1,2}\s*([^—–-]+)\s*$', candidate)
    return (m.group(1).strip() if m else '')



def get_podcast_route(podcast_name: str) -> dict:
    return PODCAST_ROUTING.get((podcast_name or '').strip(), {})



def extract_primary_reference_text(info: Optional[dict] = None) -> str:
    info = info or {}
    raw = (info.get('shownotes') or info.get('description') or '').strip()
    if not raw:
        return ''

    items = re.findall(r'<li[^>]*>(.*?)</li>', raw, flags=re.S | re.I)
    for item in items:
        text = normalize_shownotes_text(item).strip()
        if text:
            return text.rstrip(' .')
    return ''



def strip_reference_authors(reference_text: str) -> str:
    text = normalize_shownotes_text(reference_text or '').strip().rstrip('.')
    if not text:
        return ''

    if '. ' in text:
        head, tail = text.split('. ', 1)
        if tail and (
            'et al' in head
            or ',' in head
            or re.search(r'\b[A-Z][a-z]+\s+[A-Z]\b', head)
            or re.search(r'\b[A-Z]\s*,', head)
        ):
            return tail.strip().rstrip('.')
    return text



def resolve_route_episode_and_clean_title(podcast_name: str, title: str, info: Optional[dict] = None) -> tuple[str, str]:
    episode, clean_title = extract_episode_and_title(title)
    route = get_podcast_route((podcast_name or '').strip())
    release_date = format_release_date((info or {}).get('pub_date', ''))
    if route.get('keep_issue_in_news_title'):
        episode = ''
        clean_title = normalize_news_output_title(podcast_name, title)
    if route.get('strip_leading_mmdd'):
        title_no_mmdd = re.sub(r'^(?:0?[1-9]|1[0-2])[./-](?:0?[1-9]|[12]\d|3[01])\s+', '', (title or '').strip()).strip()
        if title_no_mmdd:
            clean_title = title_no_mmdd
    if route.get('episode_from_release_date_compact') and release_date:
        episode = release_date.replace('-', '')
    return episode, clean_title or (title or '').strip()



def resolve_frontmatter_title_value(podcast_name: str, title: str, episode_url: str = '', info: Optional[dict] = None) -> str:
    _, clean_title = resolve_route_episode_and_clean_title(podcast_name, title, info=info)
    route = get_podcast_route(podcast_name)
    base_title = clean_title or title or ''
    if route.get('title_from_reference'):
        reference_text = strip_reference_authors(extract_primary_reference_text(info))
        if reference_text:
            base_title = reference_text
    return f'[{base_title}]({episode_url})' if base_title and episode_url else base_title



def build_note_frontmatter(podcast_name: str, title: str, episode_url: str = '', info: Optional[dict] = None) -> str:
    episode, clean_title = resolve_route_episode_and_clean_title(podcast_name, title, info=info)
    release_date = format_release_date((info or {}).get('pub_date', ''))
    lines = ['---']
    lines.append('tags:')
    
    podcast_name_clean = (podcast_name or '').strip()
    route = get_podcast_route(podcast_name_clean)
    tag_name = make_tag_name(podcast_name)
    title_value = resolve_frontmatter_title_value(podcast_name_clean, title, episode_url, info=info)
    if route.get('frontmatter_preset') == 'poem':
        lines.append('  - poem')
        if route.get('preserve_podcast_tag') and tag_name:
            lines.append(f'  - {tag_name}')
        author = extract_poem_author(title)
        if author:
            lines.append(f"author: '{_yaml_single_quote(author)}'")
        lines.append(f"title: '{_yaml_single_quote(title_value)}'")
        lines.append('---')
        return '\n'.join(lines)

    if route.get('frontmatter_preset') in {'research', 'history'}:
        lines.append(f"  - {route.get('primary_tag') or 'Research'}")
        if route.get('preserve_podcast_tag') and tag_name:
            lines.append(f'  - {tag_name}')
        for extra_tag in route.get('extra_tags') or []:
            if extra_tag:
                lines.append(f'  - {extra_tag}')
        lines.append(f"title: '{_yaml_single_quote(title_value)}'")
        lines.append('---')
        return '\n'.join(lines)

    main_tag = 'Newsletter' if route.get('fast_note_root') == 'Newsletters' else 'Podcast'
    lines.append(f'  - {main_tag}')
    
    if tag_name:
        lines.append(f'  - {tag_name}')
    for extra_tag in route.get('extra_tags') or []:
        if extra_tag:
            lines.append(f'  - {extra_tag}')
    lines.append(f'podcast: {podcast_name_clean}')
    
    if not episode and route.get('title_date_prefix') and info:
        episode = get_news_date_prefix(info.get('pub_date', ''))

    if episode:
        if re.match(r'^\d+$', episode):
            lines.append(f'episode: "{episode}"')
        else:
            lines.append(f'episode: {episode}')
    else:
        lines.append('episode: ""')
    lines.append(f"title: '{_yaml_single_quote(title_value)}'")
    if release_date:
        lines.append(f'Release Date: {release_date}')
    lines.append('---')
    return '\n'.join(lines)



def build_episode_notebook_title(podcast_name: str, title: str, pub_date: str = '') -> str:
    normalized_title = normalize_news_output_title(podcast_name, title)
    route = get_podcast_route(podcast_name)
    if route.get('keep_issue_in_news_title'):
        filename_prefix, clean_title = '', normalized_title
    else:
        filename_prefix, clean_title = extract_filename_prefix_and_title(normalized_title)
    normalized_title = normalize_markdown_note_title(clean_title or normalized_title)
    if route.get('title_date_prefix'):
        normalized_title = f"{get_news_date_prefix(pub_date)} {normalized_title}".strip()
    if filename_prefix:
        normalized_title = f"{filename_prefix} {normalized_title}".strip()
    return f"{sanitize_filename(podcast_name)} - {sanitize_filename(normalized_title)}"



def build_news_push_line(podcast_name: str, title: str) -> str:
    normalized_title = normalize_news_output_title(podcast_name, title)
    return f"{(podcast_name or '').strip()} - {normalized_title}".strip(' -')



def should_skip_infographics_for_podcast(podcast_name: str) -> bool:
    return bool(get_podcast_route(podcast_name).get('skip_infographic'))



def should_use_direct_markdown_for_podcast(podcast_name: str) -> bool:
    route = get_podcast_route(podcast_name)
    return bool(route.get('direct_markdown') or route.get('skip_notebooklm'))



def should_skip_drive_for_podcast(podcast_name: str) -> bool:
    return bool(get_podcast_route(podcast_name).get('skip_drive'))



def resolve_nlm_profile_for_podcast(podcast_name: str) -> str:
    podcast_name = (podcast_name or '').strip()
    route = get_podcast_route(podcast_name)
    preferred = (route.get('nlm_profile') or (os.environ.get('NLM_PROFILE') or NLM_DEFAULT_PROFILE).strip() or NLM_DEFAULT_PROFILE)
    return resolve_effective_nlm_profile(preferred)



def resolve_fast_note_root_for_podcast(podcast_name: str) -> str:
    podcast_name = (podcast_name or '').strip()
    route = get_podcast_route(podcast_name)
    return route.get('fast_note_root') or FAST_NOTE_DEFAULT_ROOT



def save_podcast_summary_to_fast_note(
    podcast_name: str,
    title: str,
    answer: str,
    episode_url: str = '',
    summary: str = '',
    info: Optional[dict] = None,
) -> dict:
    markdown = build_fast_note_markdown(answer, title=title, episode_url=episode_url, summary=summary, info=info)
    root_path = resolve_fast_note_root_for_podcast(podcast_name)
    folder_path = f'{root_path}/{normalize_fast_note_segment(podcast_name)}'
    note_title = build_episode_notebook_title(podcast_name, title, (info or {}).get('pub_date', ''))
    result = upsert_fast_note_markdown(folder_path, note_title, markdown)
    result['clean_answer'] = markdown
    return result



def save_direct_markdown_to_fast_note(
    podcast_name: str,
    title: str,
    episode_url: str = '',
    info: Optional[dict] = None,
) -> dict:
    markdown = build_fast_note_markdown('', title=title, episode_url=episode_url, summary='', info=info)
    root_path = resolve_fast_note_root_for_podcast(podcast_name)
    route = get_podcast_route(podcast_name)
    note_title_mode = route.get('note_title_mode')
    fallback_title = build_episode_notebook_title(podcast_name, title, (info or {}).get('pub_date', ''))
    plain_title = normalize_markdown_note_title((title or '').strip()) or fallback_title
    if note_title_mode == 'title_only':
        folder_path = root_path
        note_title = plain_title
    elif note_title_mode == 'title_only_in_podcast_folder':
        folder_path = f'{root_path}/{normalize_fast_note_segment(podcast_name)}'
        note_title = plain_title
    else:
        folder_path = f'{root_path}/{normalize_fast_note_segment(podcast_name)}'
        note_title = fallback_title
    result = upsert_fast_note_markdown(folder_path, note_title, markdown)
    result['clean_answer'] = markdown
    return result


def _extract_uuid_strings(value) -> list[str]:
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    return re.findall(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', text, flags=re.I)



def _parse_studio_status_items(notebook_id: str) -> list[dict]:
    res = run_nlm_command(['studio', 'status', notebook_id, '--json'])
    if res.returncode != 0:
        err = (res.stderr.strip() or res.stdout.strip()).lower()
        if '400' in err or 'bad request' in err or 'authentication' in err:
            raise RuntimeError(res.stderr.strip() or res.stdout.strip() or 'studio status failed')
        return []
    try:
        payload = json.loads((res.stdout or '').strip() or '[]')
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in ['items', 'artifacts', 'value']:
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        return []
    except Exception:
        return []



def _artifact_kind(item: dict) -> str:
    for key in ['kind', 'type', 'artifactType', 'artifact_type', 'category']:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    blob = json.dumps(item, ensure_ascii=False).lower()
    return 'infographic' if 'infographic' in blob else ''



def _artifact_status(item: dict) -> str:
    for key in ['status', 'state', 'progressState', 'progress_state']:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    blob = json.dumps(item, ensure_ascii=False).lower()
    for token in ['completed', 'complete', 'ready', 'done', 'failed', 'error', 'running', 'pending', 'processing', 'queued']:
        if token in blob:
            return token
    return ''



def _artifact_id(item: dict) -> str:
    for key in ['id', 'artifactId', 'artifact_id']:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    uuids = _extract_uuid_strings(item)
    return uuids[0] if uuids else ''



def wait_for_new_infographic_artifact(notebook_id: str, before_ids: Optional[set[str]] = None, timeout_seconds: int = 600, poll_seconds: int = 20) -> Optional[dict]:
    before_ids = before_ids or set()
    deadline = time.time() + timeout_seconds
    last_items = []
    while time.time() < deadline:
        if not ensure_nlm_auth():
            return None
        try:
            items = _parse_studio_status_items(notebook_id)
        except RuntimeError as e:
            print(f'⚠️ studio status 不可用，改走 download 轮询兜底: {e}')
            return None
        last_items = items
        candidates = [item for item in items if _artifact_kind(item) == 'infographic']
        for item in reversed(candidates):
            aid = _artifact_id(item)
            status = _artifact_status(item)
            if aid and aid in before_ids:
                continue
            if status in {'completed', 'complete', 'ready', 'done'}:
                return item
        if candidates:
            newest = candidates[-1]
            aid = _artifact_id(newest)
            status = _artifact_status(newest)
            print(f"⏳ infographic 生成中: id={aid or 'unknown'} status={status or 'unknown'}")
        else:
            print('⏳ 等待 NotebookLM infographic artifact 出现...')
        time.sleep(poll_seconds)

    if last_items:
        print(f"⚠️ infographic 轮询超时，最后状态: {json.dumps(last_items[-3:], ensure_ascii=False)}")
    return None



def latest_completed_infographic_artifact(notebook_id: str, exclude_ids: Optional[set[str]] = None) -> Optional[dict]:
    exclude_ids = exclude_ids or set()
    try:
        items = _parse_studio_status_items(notebook_id)
    except RuntimeError:
        return None

    completed = []
    for item in items:
        if _artifact_kind(item) != 'infographic':
            continue
        if _artifact_status(item) not in {'completed', 'complete', 'ready', 'done'}:
            continue
        completed.append(item)

    preferred = [item for item in completed if _artifact_id(item) not in exclude_ids]
    if preferred:
        return preferred[-1]
    if completed and not exclude_ids:
        return completed[-1]
    return None



def poll_download_infographic(notebook_id: str, output_path: str, artifact_id: str = '', timeout_seconds: int = 600, poll_seconds: int = 20) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not ensure_nlm_auth():
            return False
        cmd = ['download', 'infographic', notebook_id, '--output', output_path]
        if artifact_id:
            cmd[3:3] = ['--id', artifact_id]
        cmd.append('--no-progress')
        res = run_nlm_command(cmd)
        if res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            return True
        detail = (res.stderr or res.stdout or '').strip()
        if detail:
            print(f'⚠️ infographic 下载未成功: {detail[:500]}')
        print('⏳ infographic 尚未可下载，继续轮询...')
        time.sleep(poll_seconds)
    return False



def save_fast_note_attachment(filename: str, source_path: str, folder_path: str = 'attachments') -> dict:
    filename = sanitize_filename(filename)
    if not filename:
        raise ValueError('attachment filename 不能为空')
    source = Path(source_path)
    if not source.exists():
        raise FileNotFoundError(source_path)

    folder_backup = backup_file_once(FAST_NOTE_FOLDER_DB)
    file_backup = backup_file_once(FAST_NOTE_FILE_DB)
    folder_id = ensure_fast_note_folder(folder_path)
    rel_path = f"{folder_path}/{filename}"
    now_ms = int(time.time() * 1000)
    now_dt = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    data = source.read_bytes()
    size = len(data)
    content_hash = java_string_hash(hashlib.md5(data).hexdigest())
    path_hash = java_string_hash(rel_path)

    con = sqlite3.connect(FAST_NOTE_FILE_DB)
    cur = con.cursor()
    cur.execute('select id, ctime, created_at from file where vault_id=1 and path=? and rename=0 order by id desc limit 1', (rel_path,))
    row = cur.fetchone()
    if row:
        file_id = int(row[0])
        ctime = int(row[1] or now_ms)
        created_at = row[2] or now_dt
        cur.execute(
            "update file set action='modify', fid=?, path_hash=?, content_hash=?, size=?, mtime=?, updated_timestamp=?, updated_at=? where id=?",
            (folder_id, path_hash, content_hash, size, now_ms, now_ms, now_dt, file_id),
        )
    else:
        cur.execute(
            "insert into file (vault_id, action, fid, path, path_hash, content_hash, save_path, rename, size, ctime, mtime, updated_timestamp, created_at, updated_at) values (1, 'create', ?, ?, ?, ?, '', 0, ?, ?, ?, ?, ?, ?)",
            (folder_id, rel_path, path_hash, content_hash, size, now_ms, now_ms, now_ms, now_dt, now_dt),
        )
        file_id = int(cur.lastrowid)
        ctime = now_ms
        created_at = now_dt
    con.commit()
    con.close()

    file_dir = Path(FAST_NOTE_FILE_ROOT) / f'f_{file_id}'
    file_dir.mkdir(parents=True, exist_ok=True)
    (file_dir / 'file.dat').write_bytes(data)

    try:
        sync_con = sqlite3.connect(FAST_NOTE_SYNC_LOG_DB)
        sync_cur = sync_con.cursor()
        sync_cur.execute(
            "insert into sync_log (uid, vault_id, type, action, changed_fields, path, path_hash, size, client_name, client_type, client_version, status, message, created_at) values (1, 1, 'file', ?, '', ?, ?, ?, ?, ?, ?, 1, '', ?)",
            ('modify' if row else 'create', rel_path, path_hash, size, FAST_NOTE_CLIENT_NAME, FAST_NOTE_CLIENT_TYPE, FAST_NOTE_CLIENT_VERSION, now_dt),
        )
        sync_con.commit()
        sync_con.close()
    except Exception as e:
        print(f'⚠️ Fast Note attachment sync_log 写入失败（继续）: {e}')

    return {
        'file_id': file_id,
        'file_path': rel_path,
        'folder_id': folder_id,
        'folder_backup': folder_backup,
        'file_backup': file_backup,
    }



def insert_infographic_embed_into_markdown(markdown: str, embed_filename: str) -> str:
    embed = f'![[{embed_filename}]]'
    text = (markdown or '').strip() + '\n'
    if embed in text:
        return text
    lines = text.splitlines()
    in_frontmatter = False
    frontmatter_done = False
    in_quote = False
    for idx, line in enumerate(lines):
        if idx == 0 and line.strip() == '---':
            in_frontmatter = True
            continue
        if in_frontmatter and line.strip() == '---':
            in_frontmatter = False
            frontmatter_done = True
            continue
        if not frontmatter_done:
            continue
        if line.startswith('>'):
            in_quote = True
            continue
        if in_quote and line.strip() == '':
            lines[idx + 1:idx + 1] = [embed]
            return '\n'.join(lines).strip() + '\n'

    m = re.search(r'^##\s+Shownotes\s*$', text, flags=re.M)
    if m:
        return (text[:m.start()] + embed + '\n' + text[m.start():]).replace('\n\n\n', '\n\n')
    if lines and lines[0].startswith('# '):
        return '\n'.join([lines[0], '', embed, ''] + lines[1:]).strip() + '\n'
    return (embed + '\n\n' + text).strip() + '\n'



def attach_infographic_to_fast_note(note_path: str, notebook_id: str, artifact_id: str = '') -> Optional[dict]:
    safe_name = f'NotebookLM infographic {notebook_id[:8]}.png'
    with tempfile.TemporaryDirectory() as tmpdir:
        out = os.path.join(tmpdir, safe_name)
        ok = poll_download_infographic(notebook_id, out, artifact_id=artifact_id, timeout_seconds=600, poll_seconds=20)
        if not ok:
            print('⚠️ infographic 下载轮询超时')
            return None
        attachment = save_fast_note_attachment(safe_name, out)

    folder_path, basename = note_path.rsplit('/', 1)
    note_title = basename[:-3] if basename.lower().endswith('.md') else basename
    note_con = sqlite3.connect(FAST_NOTE_NOTE_DB)
    note_cur = note_con.cursor()
    note_cur.execute('select id from note where vault_id=1 and path=? and rename=0 order by id desc limit 1', (note_path,))
    row = note_cur.fetchone()
    note_con.close()
    if not row:
        return None
    note_id = int(row[0])
    current = (Path(FAST_NOTE_VAULT_ROOT) / f'n_{note_id}' / 'content.txt').read_text(encoding='utf-8')
    updated = insert_infographic_embed_into_markdown(current, safe_name)
    upsert_fast_note_markdown(folder_path, note_title, updated)
    return {
        'artifact_id': artifact_id,
        'attachment': attachment,
        'embed_filename': safe_name,
        'note_id': note_id,
        'note_path': note_path,
    }



def trigger_default_infographics(notebook_id: str, fast_note: Optional[dict] = None) -> dict:
    """默认只触发 1 个 infographic：泥塑风；不等待生成完成。"""
    if fast_note:
        safe_name = f'NotebookLM infographic {notebook_id[:8]}.png'
        note_id = fast_note.get('note_id')
        if note_id:
            note_file = Path(FAST_NOTE_VAULT_ROOT) / f'n_{note_id}' / 'content.txt'
            if note_file.exists() and f'![[{safe_name}]]' in note_file.read_text(encoding='utf-8'):
                print(f'♻️ Fast Note 已有 infographic embed，跳过重复生成: {safe_name}')
                return {'ok': True, 'triggered': False, 'before_ids': set(), 'reason': 'embed_exists'}
    styles = [
        ('clay', 'clay style'),
    ]
    all_ok = True
    try:
        before_ids = {_artifact_id(item) for item in _parse_studio_status_items(notebook_id) if _artifact_kind(item) == 'infographic' and _artifact_id(item)}
    except RuntimeError:
        before_ids = set()

    for style_code, style_label in styles:
        print(f'🖼️ 创建 infographic: {style_label}')
        res = run_nlm_command([
            'infographic', 'create', notebook_id,
            '--style', style_code,
            '--language', 'en',
            '--orientation', 'landscape',
            '--detail', 'standard',
            '--confirm',
        ])
        if res.returncode != 0:
            print(f"⚠️ infographic 创建失败 ({style_label}): {res.stderr.strip() or res.stdout.strip()}")
            all_ok = False
            continue
        print(f'✅ infographic 已触发: {style_label}')

    return {'ok': all_ok, 'triggered': all_ok, 'before_ids': before_ids}


def finalize_default_infographics(notebook_id: str, fast_note: Optional[dict], trigger_state: Optional[dict] = None) -> bool:
    """等待已触发的 infographic 完成，下载并写入 Fast Note Markdown。"""
    if not fast_note:
        return False

    safe_name = f'NotebookLM infographic {notebook_id[:8]}.png'
    note_id = fast_note.get('note_id')
    if note_id:
        note_file = Path(FAST_NOTE_VAULT_ROOT) / f'n_{note_id}' / 'content.txt'
        if note_file.exists() and f'![[{safe_name}]]' in note_file.read_text(encoding='utf-8'):
            print(f'♻️ Fast Note 已有 infographic embed，跳过重复写入: {safe_name}')
            return True

    before_ids = set()
    if trigger_state:
        before_ids = set(trigger_state.get('before_ids') or [])

    artifact = wait_for_new_infographic_artifact(notebook_id, before_ids=before_ids, timeout_seconds=600, poll_seconds=20)
    if not artifact:
        artifact = latest_completed_infographic_artifact(notebook_id, exclude_ids=before_ids)

    artifact_id = _artifact_id(artifact) if artifact else ''
    attached = attach_infographic_to_fast_note(fast_note['note_path'], notebook_id, artifact_id=artifact_id)
    if not attached:
        return False

    print(f"🖼️ infographic 已写入 Fast Note: {attached['embed_filename']}")
    return True


def create_default_infographics(notebook_id: str, fast_note: Optional[dict] = None) -> bool:
    """兼容旧调用：触发 infographic；若有 Fast Note，则等待并写回 embed。"""
    trigger_state = trigger_default_infographics(notebook_id, fast_note=fast_note)
    if not fast_note:
        return bool(trigger_state.get('ok'))
    return finalize_default_infographics(notebook_id, fast_note, trigger_state=trigger_state)


def _parse_notebook_query_answer(query_res: subprocess.CompletedProcess) -> str:
    payload = json.loads(query_res.stdout or '{}')
    value = payload.get('value', payload)
    return (value.get('answer') or '').strip()


def _looks_truncated_query_answer(answer: str) -> tuple[bool, str]:
    text = (answer or '').strip()
    if not text:
        return True, 'empty_answer'

    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if not lines:
        return True, 'empty_answer'

    last = lines[-1].strip()
    if re.match(r'^#{2,3}\s+', last):
        return True, 'ends_with_heading'
    if re.match(r'^[\-*+]\s*$', last):
        return True, 'ends_with_empty_bullet'
    return False, ''


def _is_retryable_query_error(text: str) -> bool:
    msg = (text or '').strip().lower()
    if not msg:
        return False
    retry_markers = [
        'timed out',
        'timeout',
        'deadline',
        'empty answer',
        'read operation timed out',
        'temporarily unavailable',
    ]
    return any(marker in msg for marker in retry_markers)


def run_notebook_query_with_recovery(
    notebook_id: str,
    prompt: str,
    *,
    audio_source_id: Optional[str] = None,
    enforce_complete: bool = True,
    max_attempts: int = NLM_QUERY_MAX_ATTEMPTS,
    retry_sleep_seconds: int = NLM_QUERY_RETRY_SLEEP_SECONDS,
) -> str:
    attempts: list[dict] = []
    last_error = ''

    for idx in range(max_attempts):
        attempt_no = idx + 1
        query_cmd = [
            'notebook', 'query', notebook_id, prompt,
            '--timeout', str(NLM_QUERY_TIMEOUT_SECONDS),
            '--json',
        ]
        if audio_source_id:
            query_cmd += ['--source-ids', audio_source_id]

        query_res = run_nlm_command(query_cmd)
        if query_res.returncode != 0:
            err = (query_res.stderr or query_res.stdout or '').strip()
            last_error = err or 'query failed'
            if attempt_no < max_attempts and _is_retryable_query_error(last_error):
                print(f'⏳ NotebookLM query 第 {attempt_no} 次遇到可重试错误，{retry_sleep_seconds}s 后重试')
                time.sleep(retry_sleep_seconds)
                continue
            raise RuntimeError(last_error)

        answer = _parse_notebook_query_answer(query_res)
        if not answer:
            last_error = 'empty answer'
            if attempt_no < max_attempts:
                print(f'⏳ NotebookLM query 第 {attempt_no} 次返回空答案，{retry_sleep_seconds}s 后重试')
                time.sleep(retry_sleep_seconds)
                continue
            raise RuntimeError(last_error)

        if not enforce_complete:
            if attempt_no > 1:
                print(f'✅ NotebookLM query 第 {attempt_no} 次返回答案')
            return answer

        truncated, reason = _looks_truncated_query_answer(answer)
        attempts.append({'answer': answer, 'truncated': truncated, 'reason': reason})
        if not truncated:
            if attempt_no > 1:
                print(f'✅ NotebookLM query 第 {attempt_no} 次返回完整答案')
            return answer
        if attempt_no < max_attempts:
            print(f'⏳ NotebookLM query 第 {attempt_no} 次疑似截断（{reason}），{retry_sleep_seconds}s 后重试')
            time.sleep(retry_sleep_seconds)

    if attempts:
        attempts.sort(key=lambda item: (not item['truncated'], len(item['answer'])), reverse=True)
        best = attempts[0]
        print(f"⚠️ NotebookLM query 多次后仍疑似截断，返回最佳结果：{best['reason']}")
        return best['answer']
    raise RuntimeError(last_error or 'query failed without answer')


def run_notebooklm_default_summary(
    notebook_id: str,
    prompt: str,
    podcast_name: str,
    episode_title: str,
    episode_url: str = '',
    note_context: Optional[dict] = None,
    audio_source_id: Optional[str] = None,
    no_infographic: bool = False,
    infographic_trigger_state: Optional[dict] = None,
) -> dict:
    """触发较长总结，保存 clean 版到 Fast Note，再补写已生成的 infographic。"""
    if not ensure_nlm_auth():
        return {
            'config_ok': False,
            'query_ok': False,
            'infographic_ok': False,
            'saved_to_fast_note': False,
            'fast_note': None,
            'answer': '',
            'summary': '',
        }

    result = {
        'config_ok': True,
        'query_ok': False,
        'infographic_ok': True,
        'saved_to_fast_note': False,
        'fast_note': None,
        'answer': '',
        'summary': '',
    }

    print('🧠 配置 NotebookLM 回答长度: longer')
    cfg_res = run_nlm_command([
        'chat', 'configure', notebook_id,
        '--goal', 'default',
        '--response-length', 'longer',
    ])
    if cfg_res.returncode != 0:
        print(f"⚠️ 配置回答长度失败: {cfg_res.stderr.strip() or cfg_res.stdout.strip()}")
        result['config_ok'] = False

    print(f'💬 触发默认总结: {prompt}')
    try:
        answer = run_notebook_query_with_recovery(
            notebook_id,
            prompt,
            audio_source_id=audio_source_id,
            enforce_complete=True,
        )
        if answer:
            result['answer'] = answer
            result['query_ok'] = True
            print('✅ 已拿到 NotebookLM 总结文本')

            if not no_infographic and not infographic_trigger_state:
                infographic_trigger_state = trigger_default_infographics(notebook_id)

            summary_prompt = '请用一段 120-180 字的中文概括这期播客的核心内容，直接输出摘要正文，不要标题，不要项目符号。'
            try:
                summary_text = run_notebook_query_with_recovery(
                    notebook_id,
                    summary_prompt,
                    audio_source_id=audio_source_id,
                    enforce_complete=False,
                )
                result['summary'] = clean_notebooklm_answer(summary_text).strip()
            except Exception as e:
                print(f'⚠️ 摘要生成失败（继续）: {e}')

            fast_note = save_podcast_summary_to_fast_note(
                podcast_name,
                episode_title,
                answer,
                episode_url=episode_url,
                summary=result['summary'],
                info=note_context,
            )
            result['fast_note'] = fast_note
            result['saved_to_fast_note'] = True
            print(f"📝 已保存到 Fast Note: {fast_note['note_path']} (note_id={fast_note['note_id']})")
        else:
            print('⚠️ NotebookLM query 返回成功，但 answer 为空')
    except Exception as e:
        print(f'⚠️ 默认总结触发失败: {e}')

    if no_infographic:
        result['infographic_ok'] = False
    else:
        fast_note = result.get('fast_note')
        if fast_note:
            if not infographic_trigger_state:
                infographic_trigger_state = trigger_default_infographics(notebook_id, fast_note=fast_note)
            result['infographic_ok'] = finalize_default_infographics(
                notebook_id,
                fast_note,
                trigger_state=infographic_trigger_state,
            )
        elif infographic_trigger_state:
            result['infographic_ok'] = bool(infographic_trigger_state.get('ok'))
        else:
            result['infographic_ok'] = create_default_infographics(notebook_id)

    return result


def create_notebook_and_upload_to_notebooklm(
    audio_path: str,
    notebook_title: str,
    podcast_name: str,
    episode_title: str,
    note_context: Optional[dict] = None,
    episode_url: str = '',
    summary_prompt: str = '帮我总结这期播客',
    no_infographic: bool = False,
) -> dict:
    """使用 nlm CLI 新建 notebook、上传音频和小宇宙链接，并触发默认总结。"""
    result = {
        'ok': False,
        'notebook_id': None,
        'summary': None,
        'status': 'failed',
        'audio_source_id': None,
        'reason': '',
    }

    if not os.path.exists(audio_path):
        print(f"❌ 音频文件不存在: {audio_path}")
        return result

    if not ensure_nlm_auth(allow_fallback=True):
        return result

    notebook_id = create_or_reuse_notebook(notebook_title)
    if not notebook_id:
        result['reason'] = 'notebook_create_failed'
        print(f"📣 RESULT status=failed notebook_id=none note_id=none note_path=none reason={json.dumps('notebook_create_failed', ensure_ascii=False)}")
        return result

    result['notebook_id'] = notebook_id

    upload_result = guarded_add_audio_source(notebook_id, audio_path, max_wait_seconds=NLM_AUDIO_UPLOAD_MAX_WAIT_SECONDS)
    if upload_result.get('status') != 'confirmed' and should_retry_audio_upload_as_mp3(audio_path, upload_result):
        failed_source_id = upload_result.get('audio_source_id')
        if failed_source_id:
            delete_notebooklm_source(failed_source_id)
        fallback_audio_path = transcode_audio_to_notebooklm_mp3(audio_path)
        if fallback_audio_path:
            print('🔁 m4a 上传/处理未确认，改用 mp3 fallback 重新上传一次')
            upload_result = guarded_add_audio_source(notebook_id, fallback_audio_path, max_wait_seconds=NLM_AUDIO_UPLOAD_MAX_WAIT_SECONDS)
    audio_source_id = upload_result.get('audio_source_id')
    result['audio_source_id'] = audio_source_id

    if upload_result.get('status') != 'confirmed':
        result['status'] = 'pending' if upload_result.get('status') == 'pending' else 'failed-explicit'
        result['reason'] = upload_result.get('reason') or 'notebooklm_audio_source_unconfirmed'
        print(f"⚠️ NotebookLM 音频 source 未确认完成: {result['reason']}")
        print(f"📣 RESULT status={result['status']} notebook_id={notebook_id} note_id=none note_path=none reason={json.dumps(result['reason'], ensure_ascii=False)}")
        return result

    if audio_source_id:
        print(f"✅ 音频 source 已创建: {audio_source_id}")
    print(f"✅ NotebookLM 上传完成: {notebook_id}（默认仅上传音频 source）")
    result['summary'] = run_notebooklm_default_summary(
        notebook_id,
        summary_prompt,
        podcast_name=podcast_name,
        episode_title=episode_title,
        episode_url=episode_url,
        note_context=note_context,
        audio_source_id=audio_source_id,
        no_infographic=no_infographic,
        infographic_trigger_state=None,
    )
    result['ok'] = bool(result['summary'] and result['summary'].get('query_ok'))
    fn = (result.get('summary') or {}).get('fast_note') if result.get('summary') else None
    if result['ok'] and fn:
        result['status'] = 'success'
        print(f"📣 RESULT status=success notebook_id={notebook_id} note_id={fn['note_id']} note_path={fn['note_path']} query_ok=1 fast_note_ok=1 infographic_ok={1 if result['summary'].get('infographic_ok') else 0}")
    else:
        result['status'] = 'partial' if notebook_id else 'failed'
        result['reason'] = result['reason'] or 'query_or_fast_note_incomplete'
        print(f"📣 RESULT status={result['status']} notebook_id={notebook_id} note_id=none note_path=none query_ok={1 if (result.get('summary') or {}).get('query_ok') else 0} fast_note_ok={1 if fn else 0} infographic_ok={1 if (result.get('summary') or {}).get('infographic_ok') else 0} reason={json.dumps(result['reason'], ensure_ascii=False)}")
    return result


def find_drive_episode_record(podcast_name: str, episode_id: str = '', episode_url: str = '') -> Optional[dict]:
    """按 episode_id / episode_url 在 Drive 中定位已存在单集目录及其文件"""
    safe_podcast = sanitize_filename(podcast_name)
    podcast_folder_id = resolve_drive_folder_id(f"Podcasts/{safe_podcast}")
    if not podcast_folder_id:
        return None

    episode_folders = drive_ls(
        podcast_folder_id,
        "mimeType='application/vnd.google-apps.folder' and trashed=false"
    )

    normalized_episode_url = normalize_episode_url(episode_url)

    for ep_folder in episode_folders:
        files = drive_ls(ep_folder['id'], 'trashed=false')
        meta = None
        meta_file = None
        for f in files:
            if f.get('name') != 'metadata.json':
                continue
            meta = download_drive_file_json(f.get('id', ''))
            if meta:
                meta_file = f
                break

        if not meta:
            continue

        meta_episode_id = (meta.get('episode_id') or '').strip()
        meta_episode_url = normalize_episode_url((meta.get('episode_url') or '').strip())
        matched = False
        if episode_id and meta_episode_id == episode_id:
            matched = True
        if normalized_episode_url and meta_episode_url == normalized_episode_url:
            matched = True
        if not matched:
            continue

        audio_file = None
        for f in files:
            name = (f.get('name') or '').lower()
            if name.endswith('.m4a') or name.endswith('.mp3') or name.endswith('.wav') or name.endswith('.aac'):
                audio_file = f
                break

        if not audio_file:
            return None

        return {
            'folder': ep_folder,
            'files': files,
            'metadata': meta,
            'metadata_file': meta_file,
            'audio_file': audio_file,
        }

    return None


def download_drive_file(file_id: str, output_path: str) -> bool:
    """下载 Drive 文件到指定路径"""
    gog = os.path.expanduser('~/.local/bin/gog')
    result = subprocess.run(
        [gog, 'drive', 'download', file_id, '--out', output_path],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"❌ Drive 下载失败: {result.stderr.strip() or result.stdout.strip()}")
        return False
    return True


def refresh_episode_metadata_for_drive(existing_meta: dict, episode_url: str = '') -> dict:
    meta = dict(existing_meta or {})
    target_url = normalize_episode_url(episode_url or meta.get('episode_url') or meta.get('source_url') or '')
    if not target_url:
        return meta

    try:
        html = fetch_page(target_url)
        fresh = extract_apple_episode_info(html, target_url) if is_apple_episode_url(target_url) else extract_episode_info(html, target_url)
        if fresh.get('title'):
            meta.update({k: v for k, v in fresh.items() if v not in (None, '', [], {})})
            meta['episode_url'] = fresh.get('episode_url') or target_url
            build_timeline_summary_prompt(meta)
            print('♻️ 已按当前 episode 页面刷新 metadata 与时间轴')
            return meta
        print('⚠️ 当前 episode 页面未提取到有效 metadata，回退旧 metadata')
    except Exception as e:
        print(f'⚠️ 刷新 metadata 失败，回退旧 metadata: {e}')
    return meta



def overwrite_drive_metadata_json(record: dict, refreshed_meta: dict) -> bool:
    folder = (record or {}).get('folder') or {}
    folder_id = folder.get('id') or ''
    if not folder_id:
        return False

    gog = os.path.expanduser('~/.local/bin/gog')
    for f in (record.get('files') or []):
        if (f.get('name') or '') != 'metadata.json':
            continue
        file_id = f.get('id') or ''
        if not file_id:
            continue
        delete_res = subprocess.run([gog, 'drive', 'delete', file_id, '--force'], capture_output=True, text=True, errors='replace')
        if delete_res.returncode != 0:
            print(f"⚠️ 删除旧 metadata.json 失败: {delete_res.stderr.strip() or delete_res.stdout.strip()}")
            return False

    with tempfile.TemporaryDirectory() as tmpdir:
        metadata_path = os.path.join(tmpdir, 'metadata.json')
        save_metadata(refreshed_meta, metadata_path)
        upload_cmd = [gog, 'drive', 'upload', metadata_path, '--parent', folder_id, '--name', 'metadata.json']
        result = subprocess.run(upload_cmd, capture_output=True, text=True, errors='replace')
        if result.returncode != 0:
            print(f"⚠️ 上传新 metadata.json 失败: {result.stderr.strip() or result.stdout.strip()}")
            return False
    print('✅ Drive metadata.json 已刷新覆盖')
    return True



def upload_existing_drive_episode_to_notebooklm(podcast_name: str, title: str, episode_id: str = '', episode_url: str = '', no_infographic: bool = False) -> dict:
    """从已存在的 Drive 单集回拉音频，上传到 NotebookLM，再删除本地临时文件。"""
    record = find_drive_episode_record(podcast_name, episode_id=episode_id, episode_url=episode_url)
    if not record:
        print('⚠️ Drive 中未找到可回拉的音频，需重新下载源文件')
        return {'ok': False, 'status': 'failed', 'notebook_id': None, 'summary': None, 'reason': 'drive_audio_not_found'}

    refreshed_meta = refresh_episode_metadata_for_drive(record.get('metadata') or {}, episode_url=episode_url)
    overwrite_drive_metadata_json(record, refreshed_meta)

    notebook_title = build_episode_notebook_title(podcast_name, title, (refreshed_meta or {}).get('pub_date', ''))
    audio_name = record['audio_file']['name']
    audio_id = record['audio_file']['id']
    summary_prompt = build_timeline_summary_prompt(refreshed_meta or {})

    with tempfile.TemporaryDirectory() as tmpdir:
        audio_path = os.path.join(tmpdir, audio_name)
        print(f"📥 从 Drive 回拉音频: {audio_name}")
        if not download_drive_file(audio_id, audio_path):
            return {'ok': False, 'notebook_id': None, 'summary': None}
        return create_notebook_and_upload_to_notebooklm(
            audio_path,
            notebook_title,
            podcast_name=podcast_name,
            episode_title=title,
            note_context=refreshed_meta or {},
            episode_url=episode_url,
            summary_prompt=summary_prompt,
            no_infographic=no_infographic,
        )

def check_exists(folder_path: str) -> bool:
    """检查文件夹是否已存在"""
    return resolve_drive_folder_id(folder_path) is not None


def download_drive_file_json(file_id: str) -> Optional[dict]:
    """下载 Drive 上的小 JSON 文件并解析"""
    gog = os.path.expanduser('~/.local/bin/gog')
    with tempfile.TemporaryDirectory() as tmpdir:
        result = subprocess.run(
            [gog, 'drive', 'download', file_id, '--out', tmpdir],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            return None

        for name in os.listdir(tmpdir):
            path = os.path.join(tmpdir, name)
            if not os.path.isfile(path):
                continue
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                return None
    return None


def build_drive_existing_episode_keys(podcast_name: str) -> set:
    """扫描 Drive 上该播客目录中已存在的 episode_id / episode_url"""
    keys = set()
    safe_podcast = sanitize_filename(podcast_name)
    podcast_folder_id = resolve_drive_folder_id(f"Podcasts/{safe_podcast}")
    if not podcast_folder_id:
        return keys

    episode_folders = drive_ls(
        podcast_folder_id,
        "mimeType='application/vnd.google-apps.folder' and trashed=false"
    )

    for ep_folder in episode_folders:
        files = drive_ls(ep_folder['id'], "trashed=false")
        for f in files:
            if f.get('name') != 'metadata.json':
                continue
            meta = download_drive_file_json(f.get('id', ''))
            if not meta:
                continue
            episode_id = (meta.get('episode_id') or '').strip()
            episode_url = normalize_episode_url((meta.get('episode_url') or '').strip())
            if episode_id:
                keys.add(f"eid:{episode_id}")
            if episode_url:
                keys.add(f"url:{episode_url}")
            break

    return keys


def drive_episode_already_downloaded(podcast_name: str, episode_id: str = '', episode_url: str = '', existing_keys: set = None) -> bool:
    """按 episode_id / episode_url 判断 Drive 上是否已下载"""
    keys = existing_keys if existing_keys is not None else build_drive_existing_episode_keys(podcast_name)
    if episode_id and f"eid:{episode_id}" in keys:
        return True
    if episode_url and f"url:{episode_url}" in keys:
        return True
    return False


def normalize_episode_url(url: str) -> str:
    return re.sub(r'\?utm_source=.*$', '', (url or '').strip())



def resolve_named_episode_targets(target: str) -> list[str]:
    target = (target or '').strip()
    if not target:
        return []
    if '://' in target:
        return [target]

    # Phrase-resolution patterns come from the external routing config: any route
    # that declares a "phrase_patterns" list opts into name-based episode targeting.
    named_route_patterns = [
        (name, route['phrase_patterns'])
        for name, route in PODCAST_ROUTING.items()
        if route.get('phrase_patterns')
    ]
    for route_name, patterns in named_route_patterns:
        if not any(re.search(pattern, target, flags=re.I) for pattern in patterns):
            continue

        route = get_podcast_route(route_name)
        apple_show_url = route.get('apple_show_url', '')

        range_match = re.search(r'(\d{1,4})\s*(?:~|-|—|–|到)\s*(\d{1,4})', target)
        if range_match:
            start = int(range_match.group(1))
            end = int(range_match.group(2))
            if end < start:
                start, end = end, start
            urls = []
            for num in range(start, end + 1):
                ep = _find_episode_from_apple_show_by_number(apple_show_url, str(num), route=route)
                if ep and (ep.get('url') or '').strip():
                    urls.append((ep.get('url') or '').strip())
            return urls

        m = re.search(r'第\s*(\d{1,4})\s*[集期]', target, flags=re.I)
        if not m:
            m = re.search(r'\b(\d{1,4})\b', target)
        if m:
            episode_token = m.group(1)
            ep = _find_episode_from_apple_show_by_number(apple_show_url, episode_token, route=route)
            if ep and (ep.get('url') or '').strip():
                return [(ep.get('url') or '').strip()]
    return [target]



def resolve_podcast_name_for_drive(target: str) -> str:
    """支持传入播客 URL 或播客名称，返回 Drive 中使用的播客目录名"""
    target = (target or '').strip()
    if not target:
        raise ValueError('podcast target 不能为空')
    if 'xiaoyuzhoufm.com/podcast/' in target:
        html = fetch_page(target)
        podcast_info = extract_podcast_info(html)
        podcast_name = (podcast_info.get('title') or '').strip()
        if not podcast_name:
            raise ValueError('无法从播客 URL 解析节目名')
        return podcast_name
    return target


def scan_drive_duplicate_groups(podcast_name: str) -> list:
    """扫描 Drive 播客目录中的历史重复。按 episode_id / episode_url 分组。"""
    safe_podcast = sanitize_filename(podcast_name)
    podcast_folder_id = resolve_drive_folder_id(f"Podcasts/{safe_podcast}")
    if not podcast_folder_id:
        return []

    episode_folders = drive_ls(
        podcast_folder_id,
        "mimeType='application/vnd.google-apps.folder' and trashed=false",
        1000,
    )

    records = []
    for ep_folder in episode_folders:
        files = drive_ls(ep_folder['id'], "trashed=false", 100)
        meta = None
        for f in files:
            if f.get('name') != 'metadata.json':
                continue
            meta = download_drive_file_json(f.get('id', ''))
            if meta:
                break

        episode_id = (meta or {}).get('episode_id', '') or ''
        episode_url = normalize_episode_url((meta or {}).get('episode_url', '') or '')
        group_key = episode_id or episode_url
        if not group_key:
            continue

        records.append({
            'folder_id': ep_folder['id'],
            'folder_name': ep_folder['name'],
            'modified_time': ep_folder.get('modifiedTime', ''),
            'episode_id': episode_id,
            'episode_url': episode_url,
            'title': (meta or {}).get('title') or ep_folder['name'],
        })

    grouped = defaultdict(list)
    for record in records:
        grouped[record['episode_id'] or record['episode_url']].append(record)

    duplicate_groups = []
    for key, items in grouped.items():
        if len(items) < 2:
            continue
        ordered = sorted(items, key=lambda x: (x.get('modified_time', ''), x.get('folder_id', '')))
        duplicate_groups.append({
            'key': key,
            'title': ordered[0].get('title') or ordered[0]['folder_name'],
            'keep': ordered[0],
            'delete': ordered[1:],
            'all': ordered,
        })

    duplicate_groups.sort(key=lambda g: (g['keep'].get('modified_time', ''), g['title']))
    return duplicate_groups


def print_drive_duplicate_report(podcast_name: str, duplicate_groups: list) -> None:
    print(f"📦 Drive 播客目录: {podcast_name}")
    if not duplicate_groups:
        print("✅ 未发现重复 (按 episode_id / episode_url)")
        return

    total_delete = sum(len(g['delete']) for g in duplicate_groups)
    print(f"⚠️ 发现 {len(duplicate_groups)} 组重复，待清理文件夹 {total_delete} 个")
    print()
    for idx, group in enumerate(duplicate_groups, start=1):
        keep = group['keep']
        print(f"[{idx}] {group['title']}")
        print(f"    key: {group['key']}")
        print(f"    keep: {keep['folder_name']} ({keep['folder_id']}) @ {keep['modified_time']}")
        for item in group['delete']:
            print(f"    del : {item['folder_name']} ({item['folder_id']}) @ {item['modified_time']}")
        print()


def dedupe_drive_podcast_folder(target: str, apply: bool = False) -> int:
    """清理 Drive 上某个播客目录的历史重复。默认只读，apply=True 时将较新的重复项移到垃圾桶。"""
    podcast_name = resolve_podcast_name_for_drive(target)
    duplicate_groups = scan_drive_duplicate_groups(podcast_name)
    print_drive_duplicate_report(podcast_name, duplicate_groups)

    delete_ids = [item['folder_id'] for group in duplicate_groups for item in group['delete']]
    if not apply:
        if delete_ids:
            print("🧪 当前为只读预览；加 --apply 才会实际删除重复文件夹")
        return len(delete_ids)

    if not delete_ids:
        print("无需清理。")
        return 0

    gog = os.path.expanduser('~/.local/bin/gog')
    for folder_id in delete_ids:
        print(f"🗑️  删除重复文件夹: {folder_id}")
        result = subprocess.run(
            [gog, 'drive', 'delete', folder_id, '--json', '--force'],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            raise RuntimeError(f"删除失败 {folder_id}: {result.stderr.strip() or result.stdout.strip()}")

    print(f"✅ 已移到垃圾桶: {len(delete_ids)} 个重复文件夹")
    return len(delete_ids)


def download_single_episode(episode_url: str, local_only: bool = False, podcast_name_override: str = None, enable_notebooklm: bool = True, existing_keys: set = None, no_infographic: bool = False) -> str | bool:
    """下载单集"""
    print(f"🔍 获取单集信息...")
    if is_direct_audio_episode_url(episode_url):
        info = extract_direct_audio_episode_info(episode_url)
        print('🔗 音频直链 resolver: 已识别 RedCircle stream audio')
    else:
        html = fetch_page(episode_url)
        if is_apple_episode_url(episode_url):
            info = extract_apple_episode_info(html, episode_url)
            if not info.get('audio_url'):
                print("❌ Apple Podcasts 页面未解析到音频直链")
                return False
            print('🍎 Apple Podcasts resolver: 已解析到 audio / feed / episode webpage')
        elif is_pocketcasts_episode_url(episode_url):
            info = extract_pocketcasts_episode_info(html, episode_url)
            if not info.get('audio_url'):
                print("❌ Pocket Casts 页面未解析到音频直链")
                return False
            if not extract_timeline_outline(info.get('shownotes') or info.get('description') or ''):
                print("❌ Pocket Casts 页面未解析到 timeline")
                return False
            print('🟣 Pocket Casts resolver: 已解析到 audio + timeline')
        elif is_goodpods_episode_url(episode_url):
            info = extract_goodpods_episode_info(html, episode_url)
            if not info.get('audio_url'):
                print("❌ Goodpods 页面未解析到音频直链")
                return False
            print('🟢 Goodpods resolver: 已解析到 audio / mirrored source')
        else:
            info = extract_episode_info(html, episode_url)
    
    if 'audio_url' not in info:
        print("❌ 无法找到音频链接")
        return False
    
    title = info.get('title', 'unknown_episode')
    podcast_name = podcast_name_override or info.get('podcast_name', 'unknown_podcast')
    audio_url = info['audio_url']
    duration = info.get('duration', 0)
    audio_ext = infer_audio_extension(audio_url)
    direct_markdown = should_use_direct_markdown_for_podcast(podcast_name)
    skip_drive = should_skip_drive_for_podcast(podcast_name)
    
    print(f"   📻 节目: {podcast_name}")
    print(f"   📝 标题: {title}")
    print(f"   ⏱️  时长: {format_duration(duration)}")
    print()

    nlm_profile = resolve_nlm_profile_for_podcast(podcast_name)
    os.environ['NLM_PROFILE'] = nlm_profile
    if direct_markdown:
        print('📝 命中 direct-markdown 播客：跳过 NotebookLM，直接写入 Fast Note')
        if skip_drive:
            print('📦 命中 no-drive 播客：不上传 Google Drive')
    else:
        print(f'🪪 NotebookLM profile: {nlm_profile}')

    if should_skip_infographics_for_podcast(podcast_name):
        no_infographic = True
        print('🖼️  命中 no-infographic 播客名单：默认跳过 infographic')
        print()
    
    safe_title = sanitize_filename(title)
    safe_podcast = sanitize_filename(podcast_name)
    episode_id = info.get('episode_id', '')
    normalized_episode_url = normalize_episode_url(episode_url)
    summary_prompt = build_timeline_summary_prompt(info)
    timeline_stats = summarize_timeline_outline(info.get('timeline_outline', []))
    if timeline_stats['top_level_sections']:
        print(
            "🧭 Timeline: "
            f"sections={timeline_stats['top_level_sections']} "
            f"entries={timeline_stats['flattened_timeline_entries_total']} "
            f"children={timeline_stats['child_entries_total']}"
        )

    if (not local_only) and (not skip_drive) and drive_episode_already_downloaded(
        podcast_name,
        episode_id=episode_id,
        episode_url=normalized_episode_url,
        existing_keys=existing_keys,
    ):
        print(f"♻️  Drive 已存在，按 episode_id/URL 命中: {episode_id or normalized_episode_url}")
        if enable_notebooklm:
            notebook_result = upload_existing_drive_episode_to_notebooklm(
                podcast_name,
                title,
                episode_id=episode_id,
                episode_url=normalized_episode_url,
                no_infographic=no_infographic,
            )
            if notebook_result.get('ok'):
                print("✅ 已从 Drive 回拉并上传到 NotebookLM，并写入 Fast Note；不重复上传 Google Drive")
                return 'uploaded_from_drive'
            if notebook_result.get('status') == 'pending':
                print("⏳ Drive 回拉后的 NotebookLM 音频仍在 single-flight 等待窗口内，本次不重复上传")
                return False

            print("❌ Drive 回拉后的 NotebookLM / Fast Note 未完整成功，本次不自动重新下载源音频，避免重复上传")
            return False
        else:
            return 'skipped'

    if (not local_only) and direct_markdown and skip_drive:
        result = save_direct_markdown_to_fast_note(
            podcast_name,
            title,
            episode_url=normalized_episode_url,
            info=info,
        )
        print()
        print(f"🎉 已直接写入 Fast Note: {result.get('note_path', '')}")
        return 'direct_markdown_saved'
    
    if local_only:
        episode_dir = os.path.expanduser(f"~/Downloads/Podcasts/{safe_podcast}/{safe_title}")
        
        if os.path.exists(episode_dir):
            print(f"⏭️  已存在，跳过: {episode_dir}")
            return 'skipped'
        
        os.makedirs(episode_dir, exist_ok=True)
        
        audio_path = os.path.join(episode_dir, f"{safe_title}{audio_ext}")
        if not download_audio(audio_url, audio_path):
            return False
        
        safe_link_name = sanitize_filename(episode_url)
        link_path = os.path.join(episode_dir, f"{safe_link_name}.txt")
        save_link_txt(episode_url, link_path)
        
        metadata_path = os.path.join(episode_dir, "metadata.json")
        save_metadata(info, metadata_path)
        
        print()
        print(f"🎉 完成！文件保存在: {episode_dir}/")
    else:
        drive_folder = f"Podcasts/{safe_podcast}/{safe_title}"
        
        if check_exists(drive_folder):
            print(f"⏭️  已存在，跳过: {drive_folder}")
            return 'skipped'
        
        notebooklm_failed = False
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = os.path.join(tmpdir, f"{safe_title}{audio_ext}")
            if not download_audio(audio_url, audio_path):
                return False
            
            safe_link_name = sanitize_filename(episode_url)
            link_path = os.path.join(tmpdir, f"{safe_link_name}.txt")
            save_link_txt(episode_url, link_path)
            
            metadata_path = os.path.join(tmpdir, "metadata.json")
            save_metadata(info, metadata_path)

            if direct_markdown:
                result = save_direct_markdown_to_fast_note(
                    podcast_name,
                    title,
                    episode_url=normalized_episode_url,
                    info=info,
                )
                print()
                print(f"📝 已直接写入 Fast Note: {result.get('note_path', '')}")
            elif enable_notebooklm:
                notebook_title = build_episode_notebook_title(podcast_name, title, info.get('pub_date', ''))
                print()
                print("📒 上传到 NotebookLM (新建笔记本)...")
                notebook_result = create_notebook_and_upload_to_notebooklm(
                    audio_path,
                    notebook_title,
                    podcast_name=podcast_name,
                    episode_title=title,
                    note_context=info,
                    episode_url=normalized_episode_url,
                    summary_prompt=summary_prompt,
                    no_infographic=no_infographic,
                )
                if not notebook_result.get('ok'):
                    notebooklm_failed = True
                    print("⚠️ NotebookLM 上传/本地 Markdown 保存未完整成功，继续上传到 Drive")
            
            if not skip_drive:
                print()
                print(f"📤 上传到 Drive: {drive_folder}/")
                upload_to_drive(audio_path, drive_folder, f"{safe_title}{audio_ext}")
                upload_to_drive(link_path, drive_folder, f"{safe_title}.txt")
                upload_to_drive(metadata_path, drive_folder, "metadata.json")
        
        print()
        print("🎉 完成！")

        if notebooklm_failed:
            return False
    
    return 'downloaded'

def download_podcast(podcast_url: str, local_only: bool = False):
    """下载整个播客的所有单集 (通过 RSS)"""
    pid_match = re.search(r'/podcast/([a-f0-9]+)', podcast_url)
    if not pid_match:
        print("❌ 无法解析播客 ID")
        sys.exit(1)
    pid = pid_match.group(1)
    
    print(f"🔍 获取播客信息...")
    html = fetch_page(podcast_url)
    podcast_info = extract_podcast_info(html)
    
    if not podcast_info.get('title'):
        print("❌ 无法获取播客信息")
        sys.exit(1)
    
    podcast_name = podcast_info['title']
    total_count = podcast_info.get('episode_count', 0)
    
    print(f"   📻 节目: {podcast_name}")
    print(f"   📊 共 {total_count} 集")
    print()
    
    # 尝试获取 RSS
    print("📡 获取 RSS feed...")
    rss_url = None
    
    # Method 1: Try direct xyzfm.space RSS
    direct_rss = f"https://feed.xyzfm.space/{pid}"
    try:
        test_result = subprocess.run(
            ['curl', '-sL', '-o', '/dev/null', '-w', '%{http_code}', direct_rss],
            capture_output=True, text=True, timeout=10
        )
        if test_result.stdout.strip() == '200':
            rss_url = direct_rss
            print(f"   ✅ 找到直接 RSS: {rss_url}")
    except:
        pass
    
    # Method 2: Search Apple Podcasts
    if not rss_url:
        print("   🔎 搜索 Apple Podcasts...")
        rss_url = get_rss_from_apple(podcast_name)
        if rss_url:
            print(f"   ✅ 从 Apple Podcasts 获取 RSS: {rss_url}")
    
    if not rss_url:
        print("   ❌ 无法获取 RSS feed")
        print("   💡 尝试使用页面数据 (可能不完整)")
        # Fallback to page data (limited to 15 episodes)
        episodes_from_page = []
        json_match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(\{.+?\})</script>', html, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(1))
                episodes_from_page = data.get('props', {}).get('pageProps', {}).get('podcast', {}).get('episodes', [])
            except:
                pass
        
        if not episodes_from_page:
            print("❌ 无法获取单集列表")
            sys.exit(1)
        
        episodes = [{'eid': e.get('eid'), 'title': e.get('title'), 'url': f"https://www.xiaoyuzhoufm.com/episode/{e.get('eid')}"} for e in episodes_from_page]
    else:
        # Parse RSS
        print("📋 解析 RSS...")
        episodes = parse_rss_episodes(rss_url)
    
    print(f"   找到 {len(episodes)} 集")
    print()
    
    if not episodes:
        print("❌ 没有找到单集")
        sys.exit(1)
    
    # Sort by pub_date or eid (oldest first)
    episodes.sort(key=lambda x: x.get('eid', '') or x.get('pub_date', ''))

    existing_keys = set()
    if not local_only:
        print("🔎 扫描 Google Drive 已有单集...")
        existing_keys = build_drive_existing_episode_keys(podcast_name)
        print(f"   已发现 {len(existing_keys)} 个去重键")
        print()
    
    success_count = 0
    skip_count = 0
    fail_count = 0
    
    for i, ep in enumerate(episodes):
        ep_title = ep.get('title', f'Episode {i+1}')
        episode_url = ep.get('url', '')
        
        if not episode_url and ep.get('eid'):
            episode_url = f"https://www.xiaoyuzhoufm.com/episode/{ep['eid']}"
        
        print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print(f"📍 [{i+1}/{len(episodes)}] {ep_title}")
        print()
        
        try:
            result = download_single_episode(
                episode_url,
                local_only,
                podcast_name,
                enable_notebooklm=False,
                existing_keys=existing_keys,
            )
            if result == 'skipped':
                skip_count += 1
            elif result == 'downloaded':
                success_count += 1
                eid = ep.get('eid', '')
                normalized_url = normalize_episode_url(episode_url)
                if eid:
                    existing_keys.add(f"eid:{eid}")
                if normalized_url:
                    existing_keys.add(f"url:{normalized_url}")
            else:
                fail_count += 1
        except Exception as e:
            print(f"❌ 错误: {e}")
            fail_count += 1
        
        if i < len(episodes) - 1:
            print("⏳ 等待 2 秒...")
            time.sleep(2)
        
        print()
    
    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"🎉 全部完成！")
    print(f"   ✅ 成功: {success_count}")
    print(f"   ⏭️  跳过: {skip_count}")
    print(f"   ❌ 失败: {fail_count}")

def main():
    if len(sys.argv) < 2:
        print("用法:")
        print("  python xiaoyuzhou_dl.py <url|播客短语> [--local] [--yes]")
        print("  python xiaoyuzhou_dl.py --scan-drive-duplicates <podcast_url_or_name>")
        print("  python xiaoyuzhou_dl.py --dedupe-drive <podcast_url_or_name> [--apply]")
        print()
        print("示例:")
        print("  单集: python xiaoyuzhou_dl.py https://www.xiaoyuzhoufm.com/episode/697c58562fc7f49d0902395d")
        print("  全集: python xiaoyuzhou_dl.py https://www.xiaoyuzhoufm.com/podcast/67e366fa1c465530de1f9d61")
        print("  Apple 单集: python xiaoyuzhou_dl.py 'https://podcasts.apple.com/...?...i=...' ")
        print("  本地: python xiaoyuzhou_dl.py <url> --local")
        print("  单集默认上传 NotebookLM: python xiaoyuzhou_dl.py <url>")
        print("  按短语指定集数: python xiaoyuzhou_dl.py '<节目名> 第821集'")
        print("  按短语指定区间: python xiaoyuzhou_dl.py '<节目名> 100~150' --yes")
        print("  扫描 Drive 重复: python xiaoyuzhou_dl.py --scan-drive-duplicates '<节目名>'")
        print("  清理 Drive 重复: python xiaoyuzhou_dl.py --dedupe-drive '<节目名>' --apply")
        sys.exit(1)

    if sys.argv[1] in ('--scan-drive-duplicates', '--dedupe-drive'):
        if len(sys.argv) < 3:
            print(f"❌ 缺少目标播客：{sys.argv[1]} <podcast_url_or_name>")
            sys.exit(1)
        target = sys.argv[2]
        apply = '--apply' in sys.argv
        print("🎙️ 小宇宙下载器")
        print(f"   目标: {target}")
        print(f"   模式: {'清理 Drive 历史重复' if sys.argv[1] == '--dedupe-drive' else '扫描 Drive 历史重复'}")
        print()
        if sys.argv[1] == '--scan-drive-duplicates':
            dedupe_drive_podcast_folder(target, apply=False)
        else:
            dedupe_drive_podcast_folder(target, apply=apply)
        return

    raw_target = sys.argv[1]
    urls = resolve_named_episode_targets(raw_target)
    url = urls[0] if urls else ''
    local_only = '--local' in sys.argv
    no_notebooklm = '--no-notebooklm' in sys.argv
    force_all = '--yes' in sys.argv or '--all' in sys.argv

    print(f"🎙️ 小宇宙下载器")
    if len(urls) > 1:
        print(f"   输入: {raw_target}")
        print(f"   解析: {len(urls)} 集")
        if urls:
            print(f"   首集: {urls[0]}")
            print(f"   末集: {urls[-1]}")
    elif raw_target != url:
        print(f"   输入: {raw_target}")
        print(f"   解析: {url}")
    else:
        print(f"   URL: {url}")
    if local_only:
        print(f"   模式: 本地下载")
    elif len(urls) > 1:
        print(f"   模式: 批量单集处理")
    elif 'xiaoyuzhoufm.com/podcast/' in url:
        print(f"   模式: 上传到 Google Drive (全集默认不上传 NotebookLM)")
    elif is_apple_episode_url(url):
        print(f"   模式: Apple 单集解析 → NotebookLM + Google Drive")
    elif is_pocketcasts_episode_url(url):
        print(f"   模式: Pocket Casts 单集解析 → NotebookLM + Google Drive")
    elif is_goodpods_episode_url(url):
        print(f"   模式: Goodpods 单集解析 → NotebookLM + Google Drive")
    elif is_direct_audio_episode_url(url):
        print(f"   模式: RedCircle 音频直链 → NotebookLM + Google Drive")
    elif no_notebooklm:
        print(f"   模式: 仅上传到 Google Drive")
    else:
        print(f"   模式: 上传到 NotebookLM + Google Drive")
    print()

    if len(urls) > 1:
        if len(urls) > 50 and not force_all:
            print(f"❌ 本次解析到 {len(urls)} 集，超过 50 集确认阈值。请加 --yes / --all 后再执行。")
            sys.exit(1)
        enable_notebooklm = (not local_only) and (not no_notebooklm)
        no_infographic = '--no-infographic' in sys.argv
        success_count = 0
        fail_count = 0
        for i, batch_url in enumerate(urls, start=1):
            print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            print(f"📍 [{i}/{len(urls)}] {batch_url}")
            print()
            try:
                result = download_single_episode(batch_url, local_only, enable_notebooklm=enable_notebooklm, no_infographic=no_infographic)
                if result not in (False, None):
                    success_count += 1
                else:
                    fail_count += 1
            except Exception as e:
                print(f"❌ 错误: {e}")
                fail_count += 1
            if i < len(urls):
                print("⏳ 等待 1 秒...")
                time.sleep(1)
            print()
        print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print(f"🎉 批量完成！")
        print(f"   ✅ 成功: {success_count}")
        print(f"   ❌ 失败: {fail_count}")
    elif 'xiaoyuzhoufm.com/podcast/' in url:
        if not force_all:
            resp = input("检测到播客主页(全集)。是否下载全部? [y/N]: ").strip().lower()
            if resp not in ('y', 'yes'):
                print("已取消。")
                sys.exit(0)
        download_podcast(url, local_only)
    elif 'xiaoyuzhoufm.com/episode/' in url or is_apple_episode_url(url) or is_pocketcasts_episode_url(url) or is_goodpods_episode_url(url) or is_direct_audio_episode_url(url):
        enable_notebooklm = (not local_only) and (not no_notebooklm)
        no_infographic = '--no-infographic' in sys.argv
        result = download_single_episode(url, local_only, enable_notebooklm=enable_notebooklm, no_infographic=no_infographic)
        if result in (False, None):
            sys.exit(1)
    else:
        print("❌ 无效输入（当前支持：小宇宙 /episode/、/podcast/，Apple Podcasts 单集链接，Pocket Casts 单集链接，Goodpods 单集链接，RedCircle 音频直链，以及如『聊聊 SCI 第821集』或『聊聊 SCI 100~150』的短语；或使用 --scan-drive-duplicates / --dedupe-drive）")
        sys.exit(1)

if __name__ == '__main__':
    main()
