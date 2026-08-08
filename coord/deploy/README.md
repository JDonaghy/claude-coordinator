# `coord/deploy/` — the packaged systemd units (#1927)

**This directory is a byte-identical copy of the repo-root `deploy/` unit
files, and it is the copy that ships in the wheel.**

Edit `deploy/<name>` at the repo root — that is the reviewed source of
truth — then copy it here:

```
cp deploy/*.service deploy/*.timer coord/deploy/
```

`tests/test_packaged_deploy_units.py` fails if the two ever disagree, so the
copy cannot drift silently.

## Why the copy exists

`coord release verify`'s unit-drift check
(`coord/health/checks/unit_drift.py`, #1831) diffs each host's *installed*
unit under `~/.config/systemd/user/` against a reference. Until #1927 that
reference was `<checkout>/deploy/<name>` — a file in the host's own git
working copy, which nothing verifies is current.

Installed units and checkouts go stale for the same reason (nobody pulled),
so they go stale *together*, and the diff then reports clean — the check was
least reliable in exactly the #1831 case it exists to catch, and its printed
remedy (`cp <checkout>/deploy/... ~/.config/systemd/user/...`) cemented the
stale unit.

Files under `coord/` ship in the wheel, so on a pip-installed host this
directory is the unit set *as of the installed version*. It cannot drift
with the host. The `*.sh` helpers in the repo-root `deploy/` are not copied
here: the drift check only reads `*.service`/`*.timer`.
