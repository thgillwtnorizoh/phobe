#!/usr/bin/env python3
from __future__ import annotations

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

    lines = [
        "# André Phone Tones archive catalog",
        "",
        f"- Brands discovered: **{len(brands)}**",
        f"- Model folders discovered: **{len(rows)}**",
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


def main():
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "out/archive-catalog")
    brands, rows, failures = crawl()
    write_outputs(out_dir, brands, rows, failures)
    print(f"CATALOG COMPLETE: brands={len(brands)} models={len(rows)} failures={len(failures)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
