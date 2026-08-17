# A WSL2 box as the Windows worker — for quadraui/vimcode, not for coord

> **Status:** proposal, not yet provisioned (2026-08-17). Captures a plan discussed for using an
> existing Tailscale-connected Windows machine with WSL2 as the fleet's Windows build/test worker
> for **quadraui's Win-GUI backend** (milestone #3, issues #19–31) and **vimcode's Win-GUI issues**
> (#160, #162, #164, #165, #176) — not for porting code-coordinator itself to Windows. That's a
> different, much larger effort scoped in [`CROSS_PLATFORM.md`](CROSS_PLATFORM.md) and explicitly
> **out of scope here** — there is no interest in running `coord` natively on Windows.

## The core idea

`coord agent` runs **inside WSL2** as an ordinary Linux process — WSL2 is a real Linux VM with its
own kernel, so none of `coord/agent.py`'s POSIX assumptions (tmux, `os.killpg`, `pty.fork`) need to
change. As far as the coordinator is concerned this machine is just another Linux box, same shape
as precision/elitebook/dellserver.

The only thing that's different: when a worker needs to build/test something gated behind quadraui's
`win` Cargo feature (real Direct2D/DirectWrite/Win32/COM), it shells out to the **Windows-native**
toolchain instead of the Linux one. WSL2's interop lets a Linux process exec Windows `.exe`s
directly (they're on `$PATH`), so this is just:

```bash
cargo.exe build --target x86_64-pc-windows-msvc --features win
```

run from a bash tool call inside the same WSL2 session — no separate machine, no RDP, no manual
hand-off.

> `CROSS_PLATFORM.md` rejected WSL2 for a different question — running the whole *coordinator*
> Windows-native for one operator's own laptop. This is not that: it's one more fleet worker, and
> its POSIX side is genuinely just Linux.

## Setup checklist

1. **Windows side:** rustup + MSVC Build Tools + Windows SDK (needed for `link.exe`/`rc.exe` —
   Direct2D/DirectWrite/COM need the real toolchain, not a WSL2 cross-compile).
2. **Checkout location:** clone `quadraui`/`vimcode` onto the Windows-native drive, e.g.
   `C:\src\quadraui` (visible from WSL2 as `/mnt/c/src/quadraui`). `coord agent`'s git/Python ops
   run fine against that path from the Linux side; `cargo.exe` gets native NTFS speed from the
   Windows side — no `\\wsl$` cross-VM I/O penalty either direction.
3. **Networking — Tailscale *inside* WSL2, not host port-forwarding.** Install the Tailscale Linux
   client inside the WSL2 distro and `tailscale up` there. It gets its own tailnet IP on WSL2's own
   network namespace; `coord agent` binds directly to it on `:7433`. This avoids `netsh interface
   portproxy` + Windows Firewall rules entirely — Tailscale's interface isn't intercepted by the
   Windows Firewall, and portproxy rules are fragile anyway (WSL2's internal IP changes across
   restarts unless pinned). Check `ls /dev/net/tun` first; `sudo modprobe tun` if it's missing.
4. **Keep it "online":** WSL2 shuts its VM down when idle. Enable systemd (`/etc/wsl.conf` →
   `[boot] systemd=true`) and run `coord agent` as a systemd service, plus a Windows Task Scheduler
   entry that launches the distro at logon/boot — otherwise the fleet will see this machine flicker
   offline.
5. **Register in `coordinator.yml`** with a new `win` capability (mirrors the existing `gtk`
   build-capability pattern) once the box is actually reachable, and add a
   `smoke_tests.capability_rules` entry routing `src/win/` → `requires: [win]`.

## Ceiling — what this does and doesn't prove

This gets real compile-truth plus headless testing via #24's offscreen `ID2D1Bitmap` surface (no
visible window needed) — the same tier that let quadraui's macOS backend get built and merged
without a Mac in the fleet, verified through CI instead. It does **not** get you interactive/visual
smoke — real window behavior, DPI scaling, mouse feel — which still needs eyes on the actual Windows
desktop (RDP or physical access). Same distinction the fleet already draws between `gtk` (build) and
a hypothetical `gtk-display`; add a `win-display` capability later only if that's ever actually
needed.

## Payoff

Unblocks quadraui's Win-GUI milestone (#19 bootstrap → #20 events → #21 text → #23/#24 services +
headless surface → #25–30 rasterisers → #31 examples) and vimcode's already-filed, currently-stalled
Win-GUI issues (#160, #162, #164, #165, #176) — the second set has been waiting on exactly this.

## Related

- [`CROSS_PLATFORM.md`](CROSS_PLATFORM.md) — the *coord-itself* Windows port (out of scope here);
  its §9 already anticipates `os:windows`/`os:macos` capability routing.
- [`MAC_MINI.md`](MAC_MINI.md) — the same "add a real-OS worker to the fleet" playbook, run for
  macOS; useful as a structural template (provisioning order, capability staging, PATH gotchas)
  even though the mechanics differ (no interop trick needed there — it's native macOS).
