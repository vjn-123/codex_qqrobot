#!/usr/bin/env python3
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "data"
LOG_FILE = LOG_DIR / "qq-gateway-autostart.log"
OLD_LOG_FILE = LOG_DIR / "qq-gateway-autostart.log.1"
MAX_LOG_BYTES = 5 * 1024 * 1024
RESTART_DELAY_SECONDS = 10


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def rotate_log() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if LOG_FILE.exists() and LOG_FILE.stat().st_size > MAX_LOG_BYTES:
        if OLD_LOG_FILE.exists():
            OLD_LOG_FILE.unlink()
        LOG_FILE.replace(OLD_LOG_FILE)


def python_exe() -> str:
    current = Path(sys.executable)
    if current.name.lower() == "pythonw.exe":
        sibling = current.with_name("python.exe")
        if sibling.exists():
            return str(sibling)
    return str(current)


def write_line(text: str) -> None:
    rotate_log()
    with LOG_FILE.open("a", encoding="utf-8", errors="replace") as log:
        log.write(text.rstrip() + "\n")
        log.flush()


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    while True:
        rotate_log()
        try:
            with LOG_FILE.open("a", encoding="utf-8", errors="replace") as log:
                log.write(f"[{now_text()}] supervisor starting QQ Gateway bridge\n")
                log.flush()
                proc = subprocess.Popen(
                    [python_exe(), str(BASE_DIR / "qq_gateway_client.py")],
                    cwd=str(BASE_DIR),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    env=env,
                    creationflags=creationflags,
                )
                return_code = proc.wait()
                log.write(
                    f"[{now_text()}] QQ Gateway exited code={return_code}; "
                    f"restart in {RESTART_DELAY_SECONDS}s\n"
                )
                log.flush()
        except Exception:
            write_line(f"[{now_text()}] supervisor error:\n{traceback.format_exc().rstrip()}")

        time.sleep(RESTART_DELAY_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
