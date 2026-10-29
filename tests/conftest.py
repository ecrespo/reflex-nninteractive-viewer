"""Fixtures for the browser checks: a running demo app and a WebGL2-capable headless Chromium."""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

DEMO_DIR = Path(__file__).resolve().parents[1] / "nni_viewer_demo"
START_TIMEOUT = float(os.environ.get("NNI_DEMO_START_TIMEOUT", "600"))


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args):
    """Software WebGL2 (SwiftShader) so headless Chromium can run the viewer without a GPU."""
    args = [*browser_type_launch_args.get("args", [])]
    args += ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]
    return {**browser_type_launch_args, "args": args}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serving(url: str) -> bool:
    try:
        with urllib.request.urlopen(url + "/api/patch/phantom", timeout=2) as r:  # noqa: S310
            return r.status in (200, 204)
    except OSError:
        return False


@pytest.fixture(scope="session")
def demo_url(tmp_path_factory):
    """The demo: ``NNI_DEMO_URL`` if set, else ``reflex run`` in production mode on a free port."""
    if url := os.environ.get("NNI_DEMO_URL"):
        if not _serving(url.rstrip("/")):
            pytest.skip(f"NNI_DEMO_URL={url} does not serve the demo")
        yield url.rstrip("/")
        return
    port = _free_port()
    # A private copy: `reflex run` rewrites the app's .web/ and .data/, which would break a
    # demo the developer is running from the same folder.
    app_dir = tmp_path_factory.mktemp("demo") / "nni_viewer_demo"
    shutil.copytree(
        DEMO_DIR,
        app_dir,
        ignore=shutil.ignore_patterns(".web", ".states", ".data", "assets", "reflex.lock", "*.log"),
    )
    log = open(app_dir / "reflex-test.log", "w")  # noqa: SIM115
    proc = subprocess.Popen(
        [
            str(Path(sys.executable).with_name("reflex")),  # this environment's reflex
            "run",
            "--env",
            "prod",
            "--single-port",
            "--frontend-port",
            str(port),
        ],
        cwd=app_dir,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + START_TIMEOUT
        while not _serving(url):
            if proc.poll() is not None or time.monotonic() > deadline:
                pytest.skip(f"the demo app did not start (see {log.name})")
            time.sleep(2)
        yield url
    finally:
        try:
            os.killpg(proc.pid, signal.SIGINT)
            proc.wait(timeout=30)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            os.killpg(proc.pid, signal.SIGKILL)
        log.close()
