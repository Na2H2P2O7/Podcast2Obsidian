#!/usr/bin/env python3
import argparse
import atexit
import fcntl
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import importlib.util

WORKSPACE = os.path.expanduser('~/.openclaw/workspace')
CORE_SCRIPT = f'{WORKSPACE}/skills/podcast2obsidian/scripts/xiaoyuzhou_dl.py'
STATE_PATH = f'{WORKSPACE}/tmp/specific_podcasts_daily_state.json'
LOG_PATH = f'{WORKSPACE}/tmp/specific_podcasts_daily.log'
LOCK_PATH = f'{WORKSPACE}/tmp/specific_podcasts_daily.lock'
# Watched podcasts are configured outside the code (not hardcoded). Resolution:
#   1. --source URL          (repeatable, highest priority)
#   2. --sources-file PATH   (one URL per line; '#' starts a comment)
#   3. DEFAULT_SOURCES_PATH / SPECIFIC_PODCASTS_FILE env var
# Starter file: projects/podcast2obsidian/specific_podcasts.example.txt
DEFAULT_SOURCES_PATH = os.environ.get(
    'SPECIFIC_PODCASTS_FILE', f'{WORKSPACE}/projects/podcast2obsidian/specific_podcasts.txt'
)


def load_sources(sources_file: str = '', extra_sources=None):
    """Resolve the list of podcast URLs to watch (see DEFAULT_SOURCES_PATH)."""
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
    """单实例锁：防止两个 launchd 触发重叠导致 state 互相覆盖、重复下载。"""
    global _LOCK_HANDLE
    Path(os.path.dirname(lock_path)).mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, 'a+', encoding='utf-8')
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.seek(0)
        owner = handle.read().strip()
        log(f'skip: another specific_podcasts_daily batch is already running ({owner})' if owner
            else 'skip: another specific_podcasts_daily batch is already running')
        handle.close()
        return None

    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps({
        'pid': os.getpid(),
        'started_at': datetime.now(timezone.utc).isoformat(),
    }, ensure_ascii=False))
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
    p.add_argument('--since-hours', type=int, default=72)
    p.add_argument('--state-path', default=STATE_PATH)
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--source', action='append', default=[], help='Podcast URL to watch (repeatable). Overrides --sources-file.')
    p.add_argument('--sources-file', default='', help='Path to a file listing podcast URLs, one per line ("#" starts a comment). Defaults to SPECIFIC_PODCASTS_FILE or projects/podcast2obsidian/specific_podcasts.txt')
    return p.parse_args()


def parse_pub_date(raw: str):
    raw = (raw or '').strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace('Z', '+00:00')).astimezone(timezone.utc)
    except Exception:
        return None


def load_state(path: str):
    if not os.path.exists(path):
        return {'processed_urls': {}, 'updated_at': None}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        # 文件存在但损坏：绝不静默回退空 state（会重复下载 / 重复上传）。
        raise RuntimeError(f'state file 损坏，拒绝以空状态继续（会重复处理）: {path}: {e}')
    if isinstance(data, dict) and isinstance(data.get('processed_urls'), dict):
        return data
    raise RuntimeError(f'state file 结构非法，拒绝以空状态继续: {path}')


def save_state(path: str, state: dict):
    Path(os.path.dirname(path)).mkdir(parents=True, exist_ok=True)
    # 原子写：先写 .tmp 再 os.replace，避免写到一半被杀留下截断文件。
    tmp = f'{path}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def prune_state(state: dict, ttl_hours: int = 720):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=ttl_hours)
    keep = {}
    for url, ts in (state.get('processed_urls') or {}).items():
        dt = parse_pub_date(ts)
        if dt and dt >= cutoff:
            keep[url] = dt.isoformat()
    state['processed_urls'] = keep
    return state


def extract_podcast_and_recent_episodes(core, podcast_url: str, since_hours: int):
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

    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    out = []
    for ep in episodes:
        eid = (ep.get('eid') or '').strip()
        if not eid:
            continue
        pub = parse_pub_date(ep.get('pubDate') or '')
        if not pub or pub < cutoff:
            continue
        out.append({
            'podcast_name': title,
            'episode_url': f'https://www.xiaoyuzhoufm.com/episode/{eid}',
            'pub_date': ep.get('pubDate') or '',
            'title': ep.get('title') or '',
        })
    out.sort(key=lambda x: x['pub_date'])
    return title, out


def process_episode(core, episode_url: str, dry_run: bool = False):
    html = core.fetch_page(episode_url)
    info = core.extract_episode_info(html, episode_url)
    podcast_name = (info.get('podcast_name') or '').strip()
    title = (info.get('title') or '').strip()
    if dry_run:
        root_path = core.resolve_fast_note_root_for_podcast(podcast_name)
        route = core.get_podcast_route(podcast_name)
        title_mode = route.get('note_title_mode')
        if title_mode == 'title_only':
            note_path = f"{root_path}/{core.normalize_markdown_note_title(title)}.md"
        elif title_mode == 'title_only_in_podcast_folder':
            note_path = f"{root_path}/{core.normalize_fast_note_segment(podcast_name)}/{core.normalize_markdown_note_title(title)}.md"
        else:
            note_path = f"{root_path}/{core.normalize_fast_note_segment(podcast_name)}/{core.build_episode_notebook_title(podcast_name, title, info.get('pub_date') or '')}.md"
        return {
            'podcast_name': podcast_name,
            'episode_url': episode_url,
            'title': title,
            'note_path': note_path,
            'route': route,
        }

    result = core.download_single_episode(episode_url, local_only=False, enable_notebooklm=True)
    if result is False:
        raise RuntimeError(f'episode processing failed: {episode_url}')
    return {
        'podcast_name': podcast_name,
        'episode_url': episode_url,
        'title': title,
        'status': result,
    }


def main():
    args = parse_args()
    sources = load_sources(args.sources_file, args.source)
    if not sources:
        log('no podcast sources configured: pass --source URL, --sources-file PATH, '
            'or create projects/podcast2obsidian/specific_podcasts.txt '
            '(see specific_podcasts.example.txt)')
        return 1
    if not args.dry_run and acquire_single_instance_lock() is None:
        return 0
    core = load_core()
    state = prune_state(load_state(args.state_path))
    processed_urls = state.get('processed_urls', {})
    found = []

    for podcast_url in sources:
        podcast_name, episodes = extract_podcast_and_recent_episodes(core, podcast_url, args.since_hours)
        log(f'scan {podcast_name or podcast_url}: {len(episodes)} candidate(s) in last {args.since_hours}h')
        for ep in episodes:
            if ep['episode_url'] in processed_urls:
                continue
            found.append(ep)

    if not found:
        log(f'no updates in last {args.since_hours}h')
        save_state(args.state_path, state)
        return 0

    results = []
    fail_count = 0
    for ep in found:
        log(f"processing {ep['podcast_name']} -> {ep['episode_url']}")
        try:
            result = process_episode(core, ep['episode_url'], dry_run=args.dry_run)
        except Exception as e:
            # 单集失败不应连带丢掉本轮已完成单集的去重进度。
            fail_count += 1
            log(f"error processing {ep['episode_url']}: {e}")
            continue
        results.append(result)
        if not args.dry_run:
            processed_urls[ep['episode_url']] = datetime.now(timezone.utc).isoformat()
            # 每成功一集就落盘，崩溃/被杀也不会重跑已完成单集。
            state['processed_urls'] = processed_urls
            state['updated_at'] = datetime.now(timezone.utc).isoformat()
            save_state(args.state_path, state)

    if args.dry_run:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    state['processed_urls'] = processed_urls
    state['updated_at'] = datetime.now(timezone.utc).isoformat()
    save_state(args.state_path, state)
    log(f'processed {len(results)} update(s), {fail_count} failed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
