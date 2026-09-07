#!/usr/bin/env python3
"""Property-corpus fallback for the phone default harvester.

Before using GitHub's global code search, inspect a few compact public repositories
whose file names contain friendly phone model names and whose contents contain
captured Android properties. This bridges archive names such as `Galaxy-S9` to
real property dumps without requiring a codename database first.

v2 adds a conservative identity/provenance gate. Evidence from another brand,
another marketing model, or an obvious aftermarket/custom ROM is not allowed to
be promoted as factory-default evidence merely because a short model name collides.
"""
from __future__ import annotations

import base64
import sys
import urllib.parse

import identity_guard as ig
import model_runner as mr

pd = mr.pd
_ORIGINAL_CODE_EVIDENCE = mr.github_code_evidence
_ORIGINAL_REPO_EVIDENCE = pd.github_evidence

# Use model_runner's stronger query variants in planet/corpus mode too.
pd.repo_search_queries = mr.better_repo_queries

# Small/medium public corpora with useful captured build/getprop data.
CORPORA = (
    "pytorch/cpuinfo",
    "getActivity/AndroidSystemPropertyCollect",
    "WilliamGrondin/stockBuildProp",
)


def read_repo_file(repo: str, branch: str, path: str, token: str) -> str:
    url = (
        f"https://api.github.com/repos/{repo}/contents/"
        f"{urllib.parse.quote(path, safe='/')}?ref={urllib.parse.quote(branch, safe='')}"
    )
    try:
        data = pd.js(url, token)
        encoded = data.get("content", "")
        if not encoded:
            return ""
        return base64.b64decode(encoded).decode("utf-8", "replace")
    except Exception as exc:
        print(f"  [corpus file warn] {repo}/{path}: {exc}", file=sys.stderr)
        return ""


def _guard_evidence(brand: str, model: str, token: str, evidence, label: str):
    kept, audits = ig.filter_evidence(
        brand,
        model,
        evidence,
        token,
        pd.js,
        byte_loader=lambda url, tok: pd.req(url, tok),
    )
    for audit in audits:
        if audit["status"] != "ACCEPT":
            print(
                f"  [identity {label}] {audit['status']} "
                f"{audit['repo']}/{audit['path']}: {audit['reason']}",
                file=sys.stderr,
            )
    return kept


def validated_repo_evidence(brand: str, model: str, token: str, max_repos: int = 4):
    raw = _ORIGINAL_REPO_EVIDENCE(brand, model, token, max_repos)
    return _guard_evidence(brand, model, token, raw, "repo")


# Planet runner calls pd.github_evidence directly. Gate that first-stage evidence.
pd.github_evidence = validated_repo_evidence


def corpus_evidence(brand: str, model: str, token: str, max_files: int = 12):
    if not token:
        return []

    identity = mr.model_key(model)
    evidence = []
    inspected = 0

    for repo in CORPORA:
        try:
            meta = pd.js(f"https://api.github.com/repos/{repo}", token)
            branch = meta.get("default_branch") or "main"
            tree = pd.js(
                f"https://api.github.com/repos/{repo}/git/trees/"
                f"{urllib.parse.quote(branch, safe='')}?recursive=1",
                token,
            )
        except Exception as exc:
            print(f"  [corpus tree warn] {repo}: {exc}", file=sys.stderr)
            continue

        candidates = []
        for item in tree.get("tree", []):
            if item.get("type") != "blob":
                continue
            path = item.get("path", "")
            if identity not in mr.model_key(path):
                continue
            low = path.lower()
            score = 0
            if any(token in low for token in ("mock", "build.prop", "getprop", "property", "prop")):
                score -= 10
            if low.endswith((".h", ".txt", ".log", ".prop", ".mk")) or low.endswith("build.prop"):
                score -= 5
            score += len(path) / 1000
            candidates.append((score, path))

        candidates.sort()
        if candidates:
            print(
                f"  corpus {repo}: {len(candidates)} model-named candidate(s)",
                file=sys.stderr,
            )

        for _, path in candidates[:max_files]:
            inspected += 1
            body = read_repo_file(repo, branch, path, token)
            if not body:
                continue
            verdict = ig.validate_source(brand, model, body, repo, path)
            if verdict.status != "ACCEPT":
                print(
                    f"  [identity corpus] {verdict.status} {repo}/{path}: {verdict.reason}",
                    file=sys.stderr,
                )
                continue
            evidence.extend(mr.extended_parse_evidence(body, repo, path))
            kinds = {item.kind for item in evidence}
            if kinds >= {"ringtone", "notification", "alarm"}:
                print(f"  corpus evidence: {repo}/{path}", file=sys.stderr)
                return mr.merge_evidence(evidence)
            if inspected >= max_files:
                return mr.merge_evidence(evidence)

    print(f"  corpus files inspected: {inspected}", file=sys.stderr)
    return mr.merge_evidence(evidence)


def smart_code_evidence(brand: str, model: str, token: str, max_files: int = 12):
    corpus = corpus_evidence(brand, model, token, max_files=max_files)
    if {item.kind for item in corpus} >= {"ringtone", "notification", "alarm"}:
        return corpus
    global_raw = _ORIGINAL_CODE_EVIDENCE(brand, model, token, max_files=max_files)
    global_hits = _guard_evidence(brand, model, token, global_raw, "code")
    return mr.merge_evidence(corpus, global_hits)


mr.github_code_evidence = smart_code_evidence


if __name__ == "__main__":
    raise SystemExit(mr.main())
