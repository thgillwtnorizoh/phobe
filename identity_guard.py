#!/usr/bin/env python3
"""Conservative identity/provenance checks for phone default-sound evidence.

The v1 harvester proved the sound-resolution pipeline, but short archive names can
collide with unrelated devices (S8, G9, Mix2, Note-3, etc.).  This module rejects
an evidence source when its own identity/provenance points at another phone or at
an unrelated custom ROM.  It intentionally returns UNKNOWN rather than guessing
when marketing-name-to-codename/SKU mapping is not yet known.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import base64
import re
from typing import Iterable
import urllib.parse

COMPATIBLE_GROUPS = {
    "huawei": {"honor"},
    "honor": {"huawei"},
    "sony": {"sony ericsson"},
    "sony ericsson": {"sony"},
}

GENERIC_BRANDS = {
    "", "android", "aosp", "generic", "unknown", "qcom", "qualcomm",
    "mediatek", "mtk", "sprd", "unisoc", "alps", "rockchip", "softwinner",
}

BRAND_ALIASES: dict[str, tuple[str, ...]] = {
    "samsung": ("samsung", "galaxy"),
    "xiaomi": ("xiaomi", "redmi", "poco", "black shark"),
    "google": ("google", "pixel"),
    "sony": ("sony", "xperia"),
    "sony ericsson": ("sony ericsson", "sonyericsson", "semc"),
    "motorola": ("motorola", "moto"),
    "oneplus": ("oneplus",),
    "oppo": ("oppo",),
    "vivo": ("vivo",),
    "realme": ("realme",),
    "huawei": ("huawei",),
    "honor": ("honor",),
    "lg": ("lg", "lge", "lg electronics"),
    "htc": ("htc",),
    "nokia": ("nokia", "hmd global", "hmdglobal"),
    "microsoft": ("microsoft", "surface"),
    "blackberry": ("blackberry",),
    "blu": ("blu", "blu products", "bluproducts"),
    "bluboo": ("bluboo",),
    "allcall": ("allcall",),
    "coolpad": ("coolpad",),
    "kyocera": ("kyocera",),
    "meizu": ("meizu",),
    "zte": ("zte",),
    "alcatel": ("alcatel", "tct"),
    "lenovo": ("lenovo",),
    "asus": ("asus", "zenfone"),
    "acer": ("acer",),
    "tecno": ("tecno",),
    "infinix": ("infinix",),
    "nothing": ("nothing",),
    "pantech": ("pantech",),
    "panasonic": ("panasonic",),
    "sharp": ("sharp",),
    "sanyo": ("sanyo",),
    "siemens": ("siemens",),
    "apple": ("apple", "iphone"),
    "fairphone": ("fairphone",),
    "essential": ("essential",),
    "nubia": ("nubia",),
    "leeco": ("leeco", "letv"),
    "fly": ("fly",),
    "explay": ("explay",),
    "mobicel": ("mobicel",),
    "balmuda": ("balmuda",),
}

CUSTOM_ROM_MARKERS = (
    "lineageos", "lineage os", "cyanogenmod", "cyanogen mod", "crdroid",
    "pixel experience", "resurrection remix", "paranoid android", "evolution x",
)

OEM_ROM_BRAND = {
    "miui": "xiaomi",
    "micode": "xiaomi",
    "patchrom": "xiaomi",
    "hyperos": "xiaomi",
    "oxygenos": "oneplus",
    "oxygen os": "oneplus",
    "one ui": "samsung",
    "oneui": "samsung",
    "emui": "huawei",
    "magicui": "honor",
    "magic ui": "honor",
    "magicos": "honor",
    "magic os": "honor",
    "funtouchos": "vivo",
    "funtouch os": "vivo",
    "originos": "vivo",
    "origin os": "vivo",
    "realme ui": "realme",
    "zenui": "asus",
}

IDENTITY_KEY_RE = re.compile(r"(?:^|\.)(manufacturer|brand|model|device|name)$", re.I)


@dataclass
class IdentityVerdict:
    status: str
    reason: str
    brand_values: list[str]
    model_values: list[str]
    device_values: list[str]
    source_brand_groups: list[str]
    custom_rom_markers: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", urllib.parse.unquote(value).lower())


def _tokens(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", urllib.parse.unquote(value).lower())


def _contains_sequence(haystack: list[str], needle: list[str]) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    return any(haystack[i:i + len(needle)] == needle for i in range(len(haystack) - len(needle) + 1))


def canonical_brand(brand: str) -> str:
    target = _compact(brand)
    for canonical, aliases in BRAND_ALIASES.items():
        if target == _compact(canonical):
            return canonical
        if any(target == _compact(alias) for alias in aliases):
            return canonical
    return brand.strip().lower()


def brand_aliases(brand: str) -> tuple[str, ...]:
    canonical = canonical_brand(brand)
    return BRAND_ALIASES.get(canonical, (brand,))


def groups_compatible(requested: str, source: str) -> bool:
    return requested == source or source in COMPATIBLE_GROUPS.get(requested, set())


def brand_value_matches(brand: str, value: str) -> bool:
    value_compact = _compact(value)
    if value_compact in GENERIC_BRANDS:
        return False
    requested = canonical_brand(brand)
    source = canonical_brand(value)
    if groups_compatible(requested, source):
        return True
    for alias in brand_aliases(brand):
        alias_compact = _compact(alias)
        if not alias_compact:
            continue
        if value_compact == alias_compact:
            return True
        if len(alias_compact) >= 4 and alias_compact in value_compact:
            return True
    return False


def parse_identity(body: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {
        "brand": [], "manufacturer": [], "model": [], "device": [], "name": []
    }

    def add(key: str, value: str) -> None:
        tail = key.strip().lower().split(".")[-1]
        if tail not in found:
            return
        value = value.strip().strip('"\'').strip()
        if value and value not in found[tail]:
            found[tail].append(value)

    normal = re.compile(
        r"^\s*([A-Za-z0-9_.-]+)\s*(?:=|:)\s*[\"']?([^\"'\r\n#;]+)", re.M
    )
    for match in normal.finditer(body):
        key = match.group(1)
        if key.startswith("ro.") and IDENTITY_KEY_RE.search(key):
            add(key, match.group(2))

    bracket = re.compile(r"\[([^\]]+)\]\s*:\s*\[([^\]]*)\]")
    for match in bracket.finditer(body):
        key = match.group(1).strip()
        if key.startswith("ro.") and IDENTITY_KEY_RE.search(key):
            add(key, match.group(2))

    lines = body.splitlines()
    key_re = re.compile(r"[\"'](ro\.[^\"']*(?:manufacturer|brand|model|device|name))[\"']", re.I)
    value_re = re.compile(r"\.value\s*=\s*[\"']([^\"']+)[\"']", re.I)
    for index, raw in enumerate(lines):
        km = key_re.search(raw)
        if not km or not IDENTITY_KEY_RE.search(km.group(1)):
            continue
        for follow in range(index, min(index + 5, len(lines))):
            vm = value_re.search(lines[follow])
            if vm:
                add(km.group(1), vm.group(1))
                break
    return found


def _source_brand_groups(text: str) -> set[str]:
    tokens = _tokens(text)
    groups: set[str] = set()
    for canonical, aliases in BRAND_ALIASES.items():
        for alias in aliases:
            if _contains_sequence(tokens, _tokens(alias)):
                groups.add(canonical)
                break
    if re.search(r"(?i)(?:^|[/_. -])SM[-_ ]?[A-Z]?\d{3,}", text):
        groups.add("samsung")
    lowered = text.lower()
    for marker, canonical in OEM_ROM_BRAND.items():
        if marker in lowered:
            groups.add(canonical)
    return groups


def _custom_roms(text: str) -> list[str]:
    low = text.lower()
    return sorted({marker for marker in CUSTOM_ROM_MARKERS if marker in low})


def _oem_rom_conflict(brand: str, model: str, source_text: str) -> str | None:
    low = source_text.lower()
    model_low = model.lower()
    requested = canonical_brand(brand)
    for marker, owner in OEM_ROM_BRAND.items():
        if marker not in low:
            continue
        if marker in model_low:
            continue
        if not groups_compatible(requested, owner):
            return f"OEM ROM {marker!r} belongs to {owner}, not {requested}"
    return None


def _model_mentioned(model: str, values: Iterable[str]) -> bool:
    needle = _tokens(model)
    compact = _compact(model)
    for value in values:
        hay = _tokens(value)
        if _contains_sequence(hay, needle):
            return True
        if compact and _compact(value) == compact:
            return True
    return False


def validate_source(brand: str, model: str, body: str, repo: str = "", path: str = "") -> IdentityVerdict:
    identity = parse_identity(body)
    source_text = f"{repo} {path}"
    source_groups = _source_brand_groups(source_text)
    custom = _custom_roms(source_text + "\n" + body[:20000])
    requested = canonical_brand(brand)

    brand_values = identity["manufacturer"] + identity["brand"]
    meaningful_brand_values = [v for v in brand_values if _compact(v) not in GENERIC_BRANDS]
    if meaningful_brand_values and not any(brand_value_matches(brand, v) for v in meaningful_brand_values):
        return IdentityVerdict(
            "REJECT", f"source declares another brand/manufacturer: {meaningful_brand_values[:4]}",
            brand_values, identity["model"], identity["device"] + identity["name"],
            sorted(source_groups), custom,
        )

    if source_groups and not any(groups_compatible(requested, group) for group in source_groups):
        requested_is_visible = any(_contains_sequence(_tokens(source_text), _tokens(a)) for a in brand_aliases(brand))
        if not requested_is_visible:
            return IdentityVerdict(
                "REJECT", f"source path/repository points at another brand family: {sorted(source_groups)}",
                brand_values, identity["model"], identity["device"] + identity["name"],
                sorted(source_groups), custom,
            )

    if custom and not any(marker in model.lower() for marker in custom):
        return IdentityVerdict(
            "REJECT", f"custom ROM evidence is not factory-default evidence: {custom}",
            brand_values, identity["model"], identity["device"] + identity["name"],
            sorted(source_groups), custom,
        )

    conflict = _oem_rom_conflict(brand, model, source_text + "\n" + body[:20000])
    if conflict:
        return IdentityVerdict(
            "REJECT", conflict, brand_values, identity["model"],
            identity["device"] + identity["name"], sorted(source_groups), custom,
        )

    model_values = identity["model"] + identity["device"] + identity["name"]
    model_tied = _model_mentioned(model, [source_text, *model_values])
    brand_tied = any(brand_value_matches(brand, v) for v in meaningful_brand_values)
    if not brand_tied:
        brand_tied = any(groups_compatible(requested, group) for group in source_groups) or any(
            _contains_sequence(_tokens(source_text), _tokens(alias)) for alias in brand_aliases(brand)
        )

    if brand_tied and model_tied:
        return IdentityVerdict(
            "ACCEPT", "source is tied to both requested brand and archive model",
            brand_values, identity["model"], identity["device"] + identity["name"],
            sorted(source_groups), custom,
        )

    missing = []
    if not brand_tied:
        missing.append("brand")
    if not model_tied:
        missing.append("model")
    return IdentityVerdict(
        "UNKNOWN", "source could not be tied safely to requested " + "+".join(missing),
        brand_values, identity["model"], identity["device"] + identity["name"],
        sorted(source_groups), custom,
    )


def static_source_verdict(brand: str, model: str, repo: str, path: str) -> IdentityVerdict:
    source = f"{repo} {path}"
    source_groups = _source_brand_groups(source)
    requested = canonical_brand(brand)
    custom = _custom_roms(source)

    if source_groups:
        foreign = sorted(group for group in source_groups if not groups_compatible(requested, group))
        if foreign:
            return IdentityVerdict(
                "REJECT", f"offline source also points at another brand family: {foreign}",
                [], [], [], sorted(source_groups), custom,
            )
        if not any(groups_compatible(requested, group) for group in source_groups):
            requested_visible = any(_contains_sequence(_tokens(source), _tokens(a)) for a in brand_aliases(brand))
            if not requested_visible:
                return IdentityVerdict(
                    "REJECT", f"offline source points at another brand family: {sorted(source_groups)}",
                    [], [], [], sorted(source_groups), custom,
                )

    if custom and not any(marker in model.lower() for marker in custom):
        return IdentityVerdict(
            "REJECT", f"offline source is an aftermarket/custom ROM: {custom}",
            [], [], [], sorted(source_groups), custom,
        )

    conflict = _oem_rom_conflict(brand, model, source)
    if conflict:
        return IdentityVerdict("REJECT", conflict, [], [], [], sorted(source_groups), custom)

    brand_tied = any(groups_compatible(requested, group) for group in source_groups) or any(
        _contains_sequence(_tokens(source), _tokens(alias)) for alias in brand_aliases(brand)
    )
    model_tied = _model_mentioned(model, [source])
    if brand_tied and model_tied:
        return IdentityVerdict(
            "ACCEPT", "offline repo/path explicitly ties requested brand and model",
            [], [], [], sorted(source_groups), custom,
        )
    return IdentityVerdict(
        "UNKNOWN", "offline metadata is not strong enough to prove both brand and model",
        [], [], [], sorted(source_groups), custom,
    )


_REPO_BRANCH_CACHE: dict[tuple[str, bool], str] = {}
_SOURCE_BODY_CACHE: dict[tuple[str, str, bool], str] = {}


def fetch_github_source(repo: str, path: str, token: str, json_loader, byte_loader=None) -> str:
    cache_key = (repo, path, bool(token))
    if cache_key in _SOURCE_BODY_CACHE:
        return _SOURCE_BODY_CACHE[cache_key]
    branch_key = (repo, bool(token))
    branch = _REPO_BRANCH_CACHE.get(branch_key)
    if not branch:
        meta = json_loader(f"https://api.github.com/repos/{repo}", token)
        branch = meta.get("default_branch") or "main"
        _REPO_BRANCH_CACHE[branch_key] = branch
    endpoint = (
        f"https://api.github.com/repos/{repo}/contents/"
        f"{urllib.parse.quote(path, safe='/')}?ref={urllib.parse.quote(branch, safe='')}"
    )
    data = json_loader(endpoint, token)
    encoded = data.get("content", "") if isinstance(data, dict) else ""
    if encoded:
        body = base64.b64decode(encoded).decode("utf-8", "replace")
    elif isinstance(data, dict) and data.get("download_url") and byte_loader:
        body = byte_loader(data["download_url"], token).decode("utf-8", "replace")
    else:
        raise RuntimeError(f"GitHub contents response had no decodable content for {repo}/{path}")
    _SOURCE_BODY_CACHE[cache_key] = body
    return body


def filter_evidence(brand: str, model: str, evidence, token: str, json_loader, byte_loader=None):
    grouped: dict[tuple[str, str], list] = {}
    for item in evidence:
        grouped.setdefault((item.repo, item.path), []).append(item)
    kept = []
    audits = []
    for (repo, path), items in grouped.items():
        fetch_error = ""
        try:
            body = fetch_github_source(repo, path, token, json_loader, byte_loader)
            verdict = validate_source(brand, model, body, repo, path)
        except Exception as exc:
            fetch_error = f"{type(exc).__name__}: {exc}"
            verdict = static_source_verdict(brand, model, repo, path)
            if verdict.status == "ACCEPT":
                verdict.reason += "; source body fetch failed, accepted from strict repo/path identity only"
        audit = verdict.to_dict()
        audit.update({"repo": repo, "path": path, "evidence_count": len(items), "fetch_error": fetch_error})
        audits.append(audit)
        if verdict.status == "ACCEPT":
            kept.extend(items)
    return kept, audits


def self_test() -> None:
    wrong = """ro.product.manufacturer=Xiaomi\nro.product.brand=Redmi\nro.product.model=MIX 2\n"""
    assert validate_source("AllCall", "Mix2", wrong, "tools", "xiaomi_mix2/build.prop").status == "REJECT"

    samsung = """[ro.product.manufacturer]: [samsung]\n[ro.product.model]: [SM-G960U1]\n"""
    assert validate_source("Samsung", "Galaxy-S9", samsung, "pytorch/cpuinfo", "test/mock/galaxy-s9-us.h").status == "ACCEPT"

    lineage = """ro.product.manufacturer=motorola\nro.product.model=moto g84 5G\n"""
    assert validate_source("Motorola", "Moto-G84-5G", lineage, "dump", "LineageOS Android 16 moto g84 5G.txt").status == "REJECT"

    miui_port = """ro.product.manufacturer=HTC\nro.product.model=HTC One X\n"""
    assert validate_source("HTC", "One-X", miui_port, "MiCode/patchrom_onex", "other/build.prop").status == "REJECT"

    assert static_source_verdict("Bluboo", "S8", "pytorch/cpuinfo", "test/mock/galaxy-s8-us.h").status == "REJECT"
    assert static_source_verdict("Google", "Pixel-C", "pytorch/cpuinfo", "test/mock/pixel-c.h").status == "ACCEPT"
    assert static_source_verdict("Huawei", "Honor-6", "pytorch/cpuinfo", "test/mock/huawei-honor-6.h").status == "ACCEPT"
    print("IDENTITY GUARD SELF-TEST PASSED")


if __name__ == "__main__":
    self_test()
