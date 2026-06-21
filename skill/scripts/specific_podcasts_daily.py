#!/usr/bin/env python3
import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import importlib.util

WORKSPACE = os.path.expanduser('~/.openclaw/workspace')
CORE_SCRIPT = f'{WORKSPACE}/skills/podcast2obsidian/scripts/xiaoyuzhou_dl.py'
STATE_PATH = f'{WORKSPACE}/tmp/specific_podcasts_daily_state.json'
LOG_PATH = f'{WORKSPACE}/tmp/specific_podcasts_daily.log'
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
        if isinstance(data, dict) and isinstance(data.get('processed_urls'), dict):
            return data
    except Exception:
        pass
    return {'processed_urls': {}, 'updated_at': None}


def save_state(path: str, state: dict):
    Path(os.path.dirname(path)).mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


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
    for ep in found:
        log(f"processing {ep['podcast_name']} -> {ep['episode_url']}")
        result = process_episode(core, ep['episode_url'], dry_run=args.dry_run)
        results.append(result)
        if not args.dry_run:
            processed_urls[ep['episode_url']] = datetime.now(timezone.utc).isoformat()

    if args.dry_run:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    state['processed_urls'] = processed_urls
    state['updated_at'] = datetime.now(timezone.utc).isoformat()
    save_state(args.state_path, state)
    log(f'processed {len(results)} update(s)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
