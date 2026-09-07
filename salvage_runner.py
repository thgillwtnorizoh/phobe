#!/usr/bin/env python3
"""Offline v2 audit + salvage for a completed planet-harvest artifact.

No archive/network requests are made. The command audits legacy evidence paths,
finds declared-but-unmatched defaults already present in the sound vault, and
auto-salvages only when identity metadata is strong and exactly one checksum
variant exists. The v1 result files are never mutated.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import re
import tempfile
import zipfile

import identity_guard as ig

KINDS = ("ringtone", "notification", "alarm")
DECLARED_RE = {
    kind: re.compile(rf"{kind} declared as (.*?) but no archive filename matched", re.I | re.S)
    for kind in KINDS
}


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        if not fields:
            return
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            flat = {
                key: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                if isinstance(value, (list, dict)) else value
                for key, value in row.items()
            }
            writer.writerow(flat)


def sound_norm(value: str) -> str:
    cleaned = str(value).replace("\\n", "").replace("\x00", "").strip()
    stem = Path(cleaned).stem.lower()
    return re.sub(r"[^a-z0-9]+", "", stem)


def declared_value(result: dict, kind: str) -> str:
    notes = str(result.get("notes", ""))
    for part in notes.split(" | "):
        match = DECLARED_RE[kind].search(part)
        if match:
            return match.group(1).strip().replace("\\n", "").strip()
    return ""


def manifest_index(manifest: list[dict]) -> dict[tuple[str, str], dict[str, list[dict]]]:
    index: dict[tuple[str, str], dict[str, list[dict]]] = {}
    for entry in manifest:
        kind = entry.get("kind", "")
        key = (kind, sound_norm(entry.get("original_name", "")))
        sha = entry.get("sha256", "")
        if kind not in KINDS or not key[1] or not sha:
            continue
        index.setdefault(key, {}).setdefault(sha, []).append(entry)
    return index


def audit_result(result: dict) -> dict:
    if result.get("evidence_repo") or result.get("evidence_paths"):
        verdict = ig.static_source_verdict(
            result.get("brand", ""),
            result.get("model", ""),
            result.get("evidence_repo", ""),
            result.get("evidence_paths", ""),
        )
    else:
        verdict = ig.IdentityVerdict(
            "UNKNOWN", "no evidence source recorded", [], [], [], [], []
        )
    matched = [kind for kind in KINDS if result.get(kind) and result.get(f"{kind}_url")]
    return {
        "brand": result.get("brand", ""),
        "model": result.get("model", ""),
        "v1_confidence": result.get("confidence", ""),
        "legacy_identity_status": verdict.status,
        "legacy_identity_reason": verdict.reason,
        "source_brand_groups": verdict.source_brand_groups,
        "custom_rom_markers": verdict.custom_rom_markers,
        "evidence_repo": result.get("evidence_repo", ""),
        "evidence_paths": result.get("evidence_paths", ""),
        "existing_matched_kinds": matched,
        "existing_assignment_at_risk": bool(matched and verdict.status == "REJECT"),
    }


def run_salvage(source: Path, out: Path) -> dict:
    results = read_json(source / "results.json")
    manifest = read_json(source / "sound_manifest.json")
    index = manifest_index(manifest)

    audits: list[dict] = []
    salvage: list[dict] = []
    ambiguous: list[dict] = []
    revalidate: list[dict] = []
    no_audio_copy: list[dict] = []

    for result in results:
        audit = audit_result(result)
        audits.append(audit)
        for kind in KINDS:
            if result.get(kind) and result.get(f"{kind}_url"):
                continue
            declared = declared_value(result, kind)
            if not declared:
                continue
            variants = index.get((kind, sound_norm(declared)), {})
            base = {
                "brand": result.get("brand", ""),
                "model": result.get("model", ""),
                "kind": kind,
                "declared_name": declared,
                "v1_confidence": result.get("confidence", ""),
                "legacy_identity_status": audit["legacy_identity_status"],
                "legacy_identity_reason": audit["legacy_identity_reason"],
                "evidence_repo": result.get("evidence_repo", ""),
                "evidence_paths": result.get("evidence_paths", ""),
            }
            if not variants:
                no_audio_copy.append(base)
                continue

            variant_rows = []
            for sha, entries in sorted(variants.items()):
                canonical = sorted(
                    entries,
                    key=lambda e: (e.get("stored_file", ""), e.get("url", "")),
                )[0]
                variant_rows.append({
                    "sha256": sha,
                    "stored_file": canonical.get("stored_file", ""),
                    "source_url": canonical.get("url", ""),
                    "source_models": sorted({
                        f"{m.get('brand', '')}/{m.get('model', '')}"
                        for entry in entries
                        for m in entry.get("models", [])
                    }),
                    "manifest_task_ids": sorted({
                        entry.get("task_id", "") for entry in entries if entry.get("task_id")
                    }),
                })

            if audit["legacy_identity_status"] != "ACCEPT":
                revalidate.append({**base, "available_checksum_variants": variant_rows})
            elif len(variant_rows) == 1:
                chosen = variant_rows[0]
                salvage.append({
                    **base,
                    "salvage_confidence": "REUSED_UNIQUE_AUDIO",
                    "sha256": chosen["sha256"],
                    "stored_file": chosen["stored_file"],
                    "source_url": chosen["source_url"],
                    "source_models": chosen["source_models"],
                    "note": "No new bytes downloaded; reuses the only known checksum for this declared filename in the same semantic category.",
                })
            else:
                ambiguous.append({**base, "available_checksum_variants": variant_rows})

    at_risk = [row for row in audits if row["existing_assignment_at_risk"]]
    accepted_audits = [row for row in audits if row["legacy_identity_status"] == "ACCEPT"]
    rejected_audits = [row for row in audits if row["legacy_identity_status"] == "REJECT"]
    unknown_audits = [row for row in audits if row["legacy_identity_status"] == "UNKNOWN"]

    write_json(out / "salvage_assignments.json", salvage)
    write_csv(out / "salvage_assignments.csv", salvage)
    write_json(out / "ambiguous_audio.json", ambiguous)
    write_csv(out / "ambiguous_audio.csv", ambiguous)
    write_json(out / "revalidation_queue.json", revalidate)
    write_csv(out / "revalidation_queue.csv", revalidate)
    write_json(out / "identity_audit.json", audits)
    write_csv(out / "identity_audit.csv", audits)
    write_json(out / "identity_conflicts.json", at_risk)
    write_csv(out / "identity_conflicts.csv", at_risk)
    write_json(out / "declared_without_existing_audio.json", no_audio_copy)

    salvage_models = {(row["brand"], row["model"]) for row in salvage}
    salvage_by_model: dict[tuple[str, str], set[str]] = {}
    for row in salvage:
        salvage_by_model.setdefault((row["brand"], row["model"]), set()).add(row["kind"])
    by_key = {(r.get("brand", ""), r.get("model", "")): r for r in results}
    all_three_salvaged_or_existing = 0
    for key, kinds in salvage_by_model.items():
        result = by_key[key]
        total = set(kinds) | {
            kind for kind in KINDS if result.get(kind) and result.get(f"{kind}_url")
        }
        if total == set(KINDS):
            all_three_salvaged_or_existing += 1

    summary = {
        "models_in_source": len(results),
        "sound_manifest_tasks": len(manifest),
        "identity_accept": len(accepted_audits),
        "identity_reject": len(rejected_audits),
        "identity_unknown": len(unknown_audits),
        "existing_models_with_assignments_at_risk": len({
            (r["brand"], r["model"]) for r in at_risk
        }),
        "safe_salvage_assignments": len(salvage),
        "safe_salvage_models": len(salvage_models),
        "models_reaching_three_kinds_after_safe_salvage": all_three_salvaged_or_existing,
        "ambiguous_existing_audio_assignments": len(ambiguous),
        "needs_online_identity_revalidation": len(revalidate),
        "declared_but_not_in_current_sound_vault": len(no_audio_copy),
    }
    write_json(out / "summary.json", summary)
    (out / "summary.md").write_text(
        "# Planet v2 offline audit + salvage\n\n"
        f"- Models audited: **{summary['models_in_source']}**\n"
        f"- Legacy identity: **{summary['identity_accept']} ACCEPT / {summary['identity_reject']} REJECT / {summary['identity_unknown']} UNKNOWN**\n"
        f"- Existing models with at least one assignment at obvious identity risk: **{summary['existing_models_with_assignments_at_risk']}**\n"
        f"- Safe no-download salvage assignments: **{summary['safe_salvage_assignments']}** across **{summary['safe_salvage_models']}** models\n"
        f"- Models reaching all three kinds after safe salvage: **{summary['models_reaching_three_kinds_after_safe_salvage']}**\n"
        f"- Filename matches blocked by multiple checksum variants: **{summary['ambiguous_existing_audio_assignments']}**\n"
        f"- Filename matches waiting for online identity revalidation: **{summary['needs_online_identity_revalidation']}**\n"
        f"- Declared defaults still absent from current sound vault: **{summary['declared_but_not_in_current_sound_vault']}**\n\n"
        "The v1 artifact is not modified. REUSED_UNIQUE_AUDIO is cross-model byte reuse, not exact-device archive proof.\n",
        encoding="utf-8",
    )
    return summary


def command_salvage(args) -> int:
    source_arg = Path(args.source)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if source_arg.is_dir():
        summary = run_salvage(source_arg, out)
    else:
        with tempfile.TemporaryDirectory() as td:
            extracted = Path(td) / "source"
            extracted.mkdir()
            with zipfile.ZipFile(source_arg) as zf:
                zf.extractall(extracted)
            summary = run_salvage(extracted, out)
    print(json.dumps(summary, indent=2))
    return 0


def self_test() -> None:
    assert sound_norm("Over_the_Horizon.ogg") == sound_norm("Over the horizon.OGG")
    fake = {
        "notes": "ringtone declared as Tone.ogg but no archive filename matched | alarm declared as Beep.ogg but no archive filename matched"
    }
    assert declared_value(fake, "ringtone") == "Tone.ogg"
    assert declared_value(fake, "alarm") == "Beep.ogg"
    print("SALVAGE RUNNER SELF-TEST PASSED")


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline audit/salvage for a completed planet artifact")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("self-test")
    p = sub.add_parser("salvage")
    p.add_argument("--source", required=True, help="Extracted final artifact directory or artifact ZIP")
    p.add_argument("--out", required=True)
    args = parser.parse_args()
    if args.cmd == "self-test":
        self_test()
        return 0
    return command_salvage(args)


if __name__ == "__main__":
    raise SystemExit(main())
