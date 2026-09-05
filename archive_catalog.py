#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import urllib.parse
from collections import defaultdict
from pathlib import Path

# Reuse the archive-only TLS workaround and crawler primitives.
import archive_runner

pd = archive_runner.phone_defaults


def key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def immediate_directories(base_url: str):
    page = pd.text(base_url)
    base_path = urllib.parse.urlparse(base_url).path
    out = []
    for url in pd.children(base_url, page):
        path = urllib.parse.urlparse(url).path
        rel = path[len(base_path):].strip("/") if path.startswith(base_path) else ""
        if not path.endswith("/") or not rel or "/" in rel:
            continue
        name = urllib.parse.unquote(rel)
        if name.startswith("!") or name.startswith("."):
            continue
        out.append((name, url))
    return sorted(set(out), key=lambda item: item[0].lower())


def crawl(delay: float = 0.05):
    root = pd.ARCHIVE_ROOT.rstrip("/") + "/"
    print(f"[root] {root}", file=sys.stderr)
    brands = immediate_directories(root)
    print(f"[brands] {len(brands)}", file=sys.stderr)

    rows = []
    failures = []
    for index, (brand, brand_url) in enumerate(brands, 1):
        print(f"[{index}/{len(brands)}] {brand}", file=sys.stderr)
        try:
            models = immediate_directories(brand_url)
        except Exception as exc:
            print(f"  [warn] {exc}", file=sys.stderr)
            failures.append({"brand": brand, "url": brand_url, "error": str(exc)})
            continue

        print(f"  models: {len(models)}", file=sys.stderr)
        for model, model_url in models:
            rows.append({
                "brand": brand,
                "model": model,
                "model_key": key(model),
                "archive_url": model_url,
            })
        if delay:
            time.sleep(delay)

    rows.sort(key=lambda row: (row["brand"].lower(), row["model"].lower()))
    return brands, rows, failures


def write_outputs(out_dir: Path, brands, rows, failures):
    out_dir.mkdir(parents=True, exist_ok=True)

    with (out_dir / "models.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=["brand", "model", "model_key", "archive_url"])
        writer.writeheader()
        writer.writerows(rows)

    (out_dir / "models.json").write_text(
        json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    grouped = defaultdict(list)
    for row in rows:
        grouped[row["brand"]].append(row["model"])
    by_brand = {brand: grouped.get(brand, []) for brand, _ in brands}
    (out_dir / "by_brand.json").write_text(
        json.dumps(by_brand, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    counts = [
        {"brand": brand, "models": len(grouped.get(brand, []))}
        for brand, _ in brands
    ]
    counts.sort(key=lambda item: (-item["models"], item["brand"].lower()))
    (out_dir / "brand_counts.json").write_text(
        json.dumps(counts, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (out_dir / "failures.json").write_text(
        json.dumps(failures, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    unique_models = sorted({row["model"] for row in rows}, key=str.casefold)
    lines = [
        "# André Phone Tones archive catalog",
        "",
        f"- Brands discovered: **{len(brands)}**",
        f"- Model folders discovered: **{len(rows)}**",
        f"- Unique model names: **{len(unique_models)}**",
        f"- Brand pages that failed: **{len(failures)}**",
        "",
        "## Largest brand folders",
        "",
        "| Brand | Model folders |",
        "|---|---:|",
    ]
    for item in counts[:30]:
        lines.append(f"| {item['brand']} | {item['models']} |")
    lines.append("")
    (out_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def yaml_string(value: str) -> str:
    # JSON double-quoted strings are valid YAML double-quoted scalars and safely
    # handle punctuation, quotes, Unicode and weird archive folder names.
    return json.dumps(value, ensure_ascii=True)


def sync_model_dropdown(rows, workflow_path: Path) -> int:
    """Replace model_preset.options with every unique archive model name."""
    unique_models = sorted({row["model"] for row in rows}, key=str.casefold)
    if not unique_models:
        raise RuntimeError("Refusing to erase model dropdown: catalog has no model names")

    preferred_default = "Galaxy-S9"
    default = preferred_default if preferred_default in unique_models else unique_models[0]

    block = [
        "      model_preset:",
        f"        description: \"Archive model name ({len(unique_models)} unique names; typed filter below overrides)\"",
        "        required: true",
        f"        default: {yaml_string(default)}",
        "        type: choice",
        "        options:",
    ]
    block.extend(f"          - {yaml_string(model)}" for model in unique_models)
    replacement = "\n".join(block) + "\n"

    text = workflow_path.read_text(encoding="utf-8")
    pattern = re.compile(r"(?ms)^      model_preset:\n.*?(?=^      model:\n)")
    updated, count = pattern.subn(replacement, text, count=1)
    if count != 1:
        raise RuntimeError(f"Could not uniquely locate model_preset block in {workflow_path}")

    workflow_path.write_text(updated, encoding="utf-8")
    print(
        f"MODEL DROPDOWN SYNCED: {len(unique_models)} unique names -> {workflow_path}",
        file=sys.stderr,
    )
    return len(unique_models)


def main():
    parser = argparse.ArgumentParser(description="Catalog André's brand/model folders.")
    parser.add_argument("out_dir", nargs="?", default="out/archive-catalog")
    parser.add_argument(
        "--sync-workflow",
        default="",
        metavar="PATH",
        help="Replace model_preset options in a workflow YAML with all unique model names.",
    )
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    brands, rows, failures = crawl()
    write_outputs(out_dir, brands, rows, failures)

    synced = 0
    if args.sync_workflow:
        synced = sync_model_dropdown(rows, Path(args.sync_workflow))

    print(
        f"CATALOG COMPLETE: brands={len(brands)} models={len(rows)} "
        f"failures={len(failures)} dropdown={synced or 'not-synced'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
