#!/usr/bin/env python3
"""
Split tui/src/app/mod.rs: extract format.rs, types.rs, data.rs submodules.

Usage: python3 split_app.py [--dry-run]

Items are moved verbatim; `pub(crate)` visibility is added to each
top-level item that goes to a new file. mod.rs gains three `mod`
declarations + glob `use` imports so existing code compiles unchanged.
"""

import re
import sys
from pathlib import Path

DRY_RUN = '--dry-run' in sys.argv

BASE = Path('tui/src/app')
MOD_RS = BASE / 'mod.rs'

# ── Routing tables ────────────────────────────────────────────────────────────

FORMAT_NAMES: set = {
    'fmt_dur', 'fmt_elapsed_mmss', 'capitalize', 'format_unix_time',
    'fmt_tokens', 'format_cost_usd', 'collapse_ws', 'trunc',
    'fuzzy_score', 'word_wrap',
}

TYPES_NAMES: set = {
    # Supporting types used inside DTOs
    'TestPlanStep', 'TestStepJob',
    # Tab / mode enums
    'PipelineDetailTab', 'BoardDetailTab', 'SidebarView',
    # Core DTO structs + serde helpers
    'Assignment', 'de_bool_from_int_or_bool', 'deserialize_test_plan',
    'Machine', 'RawMachine', 'StagingEntry', 'BoardPayload',
    # Context-menu model types
    'ContextMenuTarget', 'PipelineRowLifecycle', 'BoardRowLifecycle',
    'PipelineMergeState', 'ContextMenuItem',
    # Queue + CI types
    'MergeQueueEntry', 'PlannedMergeEntry', 'CiCheckSummary', 'Proposal',
    # Issue / PR / review types
    'PipelineIssue', 'FetchedIssue', 'FetchedPr', 'FetchedReview', 'CoordReviewHeader',
    # More DTOs
    'SessionSummary', 'OpenIssue',
    # Model config
    'PipelineModels',
    'pipeline_models_default_tier', 'pipeline_models_default_escalation',
    'pipeline_models_default_escalate', 'fix_model_for_iteration',
    # Top-level data payload
    'BoardData', 'PlanData',
}

DATA_NAMES: set = {
    # Supporting types for data layer
    'MachineHealthResult',
    'METRICS_HISTORY', 'METRICS_CADENCE',
    'MetricSample', 'PendingMetrics',
    'ArtifactFile', 'ArtifactManifest', 'ArtifactAbsence',
    'ArtifactCacheEntry', 'ArtifactFetchOutcome',
    'InjectFallback', 'LiveTmuxSession',
    # SSE watch types — travel with spawn_sse_watch / make_local_sse_state
    'SseWatchMsg', 'WatchSseState', 'WATCH_POOL_CAP',
    # spawn_* functions
    'spawn_machine_health', 'spawn_machine_metrics', 'spawn_artifact_fetch',
    'spawn_inject_post', 'spawn_chat_continue',
    'spawn_remote_tmux_sessions_fetch', 'spawn_fix_briefing_fetch',
    'spawn_log_fetch', 'spawn_issue_fetch', 'spawn_pr_fetch',
    'spawn_comments_fetch', 'spawn_sse_watch', 'make_local_sse_state',
    # fetch_* functions
    'fetch_local_coord_version', 'fetch_live_tmux_sessions',
    'fetch_ci_check_summary', 'fetch_remote_config_to_cache',
    # parse_* functions
    'parse_sessions_json', 'parse_coord_review_header',
    'parse_session_summaries_from_comments', 'parse_coord_event_comment',
    'parse_iso8601_to_epoch', 'parse_pipeline_meta_from_map',
    'parse_plan_data', 'parse_test_plan_steps',
    # load_data* + board assembly
    'load_data', 'load_data_remote', 'start_data_load',
    'upsert_issue_db', 'resolve_board_service', 'is_remote_board_service',
    'assemble_board_data', 'compute_staging_local',
    'home_dir', 'coord_dir', 'tcp_probe', 'load_pipeline_meta',
    'open_purge_conn',
    # helper fns
    'issue_produces_build_artifact', 'artifact_absence_body',
    'sanitize_branch', 'read_git_branch_head',
    'extract_completion_summary', 'extract_review_summary',
}

# ── File headers ──────────────────────────────────────────────────────────────

FORMAT_HEADER = """\
//! Pure formatting helpers extracted from `app/mod.rs` (#743).
//!
//! No I/O, no quadraui types, no app state — pure text/number transformations.
use std::time::{SystemTime, UNIX_EPOCH};

"""

TYPES_HEADER = """\
//! App data-model types extracted from `app/mod.rs` (#743).
//!
//! DTO/enum structs and their pure impls — no I/O, no quadraui rendering.
use std::time::{Instant, SystemTime, UNIX_EPOCH};
use quadraui::Color;
use super::format::fmt_dur;

"""

DATA_HEADER = """\
//! Async fetch/parse free-function layer extracted from `app/mod.rs` (#743).
//!
//! Network I/O, SQLite reads, subprocess spawns, parse helpers.  No quadraui
//! rendering types appear here.
use std::net::{TcpStream, ToSocketAddrs};
use std::path::PathBuf;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use rusqlite::{Connection, OpenFlags};
use super::types::*;
#[allow(unused_imports)]
use super::format::*;

"""

# ── Brace-aware scanner ────────────────────────────────────────────────────────

def net_braces(line: str) -> int:
    """
    Count net { vs } in `line`, skipping:
      - // line comments
      - simple double-quoted strings (handles \\ and \")
      - single-quoted chars  'x'  (3-char form)
    Raw strings r#"..."# are handled naively (good enough for this codebase).
    """
    depth = 0
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        # line comment
        if c == '/' and i + 1 < n and line[i + 1] == '/':
            break
        # block comment start  /*  — scan to end-of-line or */
        if c == '/' and i + 1 < n and line[i + 1] == '*':
            end = line.find('*/', i + 2)
            i = end + 2 if end >= 0 else n
            continue
        # string literal
        if c == '"':
            i += 1
            while i < n:
                if line[i] == '\\':
                    i += 2
                    continue
                if line[i] == '"':
                    i += 1
                    break
                i += 1
            continue
        # char literal  'x' or '\n' etc.
        if c == '\'' and i + 2 < n and line[i + 2] == '\'':
            i += 3
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
        i += 1
    return depth


# ── Item-name extractor ───────────────────────────────────────────────────────

# Strip optional visibility prefix, returning the rest of the declaration line.
_VIS_RE = re.compile(
    r'^(pub\s*(?:\([^)]+\))?\s*)?(async\s+)?(unsafe\s+)?(extern\s+"[^"]+"\s+)?'
)

def item_name(keyword_line: str) -> str | None:
    """
    Return the name of the top-level Rust item declared on `keyword_line`,
    or None if the line doesn't look like a top-level item declaration.

    Rules:
      fn / struct / enum / const / static / type / trait / union  → next ident
      impl Trait for Type  → Type   (route by the concrete type)
      impl Type            → Type
    """
    line = keyword_line.strip()
    # strip visibility + async/unsafe/extern
    m = _VIS_RE.match(line)
    if m:
        line = line[m.end():]

    # impl … for Type
    m = re.match(r'^impl\b.*?\bfor\s+(\w+)', line)
    if m:
        return m.group(1)

    # impl Type
    m = re.match(r'^impl\s+(\w+)', line)
    if m:
        return m.group(1)

    # named item
    m = re.match(r'^(fn|struct|enum|const|static|type|trait|union)\s+(\w+)', line)
    if m:
        return m.group(2)

    return None


def route(name: str | None) -> str:
    """Return 'format', 'types', 'data', or 'mod' for item `name`."""
    if name is None:
        return 'mod'
    if name in FORMAT_NAMES:
        return 'format'
    if name in TYPES_NAMES:
        return 'types'
    if name in DATA_NAMES:
        return 'data'
    return 'mod'


# ── Visibility injector ───────────────────────────────────────────────────────

_KEYWORD_RE = re.compile(
    r'^(\s*)(pub\s*(?:\([^)]+\))?\s*)?(async\s+)?(unsafe\s+)?'
    r'(fn|struct|enum|const|static|type|trait|union|impl)\b'
)

def add_pub_crate(line: str) -> str:
    """
    Add `pub(crate) ` before the item keyword if there's no existing
    visibility specifier. Already-public items are left unchanged.
    impl blocks are never given a visibility qualifier (Rust forbids it).
    E.g.:
        'fn foo(' → 'pub(crate) fn foo('
        'pub fn foo(' → unchanged (already visible)
        'pub(crate) fn foo(' → unchanged
        'impl Foo {' → unchanged (impl cannot have visibility)
    """
    m = _KEYWORD_RE.match(line)
    if not m:
        return line
    indent = m.group(1)
    existing_vis = m.group(2)
    if existing_vis:
        # Already has visibility — don't add another
        return line
    keyword = m.group(5)
    if keyword == 'impl':
        # impl blocks cannot have visibility qualifiers in Rust
        return line
    # Insert pub(crate) before the keyword group
    # Preserve async/unsafe prefix if present
    async_part = m.group(3) or ''
    unsafe_part = m.group(4) or ''
    prefix = indent + 'pub(crate) ' + async_part + unsafe_part
    rest = line[m.start(5):]
    return prefix + rest


# ── Member-level visibility injector ─────────────────────────────────────────

_FIELD_NAME_RE = re.compile(r'^([a-zA-Z_][a-zA-Z0-9_]*)\s*:')
_IMPL_MEMBER_KEYWORD_RE = re.compile(
    r'^((?:pub\s*(?:\([^)]+\))?\s*)?)'
    r'(?:async\s+)?(?:unsafe\s+)?(?:extern\s+"[^"]+"\s+)?(fn|type|const)\b'
)

def pub_crate_struct_fields(item_text: str) -> str:
    """
    For items extracted to a new submodule (types.rs, data.rs), add
    `pub(crate)` to members that are not already marked public, so that
    sibling/parent modules can access them:

    - Named struct fields at depth=1 inside a `struct { }` body
    - Methods/associated items at depth=1 inside an `impl { }` body
    """
    lines = item_text.splitlines(keepends=True)
    if not lines:
        return item_text

    # Determine the outermost item kind: struct, impl, enum, fn, etc.
    outer_kind = None
    is_trait_impl = False
    for l in lines:
        s = l.strip()
        if not s or s.startswith('//') or s.startswith('#[') or s.startswith('///'):
            continue
        stripped_vis = re.sub(r'^pub\s*(?:\([^)]+\))?\s*', '', s)
        for kw in ('struct', 'impl', 'enum', 'fn', 'const', 'static', 'type', 'trait'):
            if stripped_vis.startswith(kw + ' ') or stripped_vis == kw:
                outer_kind = kw
                break
        if outer_kind == 'impl':
            # `impl Trait for Type` → trait impl, methods inherit trait visibility
            # `impl Type` → inherent impl, methods need explicit pub(crate)
            is_trait_impl = bool(re.search(r'\bfor\b', stripped_vis.split('{')[0]))
        break

    if outer_kind not in ('struct', 'impl'):
        return item_text
    if is_trait_impl:
        return item_text

    result = []
    depth = 0
    for l in lines:
        net = net_braces(l.rstrip('\n'))
        if depth == 1:
            s = l.strip()
            if s and not s.startswith('//') and not s.startswith('///') and not s.startswith('#['):
                if outer_kind == 'struct' and net == 0:
                    # Named field (no braces on the line): identifier followed by `:`
                    if not s.startswith('pub') and _FIELD_NAME_RE.match(s):
                        indent_len = len(l) - len(l.lstrip())
                        l = ' ' * indent_len + 'pub(crate) ' + s + '\n'
                elif outer_kind == 'impl':
                    # Method/assoc item keyword at depth=1: fn, type, const.
                    # net may be 0 (multi-line sig) or ≥1 (opens body inline).
                    m = _IMPL_MEMBER_KEYWORD_RE.match(s)
                    if m and not m.group(1).strip():
                        # No existing visibility — add pub(crate)
                        indent_len = len(l) - len(l.lstrip())
                        l = ' ' * indent_len + 'pub(crate) ' + s + '\n'
        result.append(l)
        depth += net

    return ''.join(result)


# ── Main parser ───────────────────────────────────────────────────────────────

def is_doc_or_attr(line: str) -> bool:
    """True for lines that are doc comments or #[...] attributes."""
    s = line.strip()
    return s.startswith('///') or s.startswith('//!') or s.startswith('#[') or s.startswith('#![')


def parse_mod(text: str):
    """
    Split mod.rs into four buckets:
      header   — everything up to (but not including) the first item we extract
                 (use stmts, module-level doc, top constants that stay)
      format   — items routed to format.rs
      types    — items routed to types.rs
      data     — items routed to data.rs
      mod      — items that stay in mod.rs (CoordApp, etc.) + section comments

    Returns: (header, format_items, types_items, data_items, mod_items)
    Each "items" list is a list of str (complete item text including prefix).
    `header` is a str.
    """
    lines = text.splitlines(keepends=True)

    buckets = {'format': [], 'types': [], 'data': [], 'mod': []}

    # Lines that form the "header" (use statements at top) — everything before
    # the first item that we recognise as top-level.
    header_lines = []
    header_done = False  # switches to True after we've seen the first const/fn/struct etc.

    # Pending prefix: doc comments + attributes accumulated before an item.
    pending_prefix: list[str] = []

    # Current item accumulation
    current_item: list[str] = []
    current_dest: str = 'mod'
    current_name: str | None = None
    depth = 0
    in_item = False
    # body_entered: True once depth has gone > 0 (i.e. we've seen the opening {).
    # We must NOT flush on depth==0 until the body has been entered, otherwise
    # multi-line function signatures (where clauses, split parameter lists) are
    # cut short before their opening brace.
    body_entered = False

    # Track whether the current_item's keyword line has been seen
    # (so we only add pub(crate) to the first keyword line)
    keyword_injected = False

    def flush_item():
        nonlocal current_item, current_dest, in_item, depth, keyword_injected, body_entered
        text_block = ''.join(current_item)
        buckets[current_dest].append(text_block)
        current_item = []
        current_dest = 'mod'
        in_item = False
        depth = 0
        body_entered = False
        keyword_injected = False

    def flush_prefix_to_mod():
        nonlocal pending_prefix
        for l in pending_prefix:
            buckets['mod'].append(l)
        pending_prefix = []

    i = 0
    while i < len(lines):
        line = lines[i]
        raw = line.rstrip('\n')

        if in_item:
            # ── Inside an item body ───────────────────────────────────────────
            current_item.append(line)
            depth += net_braces(raw)
            if depth > 0:
                body_entered = True
            # Close when depth returns to 0 after having entered the body ({...}).
            # body_entered guard prevents premature flush on multi-line signatures
            # (where clauses, split parameter lists) that have depth==0 before {.
            if body_entered and depth <= 0:
                flush_item()
            elif not body_entered and depth == 0 and raw.rstrip().endswith(';'):
                # Multi-line item with no brace body (e.g. `type Alias\n    = Bar;`).
                flush_item()
            i += 1
            continue

        # ── Between items ─────────────────────────────────────────────────────
        stripped = raw.strip()

        # Blank line
        if stripped == '':
            pending_prefix.append(line)
            i += 1
            continue

        # Doc comment or attribute → add to pending prefix
        if is_doc_or_attr(line):
            pending_prefix.append(line)
            i += 1
            continue

        # Try to parse as a top-level item keyword
        name = item_name(raw)
        if name is not None or re.match(r'^\s*(pub\s*(?:\([^)]+\))?\s*)?(async\s+)?(unsafe\s+)?(fn|struct|enum|const|static|type|trait|union|impl)\b', raw):
            dest = route(name)

            if not header_done:
                # Before the first extractable item, everything goes to header
                if dest in ('format', 'types', 'data'):
                    header_done = True
                    # Flush accumulated header
                    pass  # will be handled below
                else:
                    # This is a top-level item that stays in mod — it's still "header" area
                    # (things like initial constants NOTIFY_EVERY etc.)
                    # Accumulate into header_lines
                    for l in pending_prefix:
                        header_lines.append(l)
                    pending_prefix = []
                    # Now collect this item into header_lines
                    current_item = []
                    current_dest = 'mod'
                    d = net_braces(raw)
                    header_lines.append(line)
                    if d > 0:
                        depth = d
                        # collect body into header_lines
                        i += 1
                        while i < len(lines) and depth > 0:
                            l2 = lines[i]
                            header_lines.append(l2)
                            depth += net_braces(l2.rstrip('\n'))
                            i += 1
                        depth = 0
                    elif ';' in raw:
                        pass  # single-line
                        i += 1
                    else:
                        i += 1
                    continue

            # header_done from here
            if dest == 'mod':
                # Flush prefix to mod, then start collecting mod item
                flush_prefix_to_mod()
                # Start collecting this item for mod
                current_item = [line]
                current_dest = 'mod'
                depth = net_braces(raw)
                in_item = True
                keyword_injected = True  # no vis injection needed for mod items
                # Check if it's a one-liner
                if depth == 0 and (';' in raw or raw.strip().endswith('}')):
                    flush_item()
                    in_item = False
                i += 1
                continue

            # dest in (format, types, data)
            # The prefix belongs to this item
            item_lines = list(pending_prefix) + [add_pub_crate(line)]
            pending_prefix = []
            current_dest = dest
            current_item = item_lines
            keyword_injected = True
            depth = net_braces(raw)
            in_item = True
            if depth == 0 and (';' in raw or raw.strip().endswith('}')):
                flush_item()
                in_item = False
            i += 1
            continue

        # Not an item keyword, not blank, not doc/attr.
        # Could be: section header comment, `use` stmt, `mod` stmt, etc.
        # These go to header (if before header_done) or mod.
        if not header_done:
            for l in pending_prefix:
                header_lines.append(l)
            pending_prefix = []
            header_lines.append(line)
        else:
            flush_prefix_to_mod()
            buckets['mod'].append(line)
        i += 1

    # Flush any remaining pending
    flush_prefix_to_mod()
    if current_item:
        flush_item()

    header = ''.join(header_lines)
    return header, buckets['format'], buckets['types'], buckets['data'], buckets['mod']


# ── Mod declarations to insert ────────────────────────────────────────────────

MOD_DECLS = """\

pub(crate) mod types;
pub(crate) mod format;
pub(crate) mod data;
#[allow(unused_imports)]
use self::types::*;
#[allow(unused_imports)]
use self::format::*;
#[allow(unused_imports)]
use self::data::*;
"""


def insert_mod_decls(header: str) -> str:
    """
    Insert the mod/use declarations after the last top-level `use` statement
    in the header block (before the first `//` section comment or `const`).
    """
    lines = header.splitlines(keepends=True)
    # Find the last `use ` line
    last_use = -1
    for idx, l in enumerate(lines):
        if re.match(r'^use\b', l.strip()) or re.match(r'^pub use\b', l.strip()):
            last_use = idx
    if last_use >= 0:
        # Insert after the last use line
        insert_at = last_use + 1
    else:
        insert_at = len(lines)
    lines.insert(insert_at, MOD_DECLS)
    return ''.join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    text = MOD_RS.read_text()

    print(f"Parsing {MOD_RS} ({len(text.splitlines())} lines)…")
    header, fmt_items, typ_items, dat_items, mod_items = parse_mod(text)

    print(f"  format.rs:  {len(fmt_items)} items")
    print(f"  types.rs:   {len(typ_items)} items")
    print(f"  data.rs:    {len(dat_items)} items")
    print(f"  mod.rs rem: {len(mod_items)} items")

    # Apply pub(crate) to struct fields for items extracted to sibling modules
    typ_items = [pub_crate_struct_fields(it) for it in typ_items]
    dat_items = [pub_crate_struct_fields(it) for it in dat_items]

    # Build file contents
    format_content = FORMAT_HEADER + ''.join(fmt_items)
    types_content  = TYPES_HEADER  + ''.join(typ_items)
    data_content   = DATA_HEADER   + ''.join(dat_items)

    # Reassemble mod.rs: header (with mod decls injected) + mod items + mod tests;
    new_header = insert_mod_decls(header)
    # mod_items may already contain `mod tests;` at the very end — check
    mod_body = ''.join(mod_items)
    new_mod = new_header + mod_body

    # Remove imports from mod.rs that moved exclusively to data.rs:
    #   - std::net::{TcpStream, ToSocketAddrs}  (used by tcp_probe → data.rs)
    #   - OpenFlags from rusqlite (used by open_purge_conn → data.rs)
    new_mod = re.sub(
        r'^use std::net::\{TcpStream, ToSocketAddrs\};\n',
        '',
        new_mod,
        flags=re.MULTILINE,
    )
    new_mod = re.sub(
        r'\{Connection, OpenFlags\}',
        'Connection',
        new_mod,
    )

    if DRY_RUN:
        print("\n── DRY RUN — no files written ──")
        print(f"  format.rs would be {len(format_content.splitlines())} lines")
        print(f"  types.rs  would be {len(types_content.splitlines())} lines")
        print(f"  data.rs   would be {len(data_content.splitlines())} lines")
        print(f"  mod.rs    would be {len(new_mod.splitlines())} lines")
        # Show first few items in each bucket for spot-checking
        def show(label, items):
            print(f"\n  {label}:")
            for it in items[:5]:
                first_line = it.strip().splitlines()[0] if it.strip() else '(blank)'
                print(f"    {first_line[:80]}")
            if len(items) > 5:
                print(f"    … and {len(items)-5} more")
        show("format.rs items", fmt_items)
        show("types.rs items", typ_items)
        show("data.rs items", dat_items)
        return

    # Write files
    (BASE / 'format.rs').write_text(format_content)
    print(f"Wrote {BASE / 'format.rs'} ({len(format_content.splitlines())} lines)")

    (BASE / 'types.rs').write_text(types_content)
    print(f"Wrote {BASE / 'types.rs'} ({len(types_content.splitlines())} lines)")

    (BASE / 'data.rs').write_text(data_content)
    print(f"Wrote {BASE / 'data.rs'} ({len(data_content.splitlines())} lines)")

    MOD_RS.write_text(new_mod)
    print(f"Wrote {MOD_RS} ({len(new_mod.splitlines())} lines)")

    print("\nDone. Run `cargo build` in tui/ to verify.")


if __name__ == '__main__':
    main()
