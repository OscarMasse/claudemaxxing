# Platform seam

The engine (gate.py, lib/, the core of run.sh) is platform-agnostic: standard-library Python plus portable shell.
Everything OS-specific lives in one directory per platform, `platform/<os>/` (`macos` is the only adapter shipped).
Platform detection maps `uname -s` / `sys.platform` to the directory name (`Darwin`/`darwin` -> `macos`); the `ORCH_PLATFORM` env var overrides it.

A new OS directory must provide five executable hooks, plus whatever scheduler unit templates its own install script needs (macOS keeps its launchd plists in `macos/launchd/`):

- `install.sh`: no args; register the two scheduled units (a keep-alive loop running `gatekeeper-loop.sh`, and a daily calendar trigger running `digest-wrapper.sh`), substituting the backlog root into the unit templates; print any manual steps (e.g. scheduling a nightly wake). Idempotent.
- `uninstall.sh`: no args; unregister and remove those units. Idempotent.
- `keep-awake.sh`: exec the given command line (`"$@"`) while preventing idle system sleep; exit with the command's status. The portable layer runs the command directly if this hook is missing, so sleep prevention is best-effort.
- `kickstart-scheduler.sh`: no args; force-start the scheduler unit if the init system left it dead. Called by `digest-wrapper.sh` as a daily watchdog.
- `notify.sh`: args `<title> <message>`; show a user notification. Called by gate.py when a new question lands in NEEDS-HUMAN.md while the owner is at the machine; skipped silently if missing.

A Linux port would implement these five scripts on systemd user timers plus `systemd-inhibit` and `notify-send`; nothing in the engine needs to change.
