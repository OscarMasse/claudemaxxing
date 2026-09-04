#!/bin/bash
# macOS adapter: force-start the scheduler unit if launchd left it dead
# (KeepAlive should restart it, but "pended spawn" limbo has been observed).
set -u
exec launchctl kickstart "gui/$(id -u)/local.backlog.gatekeeper"
