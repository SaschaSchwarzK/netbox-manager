"""Run the API and Caddy in one minimal, shell-free container."""
from __future__ import annotations

import signal
import subprocess
import sys
import time


processes: list[subprocess.Popen] = []
stopping = False


def stop_processes(*_args) -> None:
    global stopping
    if stopping:
        return
    stopping = True
    for process in processes:
        if process.poll() is None:
            process.terminate()


def main() -> int:
    signal.signal(signal.SIGTERM, stop_processes)
    signal.signal(signal.SIGINT, stop_processes)

    processes.extend(
        [
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "app.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8000",
                    "--no-access-log",
                ]
            ),
            subprocess.Popen(
                [
                    "/usr/bin/caddy",
                    "run",
                    "--config",
                    "/app/Caddyfile",
                    "--adapter",
                    "caddyfile",
                ]
            ),
        ]
    )

    exit_code = 0
    while not stopping:
        for process in processes:
            result = process.poll()
            if result is not None:
                exit_code = result or 1
                stop_processes()
                break
        time.sleep(0.25)

    deadline = time.monotonic() + 10
    for process in processes:
        if process.poll() is None:
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
