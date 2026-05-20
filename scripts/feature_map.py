#!/usr/bin/env python3
"""
Map captured page identities back to features by intersecting the identity's
source file with each feature's `files` field in feature.json.

Public API: attach_features(pages: list[dict]) -> list[dict]
  - Each page dict gets a `feature` field set (or kept null if no match).
  - Pages with class_name resolved to a source file inside SOURCE_ROOT take precedence.
  - Resolution is intentionally tolerant: substring match on source-relative paths,
    because feature.json may carry paths in slightly different shapes across projects.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

from _paths import FEATURE_JSON, SOURCE_ROOT


def load_feature_index() -> dict[str, dict]:
    """Return {feature_id: feature_blob} reading the per-feature JSON files."""
    if not FEATURE_JSON.exists():
        return {}
    try:
        feat_root = json.loads(FEATURE_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    features_section = feat_root.get("features", {})
    if not isinstance(features_section, dict):
        return {}
    index: dict[str, dict] = {}
    base = FEATURE_JSON.parent
    for feature_id, rel_path in features_section.items():
        # Some entries are dicts (e.g. feature_wiring with inline metadata), some are paths.
        if isinstance(rel_path, str):
            path = (base / rel_path).resolve()
            if path.exists():
                try:
                    index[feature_id] = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    continue
        elif isinstance(rel_path, dict):
            index[feature_id] = rel_path
    return index


def _feature_files(feature: dict) -> list[str]:
    """Pull a flat list of input source file paths from a feature blob.

    Reads only fields that carry INPUT source paths (Java/Kotlin/Swift/etc.):
      - context.source_reference.source_files  (canonical for v2 schema)
      - tasks[].reference_files                (per-task references to source)
      - source_files / source                  (legacy / flat schemas)

    Deliberately does NOT read `tasks[].files` — those are *target* paths (.cj output)
    and would create false positives when matching against the input source tree.
    """
    seen = set()
    out: list[str] = []

    def emit(value):
        if isinstance(value, str) and ("/" in value or "\\" in value):
            if value not in seen:
                seen.add(value)
                out.append(value)
        elif isinstance(value, list):
            for v in value:
                emit(v)

    # v2 canonical location
    ctx = feature.get("context")
    if isinstance(ctx, dict):
        ref = ctx.get("source_reference")
        if isinstance(ref, dict):
            emit(ref.get("source_files"))

    # Legacy / flat fallbacks
    for key in ("source_files", "source"):
        if key in feature:
            emit(feature[key])

    # Per-task references (source files used during conversion)
    tasks = feature.get("tasks")
    if isinstance(tasks, list):
        for t in tasks:
            if isinstance(t, dict):
                emit(t.get("reference_files"))
                emit(t.get("source_files"))
                emit(t.get("source_file"))

    return out


def match_feature(source_file: Optional[str], feature_index: dict[str, dict]) -> Optional[str]:
    """Return feature_id whose files list contains source_file (substring match)."""
    if not source_file:
        return None
    src_norm = source_file.replace("\\", "/").lstrip("./")
    best: Optional[str] = None
    best_score = 0
    for feat_id, blob in feature_index.items():
        for f in _feature_files(blob):
            fn = f.replace("\\", "/").lstrip("./")
            if fn == src_norm or fn.endswith("/" + src_norm) or src_norm.endswith("/" + fn):
                # exact-ish match wins immediately
                return feat_id
            # softer overlap: shared filename
            if Path(fn).name == Path(src_norm).name:
                score = len(Path(src_norm).name)
                if score > best_score:
                    best_score = score
                    best = feat_id
    return best


def attach_features(pages: list[dict]) -> tuple[list[dict], list[str]]:
    """Mutates pages in place to add `feature` field. Returns (pages, shell_features).

    shell_features is the list of features that exist in feature.json but have no UI surface
    matched against any captured page. arch-gen consumes this to know "this feature is shell-only".
    """
    index = load_feature_index()
    matched_features: set[str] = set()
    for page in pages:
        src = page.get("source_file")
        feat = match_feature(src, index)
        page["feature"] = feat
        if feat:
            matched_features.add(feat)
    shell_features = sorted(set(index.keys()) - matched_features)
    return pages, shell_features


def resolve_source_file_from_class(class_name: str) -> Optional[str]:
    """Grep SOURCE_ROOT for a file defining `class <name>`. Best-effort.

    Returns source-root-relative POSIX path, or None.
    """
    if not class_name:
        return None
    target_basenames = {f"{class_name}.java", f"{class_name}.kt"}
    skip = {".git", "build", "node_modules", ".gradle", ".idea"}
    for p in SOURCE_ROOT.rglob("*"):
        if not p.is_file():
            continue
        if p.name in target_basenames and not any(part in skip for part in p.parts):
            try:
                return p.relative_to(SOURCE_ROOT).as_posix()
            except ValueError:
                return p.as_posix()
    return None


if __name__ == "__main__":
    # CLI: feed a list of {class_name, source_file?} on stdin, get back with features attached.
    raw = sys.stdin.read().strip()
    if not raw:
        print("usage: echo '[{\"class_name\":\"...\"},...]' | feature_map.py", file=sys.stderr)
        sys.exit(2)
    pages = json.loads(raw)
    for p in pages:
        if not p.get("source_file"):
            p["source_file"] = resolve_source_file_from_class(p.get("class_name", ""))
    pages, shell = attach_features(pages)
    json.dump({"pages": pages, "shell_features": shell}, sys.stdout, indent=2)
