#!/usr/bin/env python3
"""Backfill NotebookLM infographic embeds into Fast Note podcast notes.

Default mode is read-only. Use --apply to download/attach existing artifacts or
trigger missing NotebookLM infographics.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any


WORKSPACE = Path(__file__).resolve().parents[3]
CORE_SCRIPT_DIR = WORKSPACE / "skills" / "podcast2obsidian" / "scripts"
sys.path.insert(0, str(CORE_SCRIPT_DIR))
os.environ["PATH"] = (
    "/usr/local/bin:/opt/homebrew/bin:"
    f"{Path.home()}/.local/bin:"
    f"{Path.home()}/.local/venvs/openclaw-tools/bin:"
    + os.environ.get("PATH", "")
)

import xiaoyuzhou_dl as core  # noqa: E402


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
COMPLETED = {"completed", "complete", "ready", "done"}


def log_event(path: Path | None, event: str, **fields: Any) -> None:
    payload = {"event": event, "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    payload.update(fields)
    line = json.dumps(payload, ensure_ascii=False)
    print(line, flush=True)
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def read_note_content(note_id: int) -> str:
    note_file = Path(core.FAST_NOTE_VAULT_ROOT) / f"n_{note_id}" / "content.txt"
    if note_file.exists():
        return note_file.read_text(encoding="utf-8")
    con = sqlite3.connect(core.FAST_NOTE_NOTE_DB)
    try:
        row = con.execute("select content from note where id=?", (note_id,)).fetchone()
    finally:
        con.close()
    return (row[0] or "") if row else ""


def latest_podcast_notes(root: str) -> list[dict[str, Any]]:
    con = sqlite3.connect(core.FAST_NOTE_NOTE_DB)
    try:
        rows = list(
            con.execute(
                "select id,path from note "
                "where vault_id=1 and rename=0 and path like ? and path like ? "
                "order by id",
                (f"{root}/%", "%.md"),
            )
        )
    finally:
        con.close()

    latest: dict[str, int] = {}
    for note_id, note_path in rows:
        latest[str(note_path)] = int(note_id)
    return [{"note_id": note_id, "note_path": path} for path, note_id in sorted(latest.items())]


def load_file_index() -> tuple[dict[str, tuple[int, int]], dict[str, tuple[int, int, str]]]:
    con = sqlite3.connect(core.FAST_NOTE_FILE_DB)
    try:
        rows = list(
            con.execute(
                "select id,path,size from file where vault_id=1 and rename=0"
            )
        )
    finally:
        con.close()
    by_path = {str(path): (int(file_id), int(size or 0)) for file_id, path, size in rows}
    by_base = {
        Path(str(path)).name: (int(file_id), int(size or 0), str(path))
        for file_id, path, size in rows
    }
    return by_path, by_base


def large_image_hit(
    embed_name: str,
    by_path: dict[str, tuple[int, int]],
    by_base: dict[str, tuple[int, int, str]],
    min_bytes: int,
) -> dict[str, Any] | None:
    name = embed_name.strip()
    base = Path(name).name
    if Path(base).suffix.lower() not in IMAGE_EXTENSIONS:
        return None

    candidates: list[tuple[str, int, int]] = []
    if name in by_path:
        file_id, size = by_path[name]
        candidates.append((name, file_id, size))
    attachment_path = f"attachments/{base}"
    if attachment_path in by_path:
        file_id, size = by_path[attachment_path]
        candidates.append((attachment_path, file_id, size))
    if base in by_base:
        file_id, size, path = by_base[base]
        candidates.append((path, file_id, size))

    for path, file_id, size in candidates:
        if size >= min_bytes:
            return {"file_id": file_id, "path": path, "size": size}
    return None


def note_large_image_hits(note_id: int, by_path: dict[str, tuple[int, int]], by_base: dict[str, tuple[int, int, str]], min_bytes: int) -> list[dict[str, Any]]:
    content = read_note_content(note_id)
    hits = []
    for embed in re.findall(r"!\[\[([^\]\|#]+)", content):
        hit = large_image_hit(embed, by_path, by_base, min_bytes)
        if hit:
            hits.append({"embed": embed, **hit})
    return hits


def podcast_from_note_path(root: str, note_path: str) -> str:
    parts = note_path.split("/")
    if len(parts) >= 2 and parts[0] == root:
        return parts[1]
    return ""


def notebook_title_from_note_path(note_path: str) -> str:
    name = Path(note_path).name
    return name[:-3] if name.lower().endswith(".md") else name


def is_skip_infographic(podcast: str) -> bool:
    route = core.PODCAST_ROUTING.get(podcast) or {}
    return bool(route.get("skip_infographic"))


def notebook_id_map() -> dict[str, str]:
    notebooks = core.list_nlm_notebooks()
    if notebooks is None:
        raise RuntimeError("failed to list NotebookLM notebooks")
    result: dict[str, str] = {}
    for item in notebooks:
        title = (item.get("title") or "").strip()
        notebook_id = (item.get("id") or "").strip()
        if title and notebook_id and title not in result:
            result[title] = notebook_id
    return result


def completed_infographic_artifacts(notebook_id: str) -> list[dict[str, Any]]:
    items = core._parse_studio_status_items(notebook_id)
    artifacts = []
    for item in items:
        artifact_id = core._artifact_id(item)
        if not artifact_id:
            continue
        if core._artifact_kind(item) != "infographic":
            continue
        if core._artifact_status(item) not in COMPLETED:
            continue
        artifacts.append(item)
    return artifacts


def current_note_id(note_path: str) -> int | None:
    con = sqlite3.connect(core.FAST_NOTE_NOTE_DB)
    try:
        row = con.execute(
            "select id from note where vault_id=1 and path=? and rename=0 order by id desc limit 1",
            (note_path,),
        ).fetchone()
    finally:
        con.close()
    return int(row[0]) if row else None


def artifact_filename(notebook_id: str, index: int, total: int) -> str:
    suffix = f"-{index}" if total > 1 else ""
    return f"NotebookLM infographic {notebook_id[:8]}{suffix}.png"


def attach_artifacts(note_path: str, notebook_id: str, artifacts: list[dict[str, Any]], timeout: int, poll: int) -> list[dict[str, Any]]:
    if not artifacts:
        return []

    note_id = current_note_id(note_path)
    if not note_id:
        return []

    folder_path, basename = note_path.rsplit("/", 1)
    note_title = basename[:-3] if basename.lower().endswith(".md") else basename
    current = read_note_content(note_id)
    attached: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="core-infographic-backfill-") as tmpdir:
        total = len(artifacts)
        for idx, artifact in enumerate(artifacts, start=1):
            artifact_id = core._artifact_id(artifact)
            filename = artifact_filename(notebook_id, idx, total)
            if f"![[{filename}]]" in current:
                attached.append({"artifact_id": artifact_id, "filename": filename, "skipped": "embed_exists"})
                continue
            out = str(Path(tmpdir) / filename)
            ok = core.poll_download_infographic(
                notebook_id,
                out,
                artifact_id=artifact_id,
                timeout_seconds=timeout,
                poll_seconds=poll,
            )
            if not ok:
                attached.append({"artifact_id": artifact_id, "filename": filename, "error": "download_timeout"})
                continue
            attachment = core.save_fast_note_attachment(filename, out)
            current = core.insert_infographic_embed_into_markdown(current, filename)
            attached.append(
                {
                    "artifact_id": artifact_id,
                    "filename": filename,
                    "file_id": attachment.get("file_id"),
                    "file_path": attachment.get("file_path"),
                    "size": os.path.getsize(out),
                }
            )

    if any("file_id" in item for item in attached):
        core.upsert_fast_note_markdown(folder_path, note_title, current)
    return attached


def build_candidates(args: argparse.Namespace, by_path: dict[str, tuple[int, int]], by_base: dict[str, tuple[int, int, str]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    notebooks = notebook_id_map()
    candidates: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    nonmatches: list[dict[str, Any]] = []

    for note in latest_podcast_notes(args.root):
        note_id = int(note["note_id"])
        note_path = str(note["note_path"])
        hits = note_large_image_hits(note_id, by_path, by_base, args.min_bytes)
        if hits and not args.include_existing:
            continue

        podcast = podcast_from_note_path(args.root, note_path)
        title = notebook_title_from_note_path(note_path)
        notebook_id = notebooks.get(title)
        item = {
            "note_id": note_id,
            "note_path": note_path,
            "podcast": podcast,
            "notebook_title": title,
            "notebook_id": notebook_id,
            "existing_large_images": hits,
            "skip_infographic": is_skip_infographic(podcast),
        }

        if args.note_path and args.note_path not in note_path:
            continue
        if item["skip_infographic"]:
            skipped.append(item)
        elif not notebook_id:
            nonmatches.append(item)
        else:
            candidates.append(item)

    return candidates, skipped, nonmatches


def process_candidate(args: argparse.Namespace, run_log: Path | None, candidate: dict[str, Any], idx: int, total: int) -> str:
    note_path = candidate["note_path"]
    notebook_id = candidate["notebook_id"]
    try:
        artifacts = completed_infographic_artifacts(notebook_id)
    except RuntimeError as exc:
        log_event(run_log, "status_error", idx=idx, total=total, error=str(exc), **candidate)
        return "error"

    if artifacts:
        log_event(run_log, "completed_artifacts", idx=idx, total=total, artifact_count=len(artifacts), **candidate)
        if not args.apply:
            return "would_attach_existing"
        attached = attach_artifacts(note_path, notebook_id, artifacts, args.download_timeout, args.poll_seconds)
        log_event(run_log, "attached_existing", idx=idx, total=total, attached=attached, **candidate)
        return "attached_existing" if attached else "error"

    log_event(run_log, "no_completed_artifact", idx=idx, total=total, **candidate)
    if args.no_trigger:
        return "missing"
    if not args.apply:
        return "would_trigger"

    trigger_state = core.trigger_default_infographics(
        notebook_id,
        fast_note={"note_id": candidate["note_id"], "note_path": note_path},
    )
    log_event(
        run_log,
        "triggered",
        idx=idx,
        total=total,
        ok=bool(trigger_state.get("ok")),
        triggered=bool(trigger_state.get("triggered")),
        before_ids=sorted(trigger_state.get("before_ids") or []),
        **candidate,
    )
    if not trigger_state.get("ok"):
        return "trigger_error"

    artifact = core.wait_for_new_infographic_artifact(
        notebook_id,
        before_ids=set(trigger_state.get("before_ids") or []),
        timeout_seconds=args.wait_timeout,
        poll_seconds=args.poll_seconds,
    )
    artifacts = [artifact] if artifact else []
    if not artifacts:
        latest = core.latest_completed_infographic_artifact(
            notebook_id,
            exclude_ids=set(trigger_state.get("before_ids") or []),
        )
        artifacts = [latest] if latest else []
    if not artifacts:
        log_event(run_log, "generated_timeout", idx=idx, total=total, **candidate)
        return "generated_timeout"

    attached = attach_artifacts(note_path, notebook_id, artifacts, args.download_timeout, args.poll_seconds)
    log_event(run_log, "attached_generated", idx=idx, total=total, attached=attached, **candidate)
    return "attached_generated" if attached else "error"


def classify_candidate(run_log: Path | None, candidate: dict[str, Any], idx: int, total: int) -> tuple[str, list[dict[str, Any]]]:
    try:
        artifacts = completed_infographic_artifacts(candidate["notebook_id"])
    except RuntimeError as exc:
        log_event(run_log, "status_error", idx=idx, total=total, error=str(exc), **candidate)
        return "error", []
    if artifacts:
        log_event(run_log, "completed_artifacts", idx=idx, total=total, artifact_count=len(artifacts), **candidate)
        return "ready", artifacts
    log_event(run_log, "no_completed_artifact", idx=idx, total=total, **candidate)
    return "missing", []


def process_candidates_phased(args: argparse.Namespace, run_log: Path | None, selected: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    ready: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    missing: list[dict[str, Any]] = []
    errors = 0

    total = len(selected)
    for idx, candidate in enumerate(selected, start=1):
        status, artifacts = classify_candidate(run_log, candidate, idx, total)
        if status == "ready":
            ready.append((candidate, artifacts))
        elif status == "missing":
            missing.append(candidate)
        else:
            errors += 1

    log_event(
        run_log,
        "classify_done",
        total=total,
        ready=len(ready),
        missing=len(missing),
        errors=errors,
    )
    counts["ready"] = len(ready)
    counts["missing"] = len(missing)
    counts["errors"] = errors

    trigger_states: list[tuple[dict[str, Any], dict[str, Any]]] = []
    if missing and not args.no_trigger:
        for idx, candidate in enumerate(missing, start=1):
            state = core.trigger_default_infographics(
                candidate["notebook_id"],
                fast_note={"note_id": candidate["note_id"], "note_path": candidate["note_path"]},
            )
            log_event(
                run_log,
                "triggered",
                idx=idx,
                total=len(missing),
                ok=bool(state.get("ok")),
                triggered=bool(state.get("triggered")),
                before_ids=sorted(state.get("before_ids") or []),
                **candidate,
            )
            if state.get("ok"):
                trigger_states.append((candidate, state))
                counts["triggered"] = counts.get("triggered", 0) + 1
            else:
                counts["trigger_error"] = counts.get("trigger_error", 0) + 1
    elif missing:
        counts["not_triggered"] = len(missing)

    for idx, (candidate, artifacts) in enumerate(ready, start=1):
        attached = attach_artifacts(
            candidate["note_path"],
            candidate["notebook_id"],
            artifacts,
            args.download_timeout,
            args.poll_seconds,
        )
        log_event(run_log, "attached_existing", idx=idx, total=len(ready), attached=attached, **candidate)
        key = "attached_existing" if attached else "attach_existing_error"
        counts[key] = counts.get(key, 0) + 1

    for idx, (candidate, state) in enumerate(trigger_states, start=1):
        before_ids = set(state.get("before_ids") or [])
        artifact = core.wait_for_new_infographic_artifact(
            candidate["notebook_id"],
            before_ids=before_ids,
            timeout_seconds=args.wait_timeout,
            poll_seconds=args.poll_seconds,
        )
        artifacts = [artifact] if artifact else []
        if not artifacts:
            latest = core.latest_completed_infographic_artifact(candidate["notebook_id"], exclude_ids=before_ids)
            artifacts = [latest] if latest else []
        if not artifacts:
            log_event(run_log, "generated_timeout", idx=idx, total=len(trigger_states), **candidate)
            counts["generated_timeout"] = counts.get("generated_timeout", 0) + 1
            continue
        attached = attach_artifacts(
            candidate["note_path"],
            candidate["notebook_id"],
            artifacts,
            args.download_timeout,
            args.poll_seconds,
        )
        log_event(run_log, "attached_generated", idx=idx, total=len(trigger_states), attached=attached, **candidate)
        key = "attached_generated" if attached else "attach_generated_error"
        counts[key] = counts.get(key, 0) + 1

    return counts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=os.environ.get("NLM_PROFILE", "default"))
    parser.add_argument("--root", default="Podcast")
    parser.add_argument("--apply", action="store_true", help="write Fast Note attachments/Markdown and trigger missing infographics")
    parser.add_argument("--include-existing", action="store_true", help="include notes that already have a large image embed")
    parser.add_argument("--no-trigger", action="store_true", help="do not trigger missing infographics")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--scan-only", action="store_true", help="only scan and optionally write candidate/nonmatch JSON")
    parser.add_argument("--note-path", default="", help="substring filter for a specific Fast Note path")
    parser.add_argument("--min-image-mb", type=float, default=4.0)
    parser.add_argument("--wait-timeout", type=int, default=900)
    parser.add_argument("--download-timeout", type=int, default=600)
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--write-candidates", default="")
    parser.add_argument("--write-nonmatches", default="")
    parser.add_argument("--run-log", default="")
    args = parser.parse_args()
    args.min_bytes = int(args.min_image_mb * 1024 * 1024)
    return args


def main() -> int:
    args = parse_args()
    os.environ["NLM_PROFILE"] = args.profile

    run_log = Path(args.run_log) if args.run_log else Path(
        f"/tmp/core-{args.profile}-infographic-backfill-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"
    )
    if not args.apply and not args.run_log:
        run_log = None

    if not core.ensure_nlm_auth():
        return 1

    by_path, by_base = load_file_index()
    candidates, skipped, nonmatches = build_candidates(args, by_path, by_base)
    selected = candidates[: args.limit] if args.limit else candidates
    summary = {
        "profile": args.profile,
        "root": args.root,
        "apply": args.apply,
        "candidate_total": len(candidates),
        "candidate_selected": len(selected),
        "skip_infographic_count": len(skipped),
        "nonmatch_count": len(nonmatches),
        "min_image_mb": args.min_image_mb,
    }
    log_event(run_log, "scan_summary", **summary)

    if args.write_candidates:
        Path(args.write_candidates).write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.write_nonmatches:
        Path(args.write_nonmatches).write_text(json.dumps(nonmatches, ensure_ascii=False, indent=2), encoding="utf-8")

    counts: dict[str, int] = {}
    if args.scan_only:
        counts["scan_only"] = len(selected)
    elif args.apply:
        counts = process_candidates_phased(args, run_log, selected)
    else:
        for idx, candidate in enumerate(selected, start=1):
            status = process_candidate(args, run_log, candidate, idx, len(selected))
            counts[status] = counts.get(status, 0) + 1

    log_event(run_log, "done", counts=counts)
    if run_log:
        print(f"run_log={run_log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
