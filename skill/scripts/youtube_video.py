#!/usr/bin/env python3
"""YouTube video ingestion for core.

Fetches the best original-language YouTube transcript, uploads it as a NotebookLM
text source, then saves Bilibili-shaped Markdown to Fast Note under Video/<Channel>/.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, quote, urlparse

from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import xiaoyuzhou_dl as core  # noqa: E402

DEFAULT_FAST_NOTE_ROOT = os.environ.get('YOUTUBE_FAST_NOTE_ROOT', 'Video')
YOUTUBE_UA = os.environ.get('YOUTUBE_UA', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36')


def run_curl_text(url: str, timeout: int = 40) -> str:
    cmd = ['curl', '-fsSL', '--compressed', '-A', YOUTUBE_UA, url]
    res = subprocess.run(cmd, capture_output=True, text=True, errors='replace', timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or res.stdout.strip() or f'curl failed: {url}')
    return res.stdout


def run_curl_json(url: str, timeout: int = 40) -> dict[str, Any]:
    return json.loads(run_curl_text(url, timeout=timeout))


def extract_video_id(target: str) -> str:
    text = (target or '').strip()
    if re.fullmatch(r'[0-9A-Za-z_-]{11}', text):
        return text
    parsed = urlparse(text)
    host = parsed.netloc.lower()
    if 'youtu.be' in host:
        vid = parsed.path.strip('/').split('/')[0]
        return vid if re.fullmatch(r'[0-9A-Za-z_-]{11}', vid or '') else ''
    if 'youtube.com' in host:
        qs = parse_qs(parsed.query)
        if qs.get('v'):
            vid = qs['v'][0]
            return vid if re.fullmatch(r'[0-9A-Za-z_-]{11}', vid or '') else ''
        m = re.search(r'/(?:embed|shorts|live)/([0-9A-Za-z_-]{11})', parsed.path)
        if m:
            return m.group(1)
    m = re.search(r'(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([0-9A-Za-z_-]{11})', text)
    return m.group(1) if m else ''


def normalize_video_url(target: str) -> str:
    vid = extract_video_id(target)
    if not vid:
        raise ValueError('无法解析 YouTube video id')
    return f'https://www.youtube.com/watch?v={vid}'


def json_unescape(value: str) -> str:
    try:
        return json.loads('"' + value + '"')
    except Exception:
        return value.replace('\\n', '\n').replace('\\"', '"')


def date_from_youtube_value(value: str) -> str:
    value = (value or '').strip()
    if not value:
        return ''
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return value
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).strftime('%Y-%m-%d')
    except Exception:
        return value[:10] if re.match(r'\d{4}-\d{2}-\d{2}', value) else ''


def fetch_video_meta(video_id: str) -> dict[str, Any]:
    url = f'https://www.youtube.com/watch?v={video_id}'
    oembed: dict[str, Any] = {}
    try:
        oembed = run_curl_json(f'https://www.youtube.com/oembed?url={quote(url, safe=":/")}&format=json')
    except Exception as e:
        print(f'⚠️ YouTube oEmbed 失败（继续）: {e}')

    page = ''
    try:
        page = run_curl_text(url)
    except Exception as e:
        print(f'⚠️ YouTube page metadata 失败（继续）: {e}')

    def find(pattern: str) -> str:
        if not page:
            return ''
        m = re.search(pattern, page)
        return json_unescape(m.group(1)) if m else ''

    title = oembed.get('title') or find(r'"title":"((?:[^"\\]|\\.)*)"') or 'YouTube Video'
    channel = oembed.get('author_name') or find(r'"ownerChannelName":"((?:[^"\\]|\\.)*)"') or 'unknown'
    publish = date_from_youtube_value(find(r'"publishDate":"([^"]+)"') or find(r'"uploadDate":"([^"]+)"'))
    desc = find(r'"shortDescription":"((?:[^"\\]|\\.)*)"')
    thumb = oembed.get('thumbnail_url') or f'https://i.ytimg.com/vi/{video_id}/hqdefault.jpg'
    return {
        'video_id': video_id,
        'title': str(title).strip(),
        'author': str(channel).strip(),
        'description': str(desc or '').strip(),
        'pub_date': publish,
        'cover_url': str(thumb or '').strip(),
        'url': url,
    }


def yaml_quote(value: Any) -> str:
    text = str(value or '')
    return "'" + text.replace("'", "''") + "'"


def format_timestamp(seconds: Any, with_hours: bool = False) -> str:
    try:
        total = max(0, int(float(seconds or 0)))
    except Exception:
        total = 0
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f'{h:02d}:{m:02d}:{s:02d}' if with_hours or h else f'{m:02d}:{s:02d}'


def choose_best_transcript(video_id: str):
    api = YouTubeTranscriptApi()
    transcript_list = api.list(video_id)
    transcripts = list(transcript_list)
    if not transcripts:
        raise RuntimeError('没有可用 YouTube 字幕')

    # Keep YouTube/API language order as the original/default-language signal.
    ordered_langs: list[str] = []
    by_lang: dict[str, list[Any]] = {}
    for tr in transcripts:
        code = getattr(tr, 'language_code', '') or ''
        if code not in by_lang:
            by_lang[code] = []
            ordered_langs.append(code)
        by_lang[code].append(tr)

    for code in ordered_langs:
        same_lang = by_lang[code]
        manual = [t for t in same_lang if not getattr(t, 'is_generated', False)]
        if manual:
            return manual[0]
        generated = [t for t in same_lang if getattr(t, 'is_generated', False)]
        if generated:
            return generated[0]

    return transcripts[0]


def fetch_transcript_body(video_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    tr = choose_best_transcript(video_id)
    fetched = tr.fetch()
    raw = fetched.to_raw_data()
    body = [
        {
            'from': float(item.get('start') or 0),
            'to': float(item.get('start') or 0) + float(item.get('duration') or 0),
            'content': str(item.get('text') or '').strip(),
        }
        for item in raw
        if str(item.get('text') or '').strip()
    ]
    info = {
        'language': getattr(tr, 'language', '') or getattr(fetched, 'language', ''),
        'language_code': getattr(tr, 'language_code', '') or getattr(fetched, 'language_code', ''),
        'is_generated': bool(getattr(tr, 'is_generated', False)),
        'source': 'auto-generated' if bool(getattr(tr, 'is_generated', False)) else 'uploaded',
    }
    return body, info


def transcript_plain_text(body: list[dict[str, Any]]) -> str:
    return '\n'.join(str(item.get('content') or '').strip() for item in body or [] if str(item.get('content') or '').strip()).strip()


def transcript_with_timestamps(body: list[dict[str, Any]]) -> str:
    with_hours = any(float(item.get('from') or 0) >= 3600 for item in body or [])
    lines = []
    for item in body or []:
        text = str(item.get('content') or '').strip()
        if text:
            lines.append(f'[{format_timestamp(item.get("from"), with_hours)}] {text}')
    return '\n'.join(lines).strip()


def is_chinese_subtitle(subtitle_info: dict[str, Any]) -> bool:
    code = (subtitle_info.get('language_code') or '').lower()
    language = (subtitle_info.get('language') or '').lower()
    return code.startswith('zh') or 'chinese' in language or '中文' in language or '汉语' in language


def build_youtube_prompt(subtitle_info: Optional[dict[str, Any]] = None) -> str:
    subtitle_info = subtitle_info or {}
    if is_chinese_subtitle(subtitle_info):
        lang_instruction = '字幕是中文，请用中文输出。'
        summary_lang = '中文'
    else:
        lang = subtitle_info.get('language') or subtitle_info.get('language_code') or '外文'
        lang_instruction = f'检测到字幕语言是 {lang}，不是中文；请必须用中文输出总结，不要沿用原文语言。'
        summary_lang = '中文'
    return f'请根据这个 YouTube 视频内容进行结构化总结，包含二级标题，按原顺序输出，必要时使用三级标题，三级标题下请写简洁要点，避免过长整段。{lang_instruction} 不要写导言、结语、编者按或“以下是总结”等套话，直接从第一个二级标题开始。'


def build_youtube_source_text(meta: dict[str, Any], body: list[dict[str, Any]], subtitle_info: dict[str, Any]) -> str:
    parts = [
        f"标题：{meta.get('title') or ''}",
        f"Channel：{meta.get('author') or ''}",
        f"URL：{meta.get('url') or ''}",
        f"Subtitle：{subtitle_info.get('language') or ''} / {subtitle_info.get('language_code') or ''} / {subtitle_info.get('source') or ''}",
    ]
    if meta.get('description'):
        parts.append('简介：\n' + str(meta.get('description') or '').strip())
    transcript = transcript_with_timestamps(body)
    if transcript:
        parts.append('字幕全文：\n' + transcript)
    return '\n\n'.join(part.strip() for part in parts if part and part.strip())


def note_title_for_video(meta: dict[str, Any]) -> str:
    title = meta.get('title') or 'YouTube Video'
    author = (meta.get('author') or '').strip()
    if author:
        title = f'{author} {title}'
    if meta.get('pub_date'):
        title = f'{meta.get("pub_date")} {title}'
    return core.normalize_markdown_note_title(title)


def notebook_title_for_youtube(meta: dict[str, Any]) -> str:
    return core.build_episode_notebook_title(podcast_name_for_youtube(meta), note_title_for_video(meta), meta.get('pub_date') or '')


def podcast_name_for_youtube(meta: dict[str, Any]) -> str:
    author = (meta.get('author') or '').strip()
    return f'YouTube - {author}' if author else 'YouTube'


def build_youtube_markdown(meta: dict[str, Any], *, answer: str = '', summary: str = '', transcript_body: Optional[list[dict[str, Any]]] = None, subtitle_info: Optional[dict[str, Any]] = None) -> str:
    title = meta.get('title') or 'YouTube Video'
    author = meta.get('author') or 'unknown'
    pub_date = meta.get('pub_date') or ''
    video_id = meta.get('video_id') or ''
    video_url = meta.get('url') or f'https://www.youtube.com/watch?v={video_id}'
    subtitle_info = subtitle_info or {}

    parts: list[str] = []
    parts.append('\n'.join([
        '---',
        'tags:',
        '  - YouTube',
        f'channel: {yaml_quote(author)}',
        f'title: {yaml_quote(f"[{title}]({video_url})")}',
        f'video_id: {yaml_quote(video_id)}',
        'subtitle: yes',
        *( [f'subtitle_language: {yaml_quote(subtitle_info.get("language_code") or subtitle_info.get("language") or "")}' ] if subtitle_info else [] ),
        *( [f'subtitle_source: {yaml_quote(subtitle_info.get("source") or "")}' ] if subtitle_info else [] ),
        *( [f'Release Date: {pub_date}'] if pub_date else [] ),
        '---',
    ]))

    cover = meta.get('cover_url') or ''
    if cover:
        parts.append(f'![]({cover})')

    parts.append(
        '<iframe src="https://www.youtube.com/embed/' + quote(str(video_id)) + '" '
        'scrolling="no" border="0" frameborder="no" framespacing="0" '
        'allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share; fullscreen" '
        'allowfullscreen="true" style="height:100%;width:100%; aspect-ratio: 16 / 9;"> </iframe>'
    )

    desc = (meta.get('description') or '').strip()
    if desc:
        parts.append('## 简介\n\n' + desc)

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


def ensure_youtube_nlm_profile() -> None:
    os.environ['NLM_PROFILE'] = (os.environ.get('NLM_PROFILE') or 'secondary').strip() or 'secondary'


def create_or_reuse_notebook(notebook_title: str) -> Optional[str]:
    return core.create_or_reuse_notebook(notebook_title)


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


def save_youtube_summary_to_fast_note(meta: dict[str, Any], *, answer: str, summary: str = '', transcript_body: Optional[list[dict[str, Any]]] = None, subtitle_info: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    author = core.normalize_fast_note_segment(meta.get('author') or 'unknown')
    folder_path = f'{DEFAULT_FAST_NOTE_ROOT}/{author}'
    markdown = build_youtube_markdown(meta, answer=answer, summary=summary, transcript_body=transcript_body, subtitle_info=subtitle_info)
    result = core.upsert_fast_note_markdown(folder_path, note_title_for_video(meta), markdown)
    result['clean_answer'] = markdown
    return result


def run_youtube_notebooklm_summary(notebook_id: str, source_id: Optional[str], meta: dict[str, Any], body: list[dict[str, Any]], subtitle_info: dict[str, Any], *, no_infographic: bool = False) -> dict[str, Any]:
    result = {'ok': False, 'query_ok': False, 'infographic_ok': True, 'fast_note': None, 'answer': '', 'summary': ''}
    if not core.ensure_nlm_auth():
        result['infographic_ok'] = False
        return result

    print('🧠 配置 NotebookLM 回答长度: longer')
    cfg_res = core.run_nlm_command(['chat', 'configure', notebook_id, '--goal', 'default', '--response-length', 'longer'])
    if cfg_res.returncode != 0:
        print(f"⚠️ 配置回答长度失败: {cfg_res.stderr.strip() or cfg_res.stdout.strip()}")

    prompt = build_youtube_prompt(subtitle_info)
    print(f'💬 触发 YouTube 总结: {prompt[:120]}')
    try:
        answer = core.run_notebook_query_with_recovery(notebook_id, prompt, audio_source_id=source_id or None, enforce_complete=True)
        result['answer'] = answer
        result['query_ok'] = True
        print('✅ 已拿到 NotebookLM 总结文本')
        try:
            summary_text = core.run_notebook_query_with_recovery(
                notebook_id,
                '请用中文写一段 120-180 字概括这个视频的核心内容，直接输出摘要正文，不要标题，不要项目符号。',
                audio_source_id=source_id or None,
                enforce_complete=False,
            )
            result['summary'] = core.clean_notebooklm_answer(summary_text).strip()
        except Exception as e:
            print(f'⚠️ 摘要生成失败（继续）: {e}')
        fast_note = save_youtube_summary_to_fast_note(meta, answer=answer, summary=result['summary'], transcript_body=body, subtitle_info=subtitle_info)
        result['fast_note'] = fast_note
        result['ok'] = True
        print(f"📝 已保存到 Fast Note: {fast_note['note_path']} (note_id={fast_note['note_id']})")
    except Exception as e:
        print(f'⚠️ YouTube NotebookLM 总结失败: {e}')

    if not no_infographic:
        result['infographic_ok'] = core.create_default_infographics(notebook_id, result.get('fast_note'))
    return result


def write_local_artifacts(out_dir: Path, meta: dict[str, Any], markdown: str, body: list[dict[str, Any]], subtitle_info: dict[str, Any]) -> Path:
    author = core.sanitize_filename(meta.get('author') or 'unknown')
    title = core.sanitize_filename(note_title_for_video(meta))
    target_dir = out_dir / 'Video' / author / title
    target_dir.mkdir(parents=True, exist_ok=True)
    md_path = target_dir / f'{title}.md'
    md_path.write_text(markdown, encoding='utf-8')
    (target_dir / 'metadata.json').write_text(json.dumps({'meta': meta, 'subtitle': subtitle_info}, ensure_ascii=False, indent=2), encoding='utf-8')
    (target_dir / 'subtitle.json').write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding='utf-8')
    return md_path


def ingest(target: str, *, local: bool = False, no_notebooklm: bool = False, no_infographic: bool = False) -> str | bool:
    video_id = extract_video_id(target)
    if not video_id:
        raise RuntimeError('无法解析 YouTube video id')
    print('▶️ YouTube → core')
    print(f'   URL: https://www.youtube.com/watch?v={video_id}')
    print('🔍 获取视频信息...')
    meta = fetch_video_meta(video_id)
    print(f'   📻 节目: {podcast_name_for_youtube(meta)}')
    print(f'   📝 标题: {meta.get("title") or "unknown"}')
    print(f'   👤 Channel: {meta.get("author") or "unknown"}')
    if meta.get('pub_date'):
        print(f'   📅 发布: {meta.get("pub_date")}')
    print()

    print('🔎 获取 YouTube 字幕...')
    body, subtitle_info = fetch_transcript_body(video_id)
    print(f"   字幕: {subtitle_info.get('language') or ''} / {subtitle_info.get('language_code') or ''} / {subtitle_info.get('source') or ''} / lines={len(body)}")

    preview_markdown = build_youtube_markdown(meta, transcript_body=body, subtitle_info=subtitle_info)
    if local:
        out = write_local_artifacts(Path.home() / 'Downloads', meta, preview_markdown, body, subtitle_info)
        print(f'🎉 已保存本地 Markdown 预览: {out}')
        print(f'📣 RESULT status=success mode=subtitle-local notebook_id=none note_id=none note_path={out} video_id={video_id} subtitle=yes subtitle_language={subtitle_info.get("language_code") or ""} subtitle_source={subtitle_info.get("source") or ""}')
        return 'subtitle_local'
    if no_notebooklm:
        print('⏭️ 已设置 --no-notebooklm：不上传 NotebookLM')
        print(f'📣 RESULT status=skipped mode=subtitle notebook_id=none note_id=none note_path=none reason=no_notebooklm video_id={video_id} subtitle=yes')
        return 'subtitle_skipped'

    ensure_youtube_nlm_profile()
    notebook_id = create_or_reuse_notebook(notebook_title_for_youtube(meta))
    if not notebook_id:
        print(f'📣 RESULT status=partial mode=subtitle notebook_id=none note_id=none note_path=none reason=notebook_create_failed video_id={video_id} subtitle=yes')
        return False
    source_text = build_youtube_source_text(meta, body, subtitle_info)
    source_title = f'{note_title_for_video(meta)} - Transcript'
    source_id = add_text_source_to_notebook(notebook_id, source_title, source_text)
    if source_id is None:
        print(f'📣 RESULT status=partial mode=subtitle notebook_id={notebook_id} note_id=none note_path=none reason=text_source_add_failed video_id={video_id} subtitle=yes')
        return False

    summary = run_youtube_notebooklm_summary(notebook_id, source_id or None, meta, body, subtitle_info, no_infographic=no_infographic)
    fn = summary.get('fast_note')
    if summary.get('ok') and fn:
        print(f'📣 RESULT status=success mode=subtitle notebook_id={notebook_id} note_id={fn["note_id"]} note_path={fn["note_path"]} query_ok=1 fast_note_ok=1 infographic_ok={1 if summary.get("infographic_ok") else 0} video_id={video_id} subtitle=yes subtitle_language={subtitle_info.get("language_code") or ""} subtitle_source={subtitle_info.get("source") or ""}')
        return 'subtitle_notebooklm'
    print(f'📣 RESULT status=partial mode=subtitle notebook_id={notebook_id} note_id=none note_path=none query_ok={1 if summary.get("query_ok") else 0} fast_note_ok={1 if fn else 0} infographic_ok={1 if summary.get("infographic_ok") else 0} reason="query_or_fast_note_incomplete" video_id={video_id} subtitle=yes')
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description='YouTube video → Fast Note / NotebookLM for core')
    parser.add_argument('target', help='YouTube video URL or video id')
    parser.add_argument('--local', action='store_true', help='Save artifacts locally under ~/Downloads; no Fast Note/NotebookLM writes')
    parser.add_argument('--no-notebooklm', action='store_true', help='Skip NotebookLM upload')
    parser.add_argument('--no-infographic', action='store_true', help='Skip NotebookLM infographic creation')
    args = parser.parse_args()
    try:
        ingest(args.target, local=args.local, no_notebooklm=args.no_notebooklm, no_infographic=args.no_infographic)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        print(f'❌ 错误: {exc}')
        sys.exit(1)


if __name__ == '__main__':
    main()
