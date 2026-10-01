"""Convert the Amazon Fire TV IR profile dump into fire-blaster profile files.

Source: https://github.com/shaikh-amaan-fm/fire_tv_remote_ir_db (ir_profiles_db.json)

Each entry is one code set ("configuration") for one device model family,
with Pronto hex codes per function. code2, when present, is the toggle-bit
alternate of code1 (RC5/RC6 style) and becomes a second variant.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter
from pathlib import Path

from . import pronto
from .pronto import ProntoError

log = logging.getLogger(__name__)

AMAZON_DEVICE_TYPES = {1: "tv", 2: "stb", 3: "avr", 4: "soundbar"}
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-") or "unknown"


def _toml_str(value: str) -> str:
    # JSON string escapes are a subset of TOML basic-string escapes.
    return json.dumps(value, ensure_ascii=False)


def _toml_key(key: str) -> str:
    return key if _BARE_KEY.match(key) else _toml_str(key)


def _normalize(code: str) -> str:
    return " ".join(code.split()).upper()


def convert_entry(entry: dict, stats: Counter) -> dict | None:
    """One database entry -> profile dict, or None if it has no usable codes."""
    c = entry["configuration"]
    code_set = c.get("codeSet") or {}
    codes: dict[str, list[str]] = {}
    for ir in code_set.get("irCodes") or []:
        function = (ir.get("deviceFunction") or {}).get("name")
        if not function:
            continue
        if function in codes:
            stats["duplicate functions"] += 1
            continue
        variants = []
        for raw in (ir.get("code1"), ir.get("code2")):
            if not raw or not raw.strip():
                continue
            try:
                pronto.decode(raw)
            except ProntoError:
                stats["undecodable codes"] += 1
                continue
            variants.append(_normalize(raw))
        if variants:
            codes[function] = variants
    if not codes:
        stats["entries without usable codes"] += 1
        return None

    type_id = (c.get("deviceType") or {}).get("id")
    device_type = AMAZON_DEVICE_TYPES.get(type_id, f"type{type_id}")
    # Projectors share the TV type id; split them out so TV setup skips them.
    if device_type == "tv" and c.get("projector"):
        device_type = "projector"
    return {
        "id": f"amazon-{c['id']}",
        "brand": ((c.get("brand") or {}).get("name") or "Unknown").strip(),
        "name": (c.get("name") or code_set.get("name") or f"Code set {c['id']}").strip(),
        "device_type": device_type,
        "confidence": int((code_set.get("confidence") or {}).get("irConfidence") or 0),
        "blast_count": int(code_set.get("blastCount") or 1),
        "codes": codes,
        "source_id": c["id"],
    }


def to_toml(profile: dict) -> str:
    lines = [
        f"# Converted from the Amazon Fire TV IR database (configuration {profile['source_id']}).",
        f"id = {_toml_str(profile['id'])}",
        f"brand = {_toml_str(profile['brand'])}",
        f"name = {_toml_str(profile['name'])}",
        f"device_type = {_toml_str(profile['device_type'])}",
        f"confidence = {profile['confidence']}",
        f"blast_count = {profile['blast_count']}",
        "",
        "[codes]",
    ]
    for function, variants in profile["codes"].items():
        lines.append(f"{_toml_key(function)} = [{', '.join(_toml_str(v) for v in variants)}]")
    return "\n".join(lines) + "\n"


def import_db(db: dict, out_dir: Path, device_types: set[str] | None, brands: set[str] | None) -> Counter:
    stats: Counter = Counter()
    wanted_brands = {b.casefold() for b in brands} if brands else None
    for entry in db.values():
        profile = convert_entry(entry, stats)
        if profile is None:
            continue
        if device_types and profile["device_type"] not in device_types:
            stats["skipped (device type)"] += 1
            continue
        if wanted_brands and profile["brand"].casefold() not in wanted_brands:
            stats["skipped (brand)"] += 1
            continue
        path = out_dir / profile["device_type"] / f"{_slug(profile['brand'])}-{profile['source_id']}.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(to_toml(profile))
        stats["profiles written"] += 1
        stats[f"  {profile['device_type']}"] += 1
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fireblaster-import-amazon", description="Convert Amazon's IR profile dump to fire-blaster profiles.")
    parser.add_argument("db", type=Path, help="ir_profiles_db.json")
    parser.add_argument("-o", "--out", type=Path, required=True, help="output directory (files go in <out>/<device_type>/)")
    parser.add_argument("-t", "--type", action="append", choices=sorted([*AMAZON_DEVICE_TYPES.values(), "projector"]), help="device type to keep; repeatable (default: tv)")
    parser.add_argument("--all-types", action="store_true", help="keep every device type")
    parser.add_argument("-b", "--brand", action="append", help="only this brand; repeatable (case-insensitive)")
    args = parser.parse_args(argv)

    try:
        db = json.loads(args.db.read_text())
    except (OSError, ValueError) as e:
        print(f"error: cannot read {args.db}: {e}", file=sys.stderr)
        return 1

    types = None if args.all_types else set(args.type or ["tv"])
    stats = import_db(db, args.out, types, set(args.brand) if args.brand else None)
    for key, count in sorted(stats.items(), key=lambda kv: kv[0].lstrip()):
        print(f"{key}: {count}")
    return 0 if stats["profiles written"] else 1


if __name__ == "__main__":
    sys.exit(main())
