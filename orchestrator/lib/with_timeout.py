#!/usr/bin/env python3
"""Run a command with a hard wall-clock timeout, killing the whole process group.

Usage: with_timeout.py <seconds> -- <cmd> [args...]
Exits with the command's code, or 124 on timeout (GNU timeout convention).
"""
import os, signal, subprocess, sys


def main():
    seconds = int(sys.argv[1])
    assert sys.argv[2] == "--"
    cmd = sys.argv[3:]
    proc = subprocess.Popen(cmd, start_new_session=True)
    try:
        sys.exit(proc.wait(timeout=seconds))
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        sys.exit(124)


if __name__ == "__main__":
    main()
