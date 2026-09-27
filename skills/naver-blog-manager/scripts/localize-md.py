#!/usr/bin/env python3
"""Rewrite remote image links in a body md to zip-local relative paths.
Usage: localize-md.py <md-path>
Output: <stem>-body.md in the same folder (image links = images/<filename>)
"""
import csv
import pathlib
import re
import sys


def main() -> None:
    md_path = pathlib.Path(sys.argv[1]).resolve()
    imgdir = md_path.parent / "images"
    manifest = imgdir / "manifest.csv"

    # (post number, order) → filename
    mapping: dict[tuple[str, str], str] = {}
    if manifest.exists():
        for row in csv.DictReader(open(manifest, encoding="utf-8")):
            if row.get("width"):
                mapping[(row["post"], row["idx"])] = row["file"]

    text = md_path.read_text(encoding="utf-8")
    sections = re.split(r"\n## ", text)

    out = [sections[0]]
    for pno, section in enumerate(sections[1:], 1):
        counter = {"n": 0}

        def repl(match: "re.Match[str]") -> str:
            counter["n"] += 1
            name = mapping.get((str(pno), str(counter["n"])))
            return f"![](images/{name})" if name else match.group(0)

        out.append(re.sub(r"!\[\]\([^)]+\)", repl, section))

    dest = md_path.parent / f"{md_path.stem}-body.md"
    dest.write_text("\n## ".join(out), encoding="utf-8")
    replaced = len(re.findall(r"!\[\]\(images/", dest.read_text(encoding="utf-8")))
    print(f"localized body: {dest} ({replaced} links converted)")


if __name__ == "__main__":
    main()
