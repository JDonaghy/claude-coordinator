#!/usr/bin/env python3
"""Add or remove an ephemeral worker in coordinator.yml, preserving the file.

Runs on the DAEMON HOST (dellserver), where ~/.coord/coordinator.yml is the
real config. On a thin client that path is only a cache
(coordinator.remote.yml) that gets overwritten from GET /config, so editing it
there silently reverts.

Why text-based rather than a PyYAML round-trip: coordinator.yml is hand-
maintained and full of comments and deliberate ordering. yaml.safe_load ->
yaml.dump would strip every comment and reorder keys. So edits happen inside a
marker-delimited region, which one-time setup adds under `machines:`:

    machines:
      - name: precision
        ...
      # >>> epic-machines (managed by epic-up.sh) >>>
      # <<< epic-machines <<<

Everything outside the markers is byte-for-byte untouched.

The caller is responsible for atomicity: write with --out to a temp path,
validate it (`coord config --config <temp>`), then rename over the original.
`coord serve` reloads on mtime change (#1081), and a rename is atomic, so the
daemon never observes a torn file. A malformed config is *swallowed* by the
daemon (it keeps the last-good one and logs a warning), which is precisely why
validating before the rename matters — otherwise the edit silently no-ops.
"""

from __future__ import annotations

import argparse
import sys

BEGIN = "# >>> epic-machines"
END = "# <<< epic-machines"


def _region(lines: list[str]) -> tuple[int, int]:
    """Return (begin_idx, end_idx) of the managed region, or exit with help."""
    begin = end = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(BEGIN):
            begin = i
        elif stripped.startswith(END):
            end = i
    if begin < 0 or end < 0 or end < begin:
        sys.exit(
            "coordinator.yml has no epic-machines region. Add these two lines "
            "under `machines:` (indented to match the list items), once:\n"
            f"  {BEGIN} (managed by epic-up.sh) >>>\n"
            f"  {END} <<<"
        )
    return begin, end


def _entry_bounds(lines: list[str], begin: int, end: int, name: str) -> tuple[int, int] | None:
    """Bounds of the `- name: <name>` block inside the region, if present."""
    start = None
    for i in range(begin + 1, end):
        stripped = lines[i].strip()
        if stripped.startswith("- name:"):
            if start is not None:
                return (start, i)
            if stripped.split(":", 1)[1].strip() == name:
                start = i
        elif start is not None and stripped.startswith("- "):
            return (start, i)
    return (start, end) if start is not None else None


def _known_repos(lines: list[str]) -> set[str]:
    """Repo names declared in the config.

    `machines[].repos` entries must reference a declared repo or config parsing
    raises ConfigError -- which the daemon swallows, so an unknown repo name
    would look like "the edit did nothing". Catch it here with a real message.
    """
    repos: set[str] = set()
    in_repos = False
    for line in lines:
        if line.rstrip().startswith("repos:") and not line.startswith(" "):
            in_repos = True
            continue
        if in_repos:
            if line.strip() and not line.startswith((" ", "\t", "#")):
                break  # dedented to another top-level key
            s = line.strip()
            if s.startswith("- name:"):
                repos.add(s.split(":", 1)[1].strip().strip("\"'"))
    return repos


def cmd_add(args: argparse.Namespace, lines: list[str]) -> list[str]:
    begin, end = _region(lines)

    known = _known_repos(lines)
    wanted = [r for r in args.repos.split(",") if r]
    if known:
        unknown = [r for r in wanted if r not in known]
        if unknown:
            sys.exit(
                f"unknown repo(s) {unknown} — machines[].repos must reference a "
                f"repo declared in this config. Known: {sorted(known)}"
            )

    if _entry_bounds(lines, begin, end, args.name):
        sys.exit(f"machine {args.name!r} is already in the epic-machines region")

    # Match the indentation of the marker line so the block lands at the right
    # depth regardless of how the file is formatted.
    pad = " " * (len(lines[begin]) - len(lines[begin].lstrip()))
    block = [
        f"{pad}- name: {args.name}\n",
        f"{pad}  host: {args.host}\n",
        f"{pad}  capabilities: [{', '.join(args.capabilities.split(','))}]\n",
        f"{pad}  repos: [{', '.join(wanted)}]\n",
    ]
    # #1799: without repo_paths a freshly registered machine cannot receive a
    # single dispatch (`coord.dispatch.dispatch` refuses with "No repo_path
    # configured" before it ever gets near the provider/TOS gates). The image
    # bakes every repo at `--repo-root`/<name> (default ~/src/<name>), so
    # derive the mapping instead of making the caller spell each one out.
    root = args.repo_root.rstrip("/")
    block.append(f"{pad}  repo_paths:\n")
    for r in wanted:
        block.append(f"{pad}    {r}: {root}/{r}\n")
    if args.max_workers:
        block.append(f"{pad}  max_workers: {args.max_workers}\n")
    return lines[:end] + block + lines[end:]


def cmd_remove(args: argparse.Namespace, lines: list[str]) -> list[str]:
    begin, end = _region(lines)
    bounds = _entry_bounds(lines, begin, end, args.name)
    if not bounds:
        # Idempotent: epic-down must be safe to re-run after a partial failure.
        print(f"machine {args.name!r} not present — nothing to remove", file=sys.stderr)
        return lines
    start, stop = bounds
    return lines[:start] + lines[stop:]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", required=True, help="coordinator.yml to read")
    ap.add_argument("--out", required=True, help="where to write the result (use a temp path)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add")
    add.add_argument("--name", required=True, help="coordinator machine name")
    add.add_argument("--host", required=True, help="tailnet hostname — must match the VM's --hostname")
    add.add_argument("--capabilities", default="rust,python")
    add.add_argument("--repos", required=True, help="comma-separated repo names")
    add.add_argument(
        "--repo-root",
        default="~/src",
        help=(
            "parent directory each --repos entry lives under on the machine "
            "(the golden image bakes every repo at <repo-root>/<name>). "
            "Used to derive machines[].repo_paths — without it a registered "
            "machine cannot receive a single dispatch (#1799)."
        ),
    )
    add.add_argument("--max-workers", type=int, default=0)

    rm = sub.add_parser("remove")
    rm.add_argument("--name", required=True)

    args = ap.parse_args()
    with open(args.file, encoding="utf-8") as fh:
        lines = fh.readlines()

    result = cmd_add(args, lines) if args.cmd == "add" else cmd_remove(args, lines)

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.writelines(result)


if __name__ == "__main__":
    main()
