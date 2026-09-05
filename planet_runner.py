#!/usr/bin/env python3
"""Resumable, archive-friendly whole-catalog phone default sound harvester.

The planet workflow deliberately separates *resolution* from *audio download*:
model jobs discover/resolve factory defaults first, then a single-lane download
matrix fetches each unique archive URL once. Finalization checksum-deduplicates
within each semantic category and preserves the original sound filename whenever
possible.

Retry runs consume only the previous metadata state. Successfully completed
sound tasks are inherited by checksum/path metadata and are not downloaded from
André's archive again.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.parse
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import archive_catalog
import archive_runner
import model_runner as mr
import corpus_runner as cr

pd = mr.pd

SCHEMA_VERSION = 1
KINDS = ("ringtone", "notification", "alarm")
CATEGORY_DIR = {
    "ringtone": "ringtones",
    "notification": "notifications",
    "alarm": "alarms",
}
RETRYABLE_PREFIXES = ("FAILED_", "DEFERRED_")
SPECIAL_RE = re.compile(
    r"(?:^|[-_ .])(edition|limited|special|collab|collaboration)(?:$|[-_ .])",
    re.I,
)

_ORIGINAL_JS = pd.js
_JSON_CACHE: dict[tuple[str, bool], object] = {}


def _cacheable_json_url(url: str) -> bool:
    if not url.startswith("https://api.github.com/repos/"):
        return False
    for repo in cr.CORPORA:
        marker = f"https://api.github.com/repos/{repo}"
        if url == marker or url.startswith(marker + "/git/trees/"):
            return True
    return False


def cached_js(url: str, token: str = ""):
    if not _cacheable_json_url(url):
        return _ORIGINAL_JS(url, token)
    key = (url, bool(token))
    if key not in _JSON_CACHE:
        _JSON_CACHE[key] = _ORIGINAL_JS(url, token)
    return _JSON_CACHE[key]


pd.js = cached_js


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def model_id(brand: str, model: str) -> str:
    return hashlib.sha256(f"{brand}\0{model}".encode("utf-8")).hexdigest()[:20]


def task_id(kind: str, url: str) -> str:
    """Semantic sound task id. Category remains part of dedupe semantics."""
    return hashlib.sha256(f"{kind}\0{url}".encode("utf-8")).hexdigest()[:24]


def fetch_id(url: str) -> str:
    """Physical fetch id. The same archive URL is downloaded only once per run."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]


def is_special_model(model: str) -> bool:
    return bool(SPECIAL_RE.search(model.replace("+", " ")))


def strict_special_evidence(model: str, evidence):
    if not is_special_model(model):
        return list(evidence)
    identity = mr.model_key(model)
    return [
        item
        for item in evidence
        if identity and identity in mr.model_key(f"{item.repo} {item.path}")
    ]


def classify_exception(exc: Exception, stage: str) -> str:
    text = f"{type(exc).__name__}: {exc}".lower()
    if "429" in text or "rate limit" in text:
        return f"FAILED_{stage.upper()}_RATE_LIMIT"
    if "timed out" in text or "timeout" in text:
        return f"FAILED_{stage.upper()}_TIMEOUT"
    if "403" in text:
        return f"FAILED_{stage.upper()}_FORBIDDEN"
    if "404" in text:
        return f"FAILED_{stage.upper()}_NOT_FOUND"
    return f"FAILED_{stage.upper()}"


def plan_from_rows(rows: list[dict], resolve_shards: int) -> list[dict]:
    total = len(rows)
    planned = []
    for index, raw in enumerate(rows):
        row = dict(raw)
        row["catalog_index"] = index
        row["id"] = model_id(row["brand"], row["model"])
        row["special_model"] = is_special_model(row["model"])
        row["resolve_shard"] = min((index * resolve_shards) // max(total, 1), resolve_shards - 1)
        planned.append(row)
    return planned


def command_make_plan(args) -> int:
    out = Path(args.out)
    catalog_dir = out / "catalog"
    brands, rows, failures = archive_catalog.crawl(delay=args.catalog_delay)
    archive_catalog.write_outputs(catalog_dir, brands, rows, failures)
    if failures:
        print(f"catalog had {len(failures)} failed brand pages; refusing incomplete planet plan", file=sys.stderr)
        return 2
    planned = plan_from_rows(rows, args.resolve_shards)
    write_json(out / "models.json", planned)
    write_json(out / "plan.json", {
        "schema": SCHEMA_VERSION,
        "mode": "full",
        "created_at": now_iso(),
        "resolve_shards": args.resolve_shards,
        "model_count": len(planned),
        "models": planned,
    })
    print(f"PLAN COMPLETE: models={len(planned)} resolve_shards={args.resolve_shards}")
    return 0


def _resolve_state_path(root: Path) -> Path:
    for candidate in (root / "state.json", root / "state" / "state.json"):
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"state.json not found under {root}")


def load_state_file(path: str | Path) -> dict:
    p = Path(path)
    if p.is_dir():
        p = _resolve_state_path(p)
    data = read_json(p)
    if not isinstance(data, dict):
        raise RuntimeError(f"state not found or invalid: {path}")
    return data


def command_retry_plan(args) -> int:
    previous = load_state_file(args.previous_state)
    retry_models = []
    for key, entry in previous.get("models", {}).items():
        status = entry.get("status", "")
        result = entry.get("result") or {}
        retry = status.startswith(RETRYABLE_PREFIXES)
        if args.retry_unresolved and result.get("confidence") in {"UNRESOLVED", "EVIDENCE_ONLY"}:
            retry = True
        if not retry:
            continue
        row = dict(entry.get("model") or {})
        if not row:
            row = {
                "brand": result.get("brand", ""),
                "model": result.get("model", ""),
                "archive_url": entry.get("archive_url", ""),
            }
        row.setdefault("id", key)
        row["special_model"] = bool(row.get("special_model") or is_special_model(row.get("model", "")))
        retry_models.append(row)

    retry_models.sort(key=lambda r: (r.get("brand", "").lower(), r.get("model", "").lower()))
    planned = plan_from_rows(retry_models, args.resolve_shards)
    out = Path(args.out)
    write_json(out / "models.json", planned)
    write_json(out / "plan.json", {
        "schema": SCHEMA_VERSION,
        "mode": "retry",
        "created_at": now_iso(),
        "source_run_id": previous.get("current_run_id") or previous.get("source_run_id"),
        "resolve_shards": args.resolve_shards,
        "model_count": len(planned),
        "models": planned,
    })
    source = Path(args.previous_state) if Path(args.previous_state).is_file() else _resolve_state_path(Path(args.previous_state))
    shutil.copy2(source, out / "previous_state.json")
    print(f"RETRY PLAN COMPLETE: models={len(planned)} retry_unresolved={args.retry_unresolved}")
    return 0


def crawl_model_audio(row: dict, page_delay: float) -> list:
    brand = row["brand"]
    model = row["model"]
    model_url = row.get("archive_url", "")
    if not model_url:
        root = pd.ARCHIVE_ROOT.rstrip("/") + "/"
        brand_url = urllib.parse.urljoin(root, urllib.parse.quote(brand, safe="") + "/")
        model_url = urllib.parse.urljoin(brand_url, urllib.parse.quote(model, safe="") + "/")

    todo = [(model_url, 0)]
    seen = set()
    model_path = urllib.parse.urlparse(model_url).path
    all_audio = []
    while todo:
        url, depth = todo.pop()
        if url in seen or depth > 6:
            continue
        seen.add(url)
        page = pd.text(url)
        for child in pd.children(url, page):
            child_path = urllib.parse.urlparse(child).path
            if not child_path.startswith(model_path):
                continue
            rel = urllib.parse.unquote(child_path[len(model_path):].lstrip("/"))
            ext = Path(urllib.parse.unquote(child_path)).suffix.lower()
            if ext in pd.AUDIO_EXTS:
                all_audio.append(pd.Audio(
                    brand,
                    model,
                    pd.category(rel),
                    urllib.parse.unquote(Path(child_path).name),
                    child,
                    rel,
                ))
            elif child_path.endswith("/"):
                todo.append((child, depth + 1))
        if page_delay:
            time.sleep(page_delay)
    return all_audio


def resolve_one(row: dict, token: str, max_repos: int, page_delay: float) -> tuple[dict, list[dict]]:
    brand, model = row["brand"], row["model"]
    audios = crawl_model_audio(row, page_delay)
    evidence = pd.github_evidence(brand, model, token, max_repos)
    if {item.kind for item in evidence} < set(KINDS):
        evidence = mr.merge_evidence(evidence, mr.github_code_evidence(brand, model, token))
    if is_special_model(model):
        evidence = strict_special_evidence(model, evidence)
    result = pd.resolve(brand, model, audios, evidence)
    payload = asdict(result)
    payload["special_model"] = is_special_model(model)
    payload["strict_special_identity"] = is_special_model(model)
    payload["archive_url"] = row.get("archive_url", "")
    return payload, [asdict(item) for item in evidence]


def persist_resolve_shard(out: Path, rows: list[dict], failures: list[dict], shard: int) -> None:
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "results.json", rows)
    write_json(out / "failures.json", failures)
    write_json(out / "summary.json", {
        "shard": shard,
        "resolved_rows": len(rows),
        "failures": len(failures),
        "updated_at": now_iso(),
    })


def command_resolve_shard(args) -> int:
    plan = read_json(Path(args.plan))
    if not plan:
        raise RuntimeError("plan missing")
    shard = args.shard_index
    models = [row for row in plan.get("models", []) if int(row.get("resolve_shard", -1)) == shard]
    out = Path(args.out) / f"resolve-{shard:02d}"
    results, failures = [], []
    evidence_dir = out / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    token = os.getenv("GITHUB_TOKEN", "")
    started = time.monotonic()
    budget = max(1, args.max_runtime_minutes) * 60

    print(f"[resolve shard {shard}] models={len(models)}")
    for index, row in enumerate(models):
        if time.monotonic() - started > budget - 180:
            for deferred in models[index:]:
                failures.append({
                    "stage": "resolve",
                    "status": "DEFERRED_RESOLVE_TIME_BUDGET",
                    "retryable": True,
                    "brand": deferred["brand"],
                    "model": deferred["model"],
                    "model_id": deferred.get("id") or model_id(deferred["brand"], deferred["model"]),
                    "archive_url": deferred.get("archive_url", ""),
                    "error": "shard stopped before GitHub Actions timeout budget",
                })
            break

        brand, model = row["brand"], row["model"]
        print(f"[{index + 1}/{len(models)}] {brand}/{model}")
        try:
            result, evidence = resolve_one(row, token, args.max_repos, args.archive_page_delay)
            status = f"RESOLVED_{result.get('confidence', 'UNRESOLVED')}"
            results.append({
                "model_id": row.get("id") or model_id(brand, model),
                "status": status,
                "model": row,
                "result": result,
            })
            write_json(evidence_dir / f"{row.get('id') or model_id(brand, model)}.json", evidence)
            print(
                f"  {result.get('confidence')}: "
                f"ringtone={result.get('ringtone') or '?'} "
                f"notification={result.get('notification') or '?'} "
                f"alarm={result.get('alarm') or '?'}"
            )
        except Exception as exc:
            status = classify_exception(exc, "resolve")
            failures.append({
                "stage": "resolve",
                "status": status,
                "retryable": True,
                "brand": brand,
                "model": model,
                "model_id": row.get("id") or model_id(brand, model),
                "archive_url": row.get("archive_url", ""),
                "error": f"{type(exc).__name__}: {exc}",
            })
            print(f"  [failure] {status}: {exc}", file=sys.stderr)
        persist_resolve_shard(out, results, failures, shard)
        if args.model_delay:
            time.sleep(args.model_delay)

    persist_resolve_shard(out, results, failures, shard)
    print(f"RESOLVE SHARD COMPLETE: shard={shard} results={len(results)} failures={len(failures)}")
    return 0


def scan_resolve_outputs(root: Path) -> tuple[list[dict], list[dict]]:
    results, failures = [], []
    for path in root.rglob("results.json"):
        if path.parent.name.startswith("resolve-"):
            results.extend(read_json(path, []) or [])
    for path in root.rglob("failures.json"):
        if path.parent.name.startswith("resolve-"):
            failures.extend(read_json(path, []) or [])
    return results, failures


def result_task_rows(models: dict[str, dict]) -> dict[str, dict]:
    tasks: dict[str, dict] = {}
    for mid, entry in models.items():
        result = entry.get("result") or {}
        for kind in KINDS:
            url = result.get(f"{kind}_url", "")
            original = result.get(kind, "")
            if not url or not original:
                continue
            tid = task_id(kind, url)
            task = tasks.setdefault(tid, {
                "task_id": tid,
                "kind": kind,
                "category_dir": CATEGORY_DIR[kind],
                "url": url,
                "original_name": original,
                "models": [],
            })
            ref = {"model_id": mid, "brand": result.get("brand", ""), "model": result.get("model", "")}
            if ref not in task["models"]:
                task["models"].append(ref)
    return tasks


def build_fetch_plan(sound_tasks: dict[str, dict], completed: dict[str, dict]) -> list[dict]:
    fetches: dict[str, dict] = {}
    for tid, task in sound_tasks.items():
        if tid in completed:
            continue
        fid = fetch_id(task["url"])
        fetch = fetches.setdefault(fid, {"fetch_id": fid, "url": task["url"], "uses": []})
        fetch["uses"].append(task)
    return list(fetches.values())


def command_merge_resolve(args) -> int:
    plan = read_json(Path(args.plan)) or {}
    current_results, current_failures = scan_resolve_outputs(Path(args.resolved_root))
    previous = load_state_file(args.previous_state) if args.previous_state else {}
    models = dict(previous.get("models", {}))

    for row in current_results:
        models[row["model_id"]] = row
    for failure in current_failures:
        mid = failure["model_id"]
        old = models.get(mid, {})
        model_row = old.get("model") or {
            "brand": failure.get("brand", ""),
            "model": failure.get("model", ""),
            "archive_url": failure.get("archive_url", ""),
            "id": mid,
            "special_model": is_special_model(failure.get("model", "")),
        }
        models[mid] = {
            "model_id": mid,
            "status": failure["status"],
            "model": model_row,
            "result": old.get("result"),
            "failure": failure,
        }

    if plan.get("mode") == "full":
        for row in plan.get("models", []):
            mid = row.get("id") or model_id(row["brand"], row["model"])
            if mid not in models:
                failure = {
                    "stage": "resolve",
                    "status": "DEFERRED_RESOLVE_MISSING_SHARD_OUTPUT",
                    "retryable": True,
                    "brand": row["brand"],
                    "model": row["model"],
                    "model_id": mid,
                    "archive_url": row.get("archive_url", ""),
                    "error": "no resolve output was found for this planned model",
                }
                models[mid] = {
                    "model_id": mid,
                    "status": failure["status"],
                    "model": row,
                    "result": None,
                    "failure": failure,
                }

    all_tasks = result_task_rows(models)
    completed = dict(previous.get("completed_downloads", {}))
    pending = build_fetch_plan(all_tasks, completed)
    pending.sort(key=lambda t: t["url"])
    for index, task in enumerate(pending):
        task["download_shard"] = min(
            (index * args.download_shards) // max(len(pending), 1),
            args.download_shards - 1,
        )

    lineage = list(previous.get("lineage_runs", []))
    source_run = previous.get("current_run_id")
    if source_run and str(source_run) not in {str(x) for x in lineage}:
        lineage.append(str(source_run))

    state = {
        "schema": SCHEMA_VERSION,
        "mode": plan.get("mode", "full"),
        "source_run_id": plan.get("source_run_id"),
        "current_run_id": str(args.current_run_id),
        "created_at": now_iso(),
        "lineage_runs": lineage,
        "models": models,
        "completed_downloads": completed,
        "sound_task_count": len(all_tasks),
        "pending_fetch_count": len(pending),
    }
    out = Path(args.out)
    write_json(out / "pre_state.json", state)
    write_json(out / "download_plan.json", pending)
    write_json(out / "resolve_failures.json", [
        entry.get("failure")
        for entry in models.values()
        if entry.get("status", "").startswith(RETRYABLE_PREFIXES) and entry.get("failure")
    ])
    print(f"MERGE RESOLVE COMPLETE: models={len(models)} pending_fetches={len(pending)} inherited_sound_tasks={len(completed)}")
    return 0


def persist_download_shard(out: Path, successes: list[dict], failures: list[dict], shard: int) -> None:
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "download_manifest.json", successes)
    write_json(out / "download_failures.json", failures)
    write_json(out / "summary.json", {
        "shard": shard,
        "successes": len(successes),
        "failures": len(failures),
        "updated_at": now_iso(),
    })


def command_download_shard(args) -> int:
    tasks = read_json(Path(args.plan), []) or []
    shard_tasks = [task for task in tasks if int(task.get("download_shard", -1)) == args.shard_index]
    out = Path(args.out) / f"download-{args.shard_index:02d}"
    raw_dir = out / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    successes, failures = [], []
    started = time.monotonic()
    budget = max(1, args.max_runtime_minutes) * 60

    print(f"[download shard {args.shard_index}] unique fetches={len(shard_tasks)}")
    for index, task in enumerate(shard_tasks):
        if time.monotonic() - started > budget - 180:
            for deferred in shard_tasks[index:]:
                failures.append({
                    "stage": "download",
                    "status": "DEFERRED_DOWNLOAD_TIME_BUDGET",
                    "retryable": True,
                    "task": deferred,
                    "error": "shard stopped before GitHub Actions timeout budget",
                })
            break
        fid = task["fetch_id"]
        use_names = ", ".join(f"{u['kind']}:{u['original_name']}" for u in task.get("uses", []))
        print(f"[{index + 1}/{len(shard_tasks)}] {use_names}")
        try:
            blob = pd.req(task["url"], timeout=args.request_timeout)
            raw_path = raw_dir / fid
            raw_path.write_bytes(blob)
            successes.append({
                **task,
                "sha256": hashlib.sha256(blob).hexdigest(),
                "size": len(blob),
                "raw_path": str(raw_path.relative_to(out)),
            })
        except Exception as exc:
            failures.append({
                "stage": "download",
                "status": classify_exception(exc, "download"),
                "retryable": True,
                "task": task,
                "error": f"{type(exc).__name__}: {exc}",
            })
            print(f"  [download failure] {exc}", file=sys.stderr)
        persist_download_shard(out, successes, failures, args.shard_index)
        if args.audio_delay:
            time.sleep(args.audio_delay)

    persist_download_shard(out, successes, failures, args.shard_index)
    print(f"DOWNLOAD SHARD COMPLETE: shard={args.shard_index} success={len(successes)} failures={len(failures)}")
    return 0


def scan_download_outputs(root: Path) -> tuple[list[tuple[dict, Path]], list[dict]]:
    successes, failures = [], []
    for manifest in root.rglob("download_manifest.json"):
        shard_root = manifest.parent
        for item in read_json(manifest, []) or []:
            successes.append((item, shard_root / item["raw_path"]))
    for failed in root.rglob("download_failures.json"):
        failures.extend(read_json(failed, []) or [])
    return successes, failures


def safe_original_name(name: str, task: dict) -> str:
    name = name.replace("\x00", "")
    if name in {"", ".", ".."} or "/" in name or "\\" in name:
        ext = Path(urllib.parse.urlparse(task.get("url", "")).path).suffix or ".bin"
        return f"unnamed-{task['task_id']}{ext}"
    return name


def choose_stored_name(category: str, original: str, sha: str, name_index: dict[tuple[str, str], str]) -> str:
    existing_sha = name_index.get((category, original))
    if existing_sha is None or existing_sha == sha:
        return original
    p = Path(original)
    candidate = f"{p.stem}__{sha[:12]}{p.suffix}"
    counter = 2
    while (category, candidate) in name_index and name_index[(category, candidate)] != sha:
        candidate = f"{p.stem}__{sha[:12]}_{counter}{p.suffix}"
        counter += 1
    return candidate


def command_finalize(args) -> int:
    merged = Path(args.merged)
    pre = read_json(merged / "pre_state.json") or {}
    models = pre.get("models", {})
    completed: dict[str, dict] = dict(pre.get("completed_downloads", {}))
    current, download_failures = scan_download_outputs(Path(args.downloads_root))

    hash_index = {}
    name_index = {}
    for entry in completed.values():
        cat = entry.get("category_dir") or CATEGORY_DIR.get(entry.get("kind", ""), "")
        sha = entry.get("sha256", "")
        stored = entry.get("stored_file", "")
        if cat and sha:
            hash_index[(cat, sha)] = entry
        if cat and stored:
            name_index[(cat, Path(stored).name)] = sha

    final = Path(args.out)
    sounds_root = final / "sounds"
    for dirname in CATEGORY_DIR.values():
        (sounds_root / dirname).mkdir(parents=True, exist_ok=True)

    current_run_id = str(args.current_run_id)
    current_artifact = args.current_sound_artifact
    for item, raw_path in current:
        sha = item["sha256"]
        for use in item.get("uses", []):
            tid = use["task_id"]
            category = use["category_dir"]
            duplicate = hash_index.get((category, sha))
            if duplicate:
                completed[tid] = {
                    **use,
                    "fetch_id": item.get("fetch_id", ""),
                    "sha256": sha,
                    "size": item.get("size", 0),
                    "stored_file": duplicate["stored_file"],
                    "source_run_id": duplicate.get("source_run_id"),
                    "source_artifact": duplicate.get("source_artifact"),
                    "dedup_of_task": duplicate.get("task_id"),
                    "deduped": True,
                }
                continue

            original = safe_original_name(use.get("original_name", ""), {**use, "task_id": tid})
            stored_name = choose_stored_name(category, original, sha, name_index)
            dest = sounds_root / category / stored_name
            shutil.copy2(raw_path, dest)
            entry = {
                **use,
                "fetch_id": item.get("fetch_id", ""),
                "sha256": sha,
                "size": item.get("size", 0),
                "stored_file": f"sounds/{category}/{stored_name}",
                "source_run_id": current_run_id,
                "source_artifact": current_artifact,
                "deduped": False,
            }
            completed[tid] = entry
            hash_index[(category, sha)] = entry
            name_index[(category, stored_name)] = sha

    filtered_download_failures = []
    for failure in download_failures:
        fetch = failure.get("task") or {}
        remaining = [use for use in fetch.get("uses", []) if use.get("task_id") not in completed]
        if remaining:
            copy = dict(failure)
            copy["task"] = {**fetch, "uses": remaining}
            filtered_download_failures.append(copy)
    download_failures = filtered_download_failures

    result_rows = []
    for mid, entry in sorted(models.items(), key=lambda kv: (
        (kv[1].get("model") or {}).get("brand", "").lower(),
        (kv[1].get("model") or {}).get("model", "").lower(),
    )):
        result = dict(entry.get("result") or {})
        if not result:
            continue
        for kind in KINDS:
            url = result.get(f"{kind}_url", "")
            if not url:
                continue
            tid = task_id(kind, url)
            stored = completed.get(tid)
            if stored:
                result[f"{kind}_stored_file"] = stored.get("stored_file", "")
                result[f"{kind}_sha256"] = stored.get("sha256", "")
                result[f"{kind}_source_run_id"] = stored.get("source_run_id", "")
                result[f"{kind}_source_artifact"] = stored.get("source_artifact", "")
            else:
                result[f"{kind}_download_pending"] = True
        result_rows.append(result)

    resolve_failures = [
        entry["failure"]
        for entry in models.values()
        if entry.get("status", "").startswith(RETRYABLE_PREFIXES) and entry.get("failure")
    ]
    failure_rows = list(resolve_failures)
    for failure in download_failures:
        fetch = failure.get("task") or {}
        for use in fetch.get("uses", []):
            for ref in use.get("models") or [{}]:
                failure_rows.append({
                    "stage": "download",
                    "status": failure.get("status", "FAILED_DOWNLOAD"),
                    "retryable": True,
                    "brand": ref.get("brand", ""),
                    "model": ref.get("model", ""),
                    "model_id": ref.get("model_id", ""),
                    "kind": use.get("kind", ""),
                    "original_name": use.get("original_name", ""),
                    "url": fetch.get("url", ""),
                    "task_id": use.get("task_id", ""),
                    "fetch_id": fetch.get("fetch_id", ""),
                    "error": failure.get("error", ""),
                })

    unresolved = [r for r in result_rows if r.get("confidence") != "CONFIRMED"]
    confirmed = [r for r in result_rows if r.get("confidence") == "CONFIRMED"]
    lineage = list(pre.get("lineage_runs", []))
    if current_run_id not in {str(x) for x in lineage}:
        lineage.append(current_run_id)

    state = {
        **pre,
        "current_run_id": current_run_id,
        "updated_at": now_iso(),
        "lineage_runs": lineage,
        "models": models,
        "completed_downloads": completed,
        "failures": failure_rows,
        "unresolved_count": len(unresolved),
        "confirmed_count": len(confirmed),
        "result_count": len(result_rows),
        "unique_completed_sound_tasks": len(completed),
    }

    write_json(final / "state" / "state.json", state)
    write_json(final / "results.json", result_rows)
    write_json(final / "confirmed.json", confirmed)
    write_json(final / "unresolved.json", unresolved)
    write_json(final / "failures.json", failure_rows)
    write_json(final / "sound_manifest.json", sorted(
        completed.values(),
        key=lambda x: (x.get("category_dir", ""), x.get("stored_file", ""), x.get("task_id", "")),
    ))
    write_json(final / "lineage.json", {"runs": lineage})
    write_csv(final / "results.csv", result_rows)
    write_csv(final / "confirmed.csv", confirmed)
    write_csv(final / "unresolved.csv", unresolved)
    write_csv(final / "failures.csv", failure_rows)

    if pre.get("mode") == "retry":
        (final / "RETRY_DELTA_README.txt").write_text(
            "This retry artifact contains consolidated metadata plus only NEW unique audio files downloaded during this retry run.\n"
            "Previously successful audio is referenced by source_run_id/source_artifact in sound_manifest.json and was not downloaded from the archive again.\n",
            encoding="utf-8",
        )

    summary = (
        f"# Planet harvest summary\n\n"
        f"- Models with result rows: **{len(result_rows)}**\n"
        f"- Confirmed: **{len(confirmed)}**\n"
        f"- Partial/evidence/unresolved: **{len(unresolved)}**\n"
        f"- Retryable failures: **{len(failure_rows)}**\n"
        f"- Completed unique sound tasks across lineage: **{len(completed)}**\n"
        f"- Current run newly stored files: **{sum(1 for e in completed.values() if str(e.get('source_run_id')) == current_run_id and not e.get('deduped'))}**\n"
        f"- Lineage runs: `{', '.join(lineage)}`\n"
    )
    (final / "summary.md").write_text(summary, encoding="utf-8")
    print(summary)
    return 0


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        if not fields:
            handle.write("")
            return
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            flat = {}
            for key, value in row.items():
                flat[key] = json.dumps(value, ensure_ascii=False, separators=(",", ":")) if isinstance(value, (dict, list)) else value
            writer.writerow(flat)


def self_test() -> None:
    assert is_special_model("11-Genshin-Impact-Edition")
    assert is_special_model("Galaxy-Note-10+-Star-Wars-Edition")
    assert not is_special_model("Galaxy-S9")
    fake = [
        pd.Evidence("ringtone", "Base.ogg", "vendor/oneplus11", "device/11/build.prop", "x"),
        pd.Evidence("ringtone", "Special.ogg", "dump", "11-genshin-impact-edition/build.prop", "y"),
    ]
    strict = strict_special_evidence("11-Genshin-Impact-Edition", fake)
    assert len(strict) == 1 and strict[0].value == "Special.ogg"

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        merged = root / "merged"
        downloads = root / "downloads"
        out = root / "final"
        rows = []
        models = {}
        for brand, model, url in (("A", "M1", "https://example/a"), ("B", "M2", "https://example/b"), ("C", "M3", "https://example/c")):
            result = {
                "brand": brand,
                "model": model,
                "ringtone": "Tone.ogg",
                "ringtone_url": url,
                "notification": "",
                "notification_url": "",
                "alarm": "",
                "alarm_url": "",
                "confidence": "PARTIAL",
            }
            mid = model_id(brand, model)
            models[mid] = {"model_id": mid, "status": "RESOLVED_PARTIAL", "model": {"brand": brand, "model": model}, "result": result}
            rows.append((url, result))
        write_json(merged / "pre_state.json", {"mode": "full", "models": models, "completed_downloads": {}, "lineage_runs": []})
        shard = downloads / "download-00"
        raw = shard / "raw"
        raw.mkdir(parents=True)
        blobs = [b"same", b"same", b"different"]
        manifest = []
        for (url, _), blob in zip(rows, blobs):
            tid = task_id("ringtone", url)
            fid = fetch_id(url)
            (raw / fid).write_bytes(blob)
            manifest.append({
                "fetch_id": fid,
                "url": url,
                "uses": [{
                    "task_id": tid,
                    "kind": "ringtone",
                    "category_dir": "ringtones",
                    "url": url,
                    "original_name": "Tone.ogg",
                    "models": [],
                }],
                "sha256": hashlib.sha256(blob).hexdigest(),
                "size": len(blob),
                "raw_path": f"raw/{fid}",
            })
        write_json(shard / "download_manifest.json", manifest)
        write_json(shard / "download_failures.json", [])
        command_finalize(argparse.Namespace(
            merged=str(merged),
            downloads_root=str(downloads),
            out=str(out),
            current_run_id="123",
            current_sound_artifact="phone-defaults-planet-123",
        ))
        files = sorted(p.name for p in (out / "sounds" / "ringtones").iterdir())
        assert files[0] == "Tone.ogg"
        assert len(files) == 2
        assert files[1].startswith("Tone__")
        state = read_json(out / "state" / "state.json")
        assert len(state["completed_downloads"]) == 3
        paths = [v["stored_file"] for v in state["completed_downloads"].values()]
        assert paths.count("sounds/ringtones/Tone.ogg") == 2
    print("PLANET RUNNER SELF-TEST PASSED")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Whole-archive resumable phone default sound harvester")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("self-test")

    p = sub.add_parser("make-plan")
    p.add_argument("--out", required=True)
    p.add_argument("--resolve-shards", type=int, default=64)
    p.add_argument("--catalog-delay", type=float, default=0.5)

    p = sub.add_parser("retry-plan")
    p.add_argument("--previous-state", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--resolve-shards", type=int, default=64)
    p.add_argument("--retry-unresolved", action="store_true")

    p = sub.add_parser("resolve-shard")
    p.add_argument("--plan", required=True)
    p.add_argument("--shard-index", type=int, required=True)
    p.add_argument("--max-repos", type=int, default=4)
    p.add_argument("--archive-page-delay", type=float, default=0.8)
    p.add_argument("--model-delay", type=float, default=2.0)
    p.add_argument("--max-runtime-minutes", type=int, default=300)
    p.add_argument("--out", required=True)

    p = sub.add_parser("merge-resolve")
    p.add_argument("--plan", required=True)
    p.add_argument("--resolved-root", required=True)
    p.add_argument("--previous-state")
    p.add_argument("--download-shards", type=int, default=24)
    p.add_argument("--current-run-id", required=True)
    p.add_argument("--out", required=True)

    p = sub.add_parser("download-shard")
    p.add_argument("--plan", required=True)
    p.add_argument("--shard-index", type=int, required=True)
    p.add_argument("--audio-delay", type=float, default=1.5)
    p.add_argument("--request-timeout", type=int, default=60)
    p.add_argument("--max-runtime-minutes", type=int, default=300)
    p.add_argument("--out", required=True)

    p = sub.add_parser("finalize")
    p.add_argument("--merged", required=True)
    p.add_argument("--downloads-root", required=True)
    p.add_argument("--current-run-id", required=True)
    p.add_argument("--current-sound-artifact", required=True)
    p.add_argument("--out", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.cmd == "self-test":
        self_test()
        return 0
    return {
        "make-plan": command_make_plan,
        "retry-plan": command_retry_plan,
        "resolve-shard": command_resolve_shard,
        "merge-resolve": command_merge_resolve,
        "download-shard": command_download_shard,
        "finalize": command_finalize,
    }[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
