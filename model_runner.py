#!/usr/bin/env python3
"""Targeted harvester entrypoint.

Adds exact/substring model selection on top of phone_defaults.py while reusing the
archive TLS workaround from archive_runner.py.  This lets Actions test useful
phones directly instead of always starting with the alphabetically oldest model.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

import archive_runner

pd = archive_runner.phone_defaults


def model_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", urllib.parse.unquote(value).lower())


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

    q = model_key(query)
    exact = [item for item in models if model_key(item[0]) == q]
    if exact:
        return exact

    matches = [item for item in models if q in model_key(item[0])]
    if limit:
        matches = matches[:limit]
    return matches


def crawl_selected(brand: str, query: str, limit: int, delay: float):
    models = discover_models(brand)
    chosen = select_models(models, query, limit)
    if not chosen:
        nearby = [name for name, _ in models if any(part in model_key(name) for part in re.findall(r"[a-z0-9]+", query.lower()))][:20]
        print(f"No archive model matched {query!r} under {brand!r}.", file=sys.stderr)
        if nearby:
            print("Nearby candidates:", ", ".join(nearby), file=sys.stderr)
        raise SystemExit(2)

    print("[selected]", ", ".join(name for name, _ in chosen), file=sys.stderr)
    all_audio = []
    for n, (model, model_url) in enumerate(chosen, 1):
        print(f"[model {n}/{len(chosen)}] {brand}/{model}", file=sys.stderr)
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
    # Preserve order while removing duplicates.
    return list(dict.fromkeys(queries))


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
        print("MODEL SELECTOR SELF-TEST PASSED")
        return 0

    pd.repo_search_queries = better_repo_queries
    token = os.getenv("GITHUB_TOKEN", "")
    audios = crawl_selected(args.brand, args.model, args.limit_models, args.delay)
    models = sorted({audio.model for audio in audios})
    results = []
    evidence_map = {}
    out = Path(args.out_dir)

    for i, model in enumerate(models, 1):
        print(f"\n=== [{i}/{len(models)}] {args.brand}/{model} ===")
        evidence = pd.github_evidence(args.brand, model, token, args.max_repos)
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
