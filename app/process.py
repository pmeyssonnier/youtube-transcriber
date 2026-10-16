from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

# Intervalle de contrôle d'un sous-processus (secondes).
POLL_SECONDS = 1.0


class ProcessStalled(RuntimeError):
    """The process was stopped: too long overall (``kind="total"``) or no progress (``kind="idle"``)."""

    def __init__(self, kind: str, seconds: float, stdout: str = "", stderr: str = "") -> None:
        super().__init__(f"{kind}:{seconds:.0f}")
        self.kind = kind
        self.seconds = seconds
        self.stdout = stdout
        self.stderr = stderr


@dataclass
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


def watched_state(path: Path) -> tuple[int, int, float]:
    """(files, total bytes, latest change) under ``path`` : changes whenever a file is written."""
    files = size = 0
    newest = 0.0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        else:
                            info = entry.stat(follow_symlinks=False)
                            files += 1
                            size += info.st_size
                            newest = max(newest, info.st_mtime)
                    except OSError:
                        continue
        except OSError:
            continue
    return files, size, newest


def kill_tree(process: subprocess.Popen) -> None:
    """Stop the process and everything it started (yt-dlp starts FFmpeg)."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True, check=False, timeout=15,
            )
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        process.kill()
    except OSError:
        pass


def run_supervised(
    command: list[str],
    *,
    timeout: float | None = None,
    idle_timeout: float | None = None,
    watch: Path | None = None,
    poll: float | None = None,
) -> ProcessResult:
    """Run a command and stop it if it exceeds ``timeout`` or shows no progress.

    Progress means a change in the files under ``watch`` (a download or conversion that is running
    keeps writing). Without ``watch`` only the overall ``timeout`` applies.
    """
    poll = POLL_SECONDS if poll is None else poll
    options: dict = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        **options,
    )
    chunks: dict[str, list[str]] = {"out": [], "err": []}

    def drain(stream, key: str) -> None:
        for line in iter(stream.readline, ""):
            chunks[key].append(line)
        stream.close()

    readers = [
        threading.Thread(target=drain, args=(process.stdout, "out"), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, "err"), daemon=True),
    ]
    for reader in readers:
        reader.start()

    started = last_change = time.monotonic()
    state = watched_state(watch) if watch is not None and idle_timeout else None
    stalled: ProcessStalled | None = None
    while process.poll() is None:
        time.sleep(poll)
        now = time.monotonic()
        if state is not None:
            current = watched_state(watch)
            if current != state:
                state, last_change = current, now
        if timeout is not None and now - started > timeout:
            stalled = ProcessStalled("total", now - started)
        elif state is not None and now - last_change > idle_timeout:
            stalled = ProcessStalled("idle", now - last_change)
        if stalled is not None:
            kill_tree(process)
            break
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        kill_tree(process)
    for reader in readers:
        reader.join(timeout=5)
    stdout, stderr = "".join(chunks["out"]), "".join(chunks["err"])
    if stalled is not None:
        stalled.stdout, stalled.stderr = stdout, stderr
        raise stalled
    return ProcessResult(process.returncode, stdout, stderr)
