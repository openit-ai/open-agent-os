#!/usr/bin/env python3
"""Split an image directory into zip parts under a size limit.

Usage: pack-images.py <images_dir> <out_prefix> <limit_mb> [--single]

- Default: greedily group files so each part fits the limit, producing partNN.zip.
- --single: build one zip without splitting.
stdout: ZIP=<path> lines in order (machine-readable).
"""
from __future__ import annotations

import pathlib
import sys
import zipfile
import zlib

_OVERHEAD = 200  # slack for the zip local header + central directory entry


def compressed_size(path: pathlib.Path) -> int:
    """Estimate per-file compressed size (images are mostly stored-level)."""
    data = path.read_bytes()
    return len(zlib.compress(data, 6)) + _OVERHEAD


def write_zip(dest: pathlib.Path, files: list[pathlib.Path], root: pathlib.Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in files:
            zf.write(f, arcname=str(f.relative_to(root)))


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    single = "--single" in sys.argv
    images_dir = pathlib.Path(args[0]).resolve()
    out_prefix = args[1]
    limit_mb = float(args[2]) if len(args) > 2 else 20.0

    root = images_dir.parent
    files = sorted(
        (p for p in images_dir.rglob("*") if p.is_file()),
        key=lambda p: str(p),
    )
    if not files:
        print("EMPTY", file=sys.stderr)
        sys.exit(1)

    if single:
        dest = pathlib.Path(f"{out_prefix}.zip")
        write_zip(dest, files, root)
        print(f"ZIP={dest}")
        return

    limit = int(limit_mb * 1024 * 1024)
    sizes = {f: compressed_size(f) for f in files}

    # place largest files first (greedy); store each part in original order
    order = sorted(files, key=lambda p: -sizes[p])
    parts: list[list[pathlib.Path]] = []
    loads: list[int] = []
    for f in order:
        size = sizes[f]
        if size > limit:
            print(f"WARN: single file exceeds the limit: {f.name} ({size} B)", file=sys.stderr)
        placed = False
        for i, load in enumerate(loads):
            if load + size <= limit:
                parts[i].append(f)
                loads[i] += size
                placed = True
                break
        if not placed:
            parts.append([f])
            loads.append(size)

    for i, part in enumerate(parts, 1):
        dest = pathlib.Path(f"{out_prefix}-part{i:02d}.zip")
        write_zip(dest, sorted(part, key=lambda p: str(p)), root)
        print(f"ZIP={dest}")

    total = sum(loads)
    print(
        f"INFO: {len(files)} files → {len(parts)} parts "
        f"(total {total / 1024 / 1024:.1f} MB, largest part {max(loads) / 1024 / 1024:.1f} MB)",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
