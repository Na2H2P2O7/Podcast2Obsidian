#!/usr/bin/env python3
import argparse
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import time
import atexit
import fcntl
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import importlib.util

WORKSPACE = os.path.expanduser('~/.openclaw/workspace')
CORE_SCRIPT = f'{WORKSPACE}/skills/podcast2obsidian/scripts/xiaoyuzhou_dl.py'
OPENCLAW_CONFIG = os.path.expanduser('~/.openclaw/openclaw.json')
STATE_PATH = f'{WORKSPACE}/tmp/news_podcasts_daily_state.json'
LOG_PATH = f'{WORKSPACE}/tmp/news_podcasts_daily.log'
LOCK_PATH = f'{WORKSPACE}/tmp/news_podcasts_daily.lock'
CHAT_ID = '-100XXXXXXXXXX'
TZ = ZoneInfo('Asia/Shanghai')
QUERY_TIMEOUT_SECONDS = 180
QUERY_MAX_ATTEMPTS = 6
QUERY_RETRY_SLEEP_SECONDS = 30
# Newsletter sources are configured outside the code, not hardcoded here.
# Resolution order (see load_newsletter_sources):
#   1. --source URL          (repeatable, takes priority)
#   2. --sources-file PATH   (one URL per line; '#' starts a comment)
#   3. DEFAULT_SOURCES_PATH  (the default sources file below)
# A starter file lives at projects/podcast2obsidian/newsletter_sources.example.txt
DEFAULT_SOURCES_PATH = os.environ.get(
    'NEWS_SOURCES_FILE', f'{WORKSPACE}/projects/podcast2obsidian/newsletter_sources.txt'
)


def load_newsletter_sources(sources_file: str = '', extra_sources=None):
    """Resolve the list of newsletter podcast URLs to watch.

    Priority: explicit --source URLs > --sources-file / NEWS_SOURCES_FILE >
    DEFAULT_SOURCES_PATH. Inline '# name' comments and blank lines are ignored,
    and duplicates are removed while preserving order.
    """
    urls = []
    seen = set()

    def _add(raw):
        u = (raw or '').split('#', 1)[0].strip()
        if u and u not in seen:
            seen.add(u)
            urls.append(u)

    for u in (extra_sources or []):
        _add(u)

    if not urls:
        path = sources_file or DEFAULT_SOURCES_PATH
        if path and os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                for line in f:
                    if line.strip().startswith('#'):
                        continue
                    _add(line)
    return urls


def load_core():
    spec = importlib.util.spec_from_file_location('core', CORE_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def log(msg: str):
    Path(os.path.dirname(LOG_PATH)).mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now().isoformat()} {msg}"
    with open(LOG_PATH, 'a', encoding='utf-8') as f:
        f.write(line + '\n')
    print(msg)


_LOCK_HANDLE = None


def acquire_single_instance_lock(lock_path: str = LOCK_PATH):
    global _LOCK_HANDLE
    Path(os.path.dirname(lock_path)).mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, 'a+', encoding='utf-8')
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.seek(0)
        owner = handle.read().strip()
        if owner:
            log(f'skip: another news_podcasts_daily batch is already running ({owner})')
        else:
            log('skip: another news_podcasts_daily batch is already running')
        handle.close()
        return None

    handle.seek(0)
    handle.truncate()
    owner = json.dumps({
        'pid': os.getpid(),
        'started_at': datetime.now(timezone.utc).isoformat(),
    }, ensure_ascii=False)
    handle.write(owner)
    handle.flush()
    _LOCK_HANDLE = handle

    def _release():
        global _LOCK_HANDLE
        if _LOCK_HANDLE is None:
            return
        try:
            _LOCK_HANDLE.seek(0)
            _LOCK_HANDLE.truncate()
            fcntl.flock(_LOCK_HANDLE.fileno(), fcntl.LOCK_UN)
            _LOCK_HANDLE.close()
        except Exception:
            pass
        _LOCK_HANDLE = None

    atexit.register(_release)
    return handle


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument('--since-hours', type=int, default=24)
    p.add_argument('--state-path', default=STATE_PATH)
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--chat-id', default=CHAT_ID)
    p.add_argument('--only-url', default='', help='Replay exactly one episode URL from the configured newsletter sources, bypassing dedupe checks')
    p.add_argument('--source', action='append', default=[], help='Newsletter podcast URL to watch (repeatable). Overrides --sources-file.')
    p.add_argument('--sources-file', default='', help='Path to a file listing newsletter podcast URLs, one per line ("#" starts a comment). Defaults to NEWS_SOURCES_FILE or projects/podcast2obsidian/newsletter_sources.txt')
    return p.parse_args()


def parse_pub_date(raw: str):
    raw = (raw or '').strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace('Z', '+00:00')).astimezone(timezone.utc)
    except Exception:
        return None


def parse_query_answer(query_res):
    payload = json.loads(query_res.stdout or '{}')
    value = payload.get('value', payload)
    return (value.get('answer') or '').strip()


def looks_truncated_answer(answer: str):
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


def is_retryable_query_error(text: str):
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


def query_with_recovery(
    core,
    notebook_id: str,
    prompt: str,
    audio_source_id: str = '',
    *,
    enforce_complete: bool = True,
    max_attempts: int = QUERY_MAX_ATTEMPTS,
    retry_sleep_seconds: int = QUERY_RETRY_SLEEP_SECONDS,
):
    attempts = []
    last_error = ''
    for idx in range(max_attempts):
        attempt_no = idx + 1
        query_cmd = ['notebook', 'query', notebook_id, prompt, '--timeout', str(QUERY_TIMEOUT_SECONDS), '--json']
        if audio_source_id:
            query_cmd += ['--source-ids', audio_source_id]
        query_res = core.run_nlm_command(query_cmd)
        if query_res.returncode != 0:
            err = (query_res.stderr or query_res.stdout or '').strip()
            last_error = err or 'query failed'
            if attempt_no < max_attempts and is_retryable_query_error(last_error):
                log(f'recovery: notebook query attempt {attempt_no} got retryable error, wait {retry_sleep_seconds}s then retry')
                time.sleep(retry_sleep_seconds)
                continue
            raise RuntimeError(last_error)

        answer = parse_query_answer(query_res)
        if not answer:
            last_error = 'empty answer'
            if attempt_no < max_attempts:
                log(f'recovery: notebook query attempt {attempt_no} returned empty answer, wait {retry_sleep_seconds}s then retry')
                time.sleep(retry_sleep_seconds)
                continue
            raise RuntimeError(last_error)

        if not enforce_complete:
            if attempt_no > 1:
                log(f'recovery: notebook query attempt {attempt_no} returned an answer')
            return answer

        truncated, reason = looks_truncated_answer(answer)
        attempts.append({
            'answer': answer,
            'truncated': truncated,
            'reason': reason,
        })
        if not truncated:
            if attempt_no > 1:
                log(f'recovery: notebook query attempt {attempt_no} returned a complete answer')
            return answer
        if attempt_no < max_attempts:
            log(f'recovery: notebook query attempt {attempt_no} looks truncated ({reason}), wait {retry_sleep_seconds}s then retry')
            time.sleep(retry_sleep_seconds)

    if attempts:
        attempts.sort(key=lambda item: (not item['truncated'], len(item['answer'])), reverse=True)
        best = attempts[0]
        log(f"WARN notebook query still looks truncated after {max_attempts} attempts: {best['reason']}")
        return best['answer']
    raise RuntimeError(last_error or 'query failed without answer')


def load_state(path: str):
    default = {'processed_urls': {}, 'failed_urls': {}, 'updated_at': None}
    if not os.path.exists(path):
        return default
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get('processed_urls'), dict):
            if not isinstance(data.get('failed_urls'), dict):
                data['failed_urls'] = {}
            return data
    except Exception:
        pass
    return default


def save_state(path: str, state: dict):
    Path(os.path.dirname(path)).mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def prune_state(state: dict, ttl_hours: int = 168):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=ttl_hours)
    keep_processed = {}
    for url, ts in (state.get('processed_urls') or {}).items():
        dt = parse_pub_date(ts)
        if dt and dt >= cutoff:
            keep_processed[url] = dt.isoformat()
    state['processed_urls'] = keep_processed

    keep_failed = {}
    for url, payload in (state.get('failed_urls') or {}).items():
        if isinstance(payload, str):
            payload = {'ts': payload, 'reason': ''}
        ts = (payload or {}).get('ts') or ''
        dt = parse_pub_date(ts)
        if dt and dt >= cutoff:
            keep_failed[url] = {
                'ts': dt.isoformat(),
                'reason': (payload or {}).get('reason') or '',
            }
    state['failed_urls'] = keep_failed
    return state


def get_bot_token():
    result = subprocess.run(
        ['bash', '-lc', f"jq -r '.. | objects | select(has(\"botToken\")) | .botToken' '{OPENCLAW_CONFIG}' | head -n 1"],
        capture_output=True, text=True
    )
    token = (result.stdout or '').strip()
    if result.returncode != 0 or not token or token == 'null':
        raise RuntimeError('Telegram botToken not found')
    return token


def send_telegram(bot_token: str, chat_id: str, text: str):
    payload = {
        'chat_id': chat_id,
        'text': text,
        'disable_web_page_preview': True,
    }
    result = subprocess.run(
        ['curl', '-sS', '-X', 'POST', f'https://api.telegram.org/bot{bot_token}/sendMessage', '-H', 'Content-Type: application/json', '-d', json.dumps(payload, ensure_ascii=False)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout)
    body = json.loads(result.stdout or '{}')
    if not body.get('ok'):
        raise RuntimeError(result.stdout)
    return body


def extract_podcast_episodes(core, podcast_url: str):
    html = core.fetch_page(podcast_url)
    podcast_info = core.extract_podcast_info(html)
    title = (podcast_info.get('title') or '').strip()

    import re
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(\{.+?\})</script>', html, re.S)
    if not m:
        return title, []
    data = json.loads(m.group(1))
    podcast = data.get('props', {}).get('pageProps', {}).get('podcast', {})
    episodes = podcast.get('episodes') or []

    out = []
    for ep in episodes:
        eid = (ep.get('eid') or '').strip()
        if not eid:
            continue
        out.append({
            'podcast_name': title,
            'episode_url': f'https://www.xiaoyuzhoufm.com/episode/{eid}',
            'pub_date': ep.get('pubDate') or '',
            'title': ep.get('title') or '',
        })
    out.sort(key=lambda x: x['pub_date'])
    return title, out


def extract_podcast_and_recent_episodes(core, podcast_url: str, since_hours: int):
    title, episodes = extract_podcast_episodes(core, podcast_url)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    out = []
    for ep in episodes:
        pub = parse_pub_date(ep.get('pub_date') or '')
        if not pub or pub < cutoff:
            continue
        out.append(ep)
    return title, out


def find_episode_in_news_sources(core, episode_url: str, sources):
    target = (episode_url or '').strip()
    if not target:
        return None
    for podcast_url in sources:
        _, episodes = extract_podcast_episodes(core, podcast_url)
        for ep in episodes:
            if ep['episode_url'] == target:
                return ep
    return None


def process_episode(core, episode_url: str, podcast_name: str, pub_date: str, dry_run: bool = False):
    html = core.fetch_page(episode_url)
    info = core.extract_episode_info(html, episode_url)
    original_title = (info.get('title') or '').strip()
    notebook_title = core.build_episode_notebook_title(podcast_name, original_title, info.get('pub_date') or pub_date)
    push_line = core.build_news_push_line(podcast_name, original_title)

    if dry_run:
        return {
            'podcast_name': podcast_name,
            'episode_url': episode_url,
            'notebook_title': notebook_title,
            'note_path': f'Newsletters/{podcast_name}/{notebook_title}.md',
            'push_line': push_line,
        }

    os.environ['NLM_PROFILE'] = core.resolve_nlm_profile_for_podcast(podcast_name)

    if core.should_use_direct_markdown_for_podcast(podcast_name):
        result = core.save_direct_markdown_to_fast_note(
            podcast_name,
            original_title,
            episode_url=episode_url,
            info=info,
        )
        return {
            'podcast_name': podcast_name,
            'episode_url': episode_url,
            'notebook_title': notebook_title,
            'note_path': result['note_path'],
            'push_line': push_line,
        }

    summary_prompt = core.build_timeline_summary_prompt(info)
    audio_ext = core.infer_audio_extension(info['audio_url'])
    no_infographic = core.should_skip_infographics_for_podcast(podcast_name)

    with tempfile.TemporaryDirectory() as tmpdir:
        audio_path = os.path.join(tmpdir, core.sanitize_filename(original_title) + audio_ext)
        if not core.download_audio(info['audio_url'], audio_path):
            raise RuntimeError(f'audio download failed: {episode_url}')

        notebook_result = core.create_notebook_and_upload_to_notebooklm(
            audio_path,
            notebook_title,
            podcast_name=podcast_name,
            episode_title=original_title,
            note_context=info,
            episode_url=episode_url,
            summary_prompt=summary_prompt,
            no_infographic=no_infographic,
        )

    fast_note = ((notebook_result.get('summary') or {}).get('fast_note') or {})
    note_path = fast_note.get('note_path') or ''
    if not notebook_result.get('ok') or not note_path:
        raise RuntimeError(notebook_result.get('reason') or notebook_result.get('status') or 'notebook pipeline incomplete')

    return {
        'podcast_name': podcast_name,
        'episode_url': episode_url,
        'notebook_title': notebook_title,
        'note_path': note_path,
        'push_line': push_line,
    }


def main():
    args = parse_args()
    lock_handle = acquire_single_instance_lock()
    if lock_handle is None:
        return 0
    sources = load_newsletter_sources(args.sources_file, args.source)
    if not sources:
        log('no newsletter sources configured: pass --source URL, --sources-file PATH, '
            'or create projects/podcast2obsidian/newsletter_sources.txt '
            '(see newsletter_sources.example.txt)')
        return 1
    core = load_core()
    state = prune_state(load_state(args.state_path))
    processed_urls = state.get('processed_urls', {})
    failed_urls = state.get('failed_urls', {})
    found = []

    if args.only_url:
        ep = find_episode_in_news_sources(core, args.only_url, sources)
        if not ep:
            raise RuntimeError(f'episode not found in configured newsletter sources: {args.only_url}')
        found = [ep]
    else:
        for podcast_url in sources:
            podcast_name, episodes = extract_podcast_and_recent_episodes(core, podcast_url, args.since_hours)
            for ep in episodes:
                if ep['episode_url'] in processed_urls:
                    continue
                found.append(ep)

    if not found:
        if args.only_url:
            log(f'no matching episode found for replay: {args.only_url}')
        else:
            log(f'no updates in last {args.since_hours}h')
        save_state(args.state_path, state)
        return 0

    results = []
    failures = []
    for ep in found:
        log(f"processing {ep['podcast_name']} -> {ep['episode_url']}")
        try:
            result = process_episode(core, ep['episode_url'], ep['podcast_name'], ep.get('pub_date') or '', dry_run=args.dry_run)
            result['status'] = 'success'
            results.append(result)
            if not args.dry_run:
                processed_urls[ep['episode_url']] = datetime.now(timezone.utc).isoformat()
                failed_urls.pop(ep['episode_url'], None)
        except Exception as e:
            reason = str(e).strip() or 'unknown_error'
            log(f"WARN episode failed but batch continues: {ep['podcast_name']} -> {ep['episode_url']} :: {reason}")
            failure = {
                'status': 'failed',
                'podcast_name': ep['podcast_name'],
                'episode_url': ep['episode_url'],
                'push_line': ep.get('title') or ep['episode_url'],
                'reason': reason,
            }
            failures.append(failure)
            if not args.dry_run:
                failed_urls[ep['episode_url']] = {
                    'ts': datetime.now(timezone.utc).isoformat(),
                    'reason': reason,
                }

    if args.dry_run:
        print(json.dumps({'successes': results, 'failures': failures}, ensure_ascii=False, indent=2))
        return 0

    bot_token = get_bot_token()
    today = datetime.now(TZ).strftime('%Y%m%d')
    lines = [f'{today}的新闻:']
    if results:
        lines.extend(item['push_line'] for item in results)
    else:
        lines.append('（本轮无成功入库条目）')

    if failures:
        lines.append('')
        lines.append(f'本轮有 {len(failures)} 条待重试：')
        for item in failures:
            lines.append(f"- {item['podcast_name']}: {item['reason']}")

    lines.append('')
    lines.append('已保存到Vault。')
    text = '\n'.join(lines)
    send_telegram(bot_token, args.chat_id, text)
    state['processed_urls'] = processed_urls
    state['failed_urls'] = failed_urls
    state['updated_at'] = datetime.now(timezone.utc).isoformat()
    save_state(args.state_path, state)
    log(f'sent {len(results)} success update(s) to {args.chat_id}; failures={len(failures)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
