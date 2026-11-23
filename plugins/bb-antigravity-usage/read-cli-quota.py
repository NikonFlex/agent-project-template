"""Read Antigravity's status-line quota without submitting a model prompt."""

import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shlex
import signal
import struct
import sys
import tempfile
import termios
import time

LAUNCHER = os.environ.get("BB_AGY_LAUNCHER", "agy")


def record_quota(destination):
    quota = json.load(sys.stdin).get("quota")
    if isinstance(quota, dict) and quota:
        temporary = destination.with_suffix(".tmp")
        fields = ("remaining_fraction", "reset_time", "disabled")
        snapshot = {
            key: {field: bucket[field] for field in fields if field in bucket}
            for key, bucket in quota.items() if isinstance(bucket, dict)
        }
        temporary.write_text(json.dumps(snapshot), encoding="utf-8")
        temporary.replace(destination)
    print("Antigravity quota")


def stop_terminal(pid, terminal):
    os.close(terminal)
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        if os.waitpid(pid, os.WNOHANG)[0]:
            return
        time.sleep(0.05)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    os.waitpid(pid, 0)


def read_quota():
    with tempfile.TemporaryDirectory(prefix="bb-antigravity-quota-") as directory:
        destination = Path(directory, "quota.json")
        command = shlex.join([sys.executable, str(Path(__file__).resolve()), "--record", str(destination)])
        settings = json.dumps({"statusLine": {"type": "command", "enabled": True, "command": command}})
        pid, terminal = pty.fork()
        if pid == 0:
            os.environ["TERM"] = "xterm-256color"
            os.chdir(directory)
            os.execl(LAUNCHER, LAUNCHER, "--settings", settings)

        snapshot = ""
        last_change = 0.0
        try:
            fcntl.ioctl(terminal, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 100, 0, 0))
            deadline = time.monotonic() + 9
            while time.monotonic() < deadline:
                readable, _, _ = select.select([terminal], [], [], 0.15)
                if readable:
                    try:
                        if not os.read(terminal, 65536):
                            break
                    except OSError:
                        break
                if destination.exists():
                    current = destination.read_text(encoding="utf-8")
                    if current != snapshot:
                        snapshot = current
                        last_change = time.monotonic()
                    elif time.monotonic() - last_change >= 1:
                        break
        finally:
            stop_terminal(pid, terminal)
        return snapshot or "{}"


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--record":
        record_quota(Path(sys.argv[2]))
    else:
        print(read_quota())
