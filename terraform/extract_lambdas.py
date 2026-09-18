#!/usr/bin/env python3
"""One-shot helper: extract the inline Lambda `ZipFile: |` blocks from the
CloudFormation template into standalone index.py files for the Terraform port.

Maps each CFN Lambda logical id -> output directory. Only the four core
pipeline functions are extracted; the custom-resource helpers are replaced by
native Terraform resources and are intentionally skipped.
"""
import os
import sys

TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "template.yaml")

# logical id of the function resource -> output subdir under functions/
TARGETS = {
    "StartQueryFunction": "startquery",
    "ManifestMakerFunction": "manifestmaker",
    "CreateJobFunction": "createjob",
    "JobCompletionFunction": "jobcompletion",
}


def extract(lines, func_logical_id):
    """Return the dedented body of the ZipFile block for a given function id."""
    # Find the resource block start: two-space indented "  <id>:"
    start = None
    for i, line in enumerate(lines):
        if line.rstrip("\n") == f"  {func_logical_id}:":
            start = i
            break
    if start is None:
        raise SystemExit(f"resource {func_logical_id} not found")

    # Find "ZipFile: |" after start
    zip_idx = None
    for i in range(start, len(lines)):
        if lines[i].strip() == "ZipFile: |":
            zip_idx = i
            break
        # stop if we hit the next top-level resource
        if i > start and lines[i].startswith("  ") and not lines[i].startswith("   ") and lines[i].rstrip().endswith(":"):
            break
    if zip_idx is None:
        raise SystemExit(f"ZipFile block not found for {func_logical_id}")

    # The block scalar body is indented deeper than "ZipFile:". Determine the
    # indent of the first non-blank body line and strip exactly that.
    body = []
    block_indent = None
    for i in range(zip_idx + 1, len(lines)):
        raw = lines[i].rstrip("\n")
        if raw.strip() == "" and block_indent is None:
            body.append("")
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if block_indent is None:
            block_indent = indent
        # A line with less indentation than the block (and non-blank) ends it.
        if raw.strip() != "" and indent < block_indent:
            break
        body.append(raw[block_indent:] if len(raw) >= block_indent else raw.strip())
    # Trim trailing blank lines
    while body and body[-1] == "":
        body.pop()
    return "\n".join(body) + "\n"


def main():
    with open(TEMPLATE) as f:
        lines = f.readlines()
    for logical_id, subdir in TARGETS.items():
        code = extract(lines, logical_id)
        out = os.path.join(os.path.dirname(__file__), "functions", subdir, "index.py")
        with open(out, "w") as f:
            f.write(code)
        print(f"wrote {out} ({len(code.splitlines())} lines)")


if __name__ == "__main__":
    main()
