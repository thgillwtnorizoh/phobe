#!/usr/bin/env python3
"""Targeted harvester entrypoint.

Adds exact/substring model selection, broader evidence parsing, and a GitHub code
search fallback on top of phone_defaults.py.  The archive TLS workaround comes
from archive_runner.py.
"""
from __future__ import annotations

import argparse
import base64
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

import archive_runner

pd = archive_runner.phone_defaults
_ORIGINAL_PARSE_EVIDENCE = pd.parse_evidence


def model_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", urllib.parse.unquote(value).lower())


def prop_kind(key: str):
    key = key.lower()
    if re.fullmatch(r"ro\.config\.ringtone(?:_1)?|default_ringtone|ringtone_default", key):
        return "ringtone"
    if re.fullmatch(r"ro\.config\.notification_sound(?:_1)?|default_notification(?:_sound)?|notification_sound_default", key):
        return "notification"
    if re.fullmatch(r"ro\.config\.alarm_alert|default_alarm(?:_alert|_sound)?|alarm_alert_default", key):
        return "alarm"
    return None


def extended_parse_evidence(body: str, repo: str, path: str):
    """Parse normal props plus getprop brackets and C-style key/value fixtures."""
    out = list(_ORIGINAL_PARSE_EVIDENCE(body, repo, path))
    lines = body.splitlines()

    # Android `getprop` output: [ro.config.notification_sound]: [Skyline.ogg]
    bracket = re.compile(r"\[([^\]]+)\]\s*:\s*\[([^\]]*)\]")
    for num, raw in enumerate(lines, 1):
        match = bracket.search(raw)
        if not match:
            continue
        kind = prop_kind(match.group(1).strip())
        value = pd.clean(match.group(2))
        if kind and value:
            out.append(pd.Evidence(kind, value, repo, path, f"{num}: {raw.strip()}"))

    # cpuinfo-style fixtures commonly store the property and value on adjacent
    # lines as `.key = "ro.config.ringtone"` / `.value = "..."`.
    key_re = re.compile(r"[\"']((?:ro\.config\.(?:ringtone(?:_1)?|notification_sound(?:_1)?|alarm_alert))|(?:default_(?:ringtone|notification(?:_sound)?|alarm(?:_alert|_sound)?))|(?:ringtone_default|notification_sound_default|alarm_alert_default))[\"']", re.I)
    value_re = re.compile(r"\.value\s*=\s*[\"']([^\"']+)[\"']", re.I)
    for index, raw in enumerate(lines):
        match = key_re.search(raw)
        if not match:
            continue
        kind = prop_kind(match.group(1))
        if not kind:
            continue
        for follow in range(index, min(index + 5, len(lines))):
            value_match = value_re.search(lines[follow])
            if value_match:
                value = pd.clean(value_match.group(1))
                if value:
                    out.append(pd.Evidence(kind, value, repo, path, f"{index + 1}: {raw.strip()} -> {lines[follow].strip()}"))
                break

    deduped = []
    seen = set()
    for evidence in out:
        key = (evidence.kind, evidence.value, evidence.repo, evidence.path)
        if key not in seen:
            seen.add(key)
            deduped.append(evidence)
    return deduped


pd.parse_evidence = extended_parse_evidence


def discover_models(brand: str):
    root = pd.ARCHIVE_ROOT.rstrip("/") + "/"
    brand_url = urllib.parse.urljoin(root, urllib.parse.quote(brand, safe="") + "/")
    print("[crawl index]", brand_url, file=sys.stderr)
    page = pd.text(brand_url)
    brand_path = urllib.parse.urlparse(brand_url).path
    models = []
    for url in pd.children(brand_url, page):
        path = urllib.parse.urlparse(url).path
        rel = path[len(brand_path):].strip("/") if path.startswith(brand_path) else ""
        if path.endswith("/") and rel and "/" not in rel:
            name = urllib.parse.unquote(rel)
            if not name.startswith("!"):
                models.append((name, url))
    return sorted(set(models), key=lambda x: x[0].lower())


def select_models(models, query: str, limit: int):
    if not query.strip():
        return models if not limit else models[:limit]

    query_key = model_key(query)
    exact = [item for item in models if model_key(item[0]) == query_key]
    if exact:
        return exact

    matches = [item for item in models if query_key in model_key(item[0])]
    if limit:
        matches = matches[:limit]
    return matches


def crawl_selected(brand: str, query: str, limit: int, delay: float):
    models = discover_models(brand)
    chosen = select_models(models, query, limit)
    if not chosen:
        query_parts = re.findall(r"[a-z0-9]+", query.lower())
        nearby = [name for name, _ in models if any(part in model_key(name) for part in query_parts)][:20]
        print(f"No archive model matched {query!r} under {brand!r}.", file=sys.stderr)
        if nearby:
            print("Nearby candidates:", ", ".join(nearby), file=sys.stderr)
        raise SystemExit(2)

    print("[selected]", ", ".join(name for name, _ in chosen), file=sys.stderr)
    all_audio = []
    for number, (model, model_url) in enumerate(chosen, 1):
        print(f"[model {number}/{len(chosen)}] {brand}/{model}", file=sys.stderr)
        todo = [(model_url, 0)]
        seen = set()
        model_path = urllib.parse.urlparse(model_url).path
        while todo:
            url, depth = todo.pop()
            if url in seen or depth > 6:
                continue
            seen.add(url)
            try:
                page = pd.text(url)
            except Exception as exc:
                print("[warn]", url, exc, file=sys.stderr)
                continue
            for child in pd.children(url, page):
                child_path = urllib.parse.urlparse(child).path
                if not child_path.startswith(model_path):
                    continue
                rel = urllib.parse.unquote(child_path[len(model_path):].lstrip("/"))
                ext = Path(urllib.parse.unquote(child_path)).suffix.lower()
                if ext in pd.AUDIO_EXTS:
                    all_audio.append(
                        pd.Audio(
                            brand,
                            model,
                            pd.category(rel),
                            urllib.parse.unquote(Path(child_path).name),
                            child,
                            rel,
                        )
                    )
                elif child_path.endswith("/"):
                    todo.append((child, depth + 1))
            if delay:
                time.sleep(delay)
    return all_audio


def better_repo_queries(brand: str, model: str):
    spaced = re.sub(r"[-_]+", " ", model).strip()
    compact = re.sub(r"[^A-Za-z0-9]+", "", model)
    queries = [
        f'android dump "{brand}" "{spaced}"',
        f'vendor "{brand}" "{spaced}"',
        f'firmware "{brand}" "{spaced}"',
        f'{brand} {compact} android dump',
    ]
    return list(dict.fromkeys(queries))


def read_code_search_item(item, token: str):
    """Fetch UTF-8-ish content for one REST code-search result."""
    url = item.get("url", "")
    if not url:
        return ""
    try:
        data = pd.js(url, token)
    except Exception as exc:
        print("  [code fetch warn]", exc, file=sys.stderr)
        return ""
    encoded = data.get("content", "")
    if not encoded:
        return ""
    try:
        return base64.b64decode(encoded).decode("utf-8", "replace")
    except Exception:
        return ""


def github_code_evidence(brand: str, model: str, token: str, max_files: int = 12):
    """Search property-bearing files directly when repository discovery misses."""
    if not token:
        return []

    spaced = re.sub(r"[-_]+", " ", model).strip()
    identity_key = model_key(model)
    queries = [
        f'ro.config.notification_sound "{spaced}"',
        f'ro.config.ringtone "{spaced}"',
        f'ro.config.alarm_alert "{spaced}"',
    ]
    evidence = []
    seen_urls = set()
    inspected = 0

    for query in queries:
        endpoint = "https://api.github.com/search/code?per_page=20&q=" + urllib.parse.quote(query)
        try:
            data = pd.js(endpoint, token)
        except Exception as exc:
            print("  [code search warn]", exc, file=sys.stderr)
            continue

        for item in data.get("items", []):
            item_url = item.get("url", "")
            if not item_url or item_url in seen_urls:
                continue
            seen_urls.add(item_url)
            repo = item.get("repository", {}).get("full_name", "")
            path = item.get("path", "")

            # Require the archive model name to appear in the result path or
            # repository name. This avoids accepting a random phone that merely
            # shares the same Samsung default sound.
            identity_haystack = model_key(repo + " " + path)
            if identity_key not in identity_haystack:
                continue

            inspected += 1
            body = read_code_search_item(item, token)
            if not body:
                continue
            evidence.extend(extended_parse_evidence(body, repo, path))
            if {item.kind for item in evidence} >= {"ringtone", "notification", "alarm"}:
                print(f"  code-search evidence: {repo}/{path}", file=sys.stderr)
                return evidence
            if inspected >= max_files:
                return evidence
        time.sleep(.3)

    print(f"  code-search files inspected: {inspected}", file=sys.stderr)
    return evidence


def merge_evidence(*groups):
    merged = []
    seen = set()
    for group in groups:
        for evidence in group:
            key = (evidence.kind, evidence.value, evidence.repo, evidence.path)
            if key not in seen:
                seen.add(key)
                merged.append(evidence)
    return merged


def main():
    parser = argparse.ArgumentParser(description="Harvest factory-default phone sounds for a targeted archive model.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("self-test")
    harvest = sub.add_parser("harvest")
    harvest.add_argument("--brand", required=True)
    harvest.add_argument("--model", default="", help="Exact or substring archive model name, e.g. Galaxy-S9")
    harvest.add_argument("--limit-models", type=int, default=3)
    harvest.add_argument("--max-repos", type=int, default=4)
    harvest.add_argument("--delay", type=float, default=.2)
    harvest.add_argument("--download", action="store_true")
    harvest.add_argument("--out-dir", default="out")
    args = parser.parse_args()

    if args.cmd == "self-test":
        pd.self_test()
        assert model_key("Galaxy-S9") == model_key("Galaxy S9")
        fixture = '''[ro.config.notification_sound]: [Skyline.ogg]\n{ .key = "ro.config.ringtone",\n  .value = "Over_the_Horizon.ogg" },\n{ .key = "ro.config.alarm_alert",\n  .value = "Morning_Glory.ogg" },'''
        parsed = extended_parse_evidence(fixture, "test/repo", "galaxy-s9-global.h")
        assert {(item.kind, item.value) for item in parsed} == {
            ("ringtone", "Over_the_Horizon.ogg"),
            ("notification", "Skyline.ogg"),
            ("alarm", "Morning_Glory.ogg"),
        }
        print("MODEL SELECTOR + EXTENDED EVIDENCE SELF-TEST PASSED")
        return 0

    pd.repo_search_queries = better_repo_queries
    token = os.getenv("GITHUB_TOKEN", "")
    audios = crawl_selected(args.brand, args.model, args.limit_models, args.delay)
    models = sorted({audio.model for audio in audios})
    results = []
    evidence_map = {}
    out = Path(args.out_dir)

    for index, model in enumerate(models, 1):
        print(f"\n=== [{index}/{len(models)}] {args.brand}/{model} ===")
        repo_evidence = pd.github_evidence(args.brand, model, token, args.max_repos)
        kinds = {item.kind for item in repo_evidence}
        code_evidence = []
        if kinds < {"ringtone", "notification", "alarm"}:
            code_evidence = github_code_evidence(args.brand, model, token)
        evidence = merge_evidence(repo_evidence, code_evidence)
        evidence_map[model] = evidence

        result = pd.resolve(args.brand, model, audios, evidence)
        results.append(result)
        print(
            f"{result.confidence}: ringtone={result.ringtone or '?'} "
            f"notification={result.notification or '?'} alarm={result.alarm or '?'}"
        )
        pd.write_outputs(out, audios, results, evidence_map)
        if args.download and result.confidence in ("CONFIRMED", "PARTIAL"):
            pd.download_result(result, out)

    pd.write_outputs(out, audios, results, evidence_map)
    print(
        f"\nDone: {len(results)} models; "
        f"confirmed={sum(r.confidence == 'CONFIRMED' for r in results)}; output={out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
