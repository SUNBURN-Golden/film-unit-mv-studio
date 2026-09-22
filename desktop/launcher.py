"""Small native launcher for the existing local browser-based control panel."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
import webbrowser

from desktop.runtime import configure_runtime, resource_root


def child_command(*args):
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, "-m", "desktop.launcher", *args]


class Server:
    def __init__(self, root):
        self.root, self.process, self.job, self.log = Path(root), None, None, None
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.log_path = self.root / "logs" / f"server-{os.getpid()}.log"
        self.gate = self.root / "cache" / ("start-" + uuid.uuid4().hex)

    def start(self):
        if self.process:
            raise RuntimeError("Server already started")
        self.log = self.log_path.open("ab", buffering=0)
        if os.name == "nt":
            from desktop.windows_job import Job
            self.job = Job()
        try:
            self.process = subprocess.Popen(
                child_command("--serve", "--port", str(self.port), "--start-gate", str(self.gate), "--server-log", str(self.log_path)),
                stdout=self.log, stderr=self.log, cwd=resource_root(),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
            # The child waits for this handshake, so it cannot spawn an encoder
            # before being attached to the parent's kill-on-close Windows job.
            if self.job:
                self.job.assign(self.process)
            self.gate.write_text("START", encoding="ascii")
        except Exception:
            self.stop()
            raise

    def ready(self):
        if not self.process or self.process.poll() is not None:
            return False
        try:
            # Local traffic must not go through a configured corporate proxy.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(self.url + "/_stcore/health", timeout=0.3) as response:
                return response.status == 200 and response.read().strip() == b"ok"
        except OSError:
            return False

    def stop(self):
        if self.job:
            self.job.close()
            self.job = None
        if self.process:
            if self.process.poll() is None:
                if os.name == "nt":
                    self.process.terminate()
                else:
                    os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    os.killpg(self.process.pid, signal.SIGKILL)
                else:
                    self.process.kill()
                self.process.wait(timeout=5)
            self.process = None
        if self.log:
            self.log.close()
            self.log = None
        self.gate.unlink(missing_ok=True)


def serve(port, gate):
    # Windowed frozen Python has no stdin, even with a redirected OS handle.
    # Wait for the parent's post-job-assignment signal before starting workers.
    if not gate:
        return 1
    path = Path(gate)
    deadline = time.monotonic() + 15
    while not path.is_file():
        if time.monotonic() > deadline:
            return 1
        time.sleep(0.1)
    path.unlink()
    from streamlit.web import cli
    sys.argv = ["streamlit", "run", str(resource_root() / "app/control_panel.py"),
                "--server.address=127.0.0.1", f"--server.port={port}",
                "--server.headless=true", "--server.fileWatcherType=none",
                "--browser.gatherUsageStats=false", "--global.developmentMode=false"]
    cli.main()
    return 0


def open_folder(path):
    if os.name == "nt":
        os.startfile(str(path))
    else:
        webbrowser.open(Path(path).as_uri())


def gui(root, smoke_report=None):
    import tkinter as tk
    from tkinter import messagebox, ttk
    import queue
    import threading
    from desktop.ffmpeg_setup import ensure_ffmpeg, is_ready

    window = tk.Tk()
    window.title("FILM UNIT · MV Compiler")
    window.geometry("540x335")
    window.resizable(False, False)
    window.configure(background="#eeeae3")
    frame = ttk.Frame(window, padding=25)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="FILM UNIT", font=("Segoe UI", 24, "bold")).pack(anchor="w")
    ttk.Label(frame, text="음원 · 콘티 · 가사 → Music Video", font=("Malgun Gothic", 11)).pack(anchor="w", pady=(4, 14))
    status = tk.StringVar(value="로컬 편집 화면을 준비하고 있습니다…")
    ttk.Label(frame, textvariable=status, wraplength=485).pack(anchor="w", pady=6)
    server = Server(root)
    opened, deadline = False, time.monotonic() + 150
    open_button = ttk.Button(frame, text="편집 화면 열기", command=lambda: webbrowser.open(server.url), state="disabled")
    open_button.pack(fill="x", pady=4)
    ttk.Button(frame, text="프로젝트 폴더 열기", command=lambda: open_folder(Path(os.environ["FILM_UNIT_PROJECTS"]))).pack(fill="x", pady=4)
    ttk.Button(frame, text="로그 폴더 열기", command=lambda: open_folder(root / "logs")).pack(fill="x", pady=4)
    events = queue.Queue()
    installing = False
    failed = False

    def close(force=False):
        if not force and server.process and not messagebox.askokcancel("FILM UNIT 종료", "진행 중인 분석·렌더도 중단됩니다. 종료할까요?"):
            return
        server.stop()
        window.destroy()

    ttk.Button(frame, text="프로그램 종료", command=close).pack(fill="x", pady=4)
    window.protocol("WM_DELETE_WINDOW", close)

    def start():
        nonlocal failed
        try:
            server.start()
        except Exception as exc:
            failed = True
            status.set(f"실행 실패: {exc}\n로그: {server.log_path}")
            if smoke_report:
                window.after(0, lambda: close(force=True))

    if is_ready():
        start()
    elif smoke_report:
        window.destroy()
        raise RuntimeError("GUI smoke requires installed FFmpeg")
    elif messagebox.askyesno("첫 실행 준비", "영상 편집 엔진 FFmpeg를 Gyan.dev에서 내려받을까요?\n최초 한 번 인터넷이 필요합니다. API·구독 결제는 없습니다."):
        installing = True
        def install():
            try:
                ensure_ffmpeg(root, progress=lambda message: events.put(("status", message)))
                events.put(("installed", None))
            except Exception as exc:
                events.put(("error", str(exc)))
        threading.Thread(target=install, daemon=True).start()
    else:
        failed = True
        status.set("FFmpeg 준비가 취소됐습니다. 프로그램을 다시 열어 설치할 수 있습니다.")

    def check():
        nonlocal opened, installing, failed, deadline
        while not events.empty():
            kind, value = events.get_nowait()
            if kind == "status":
                status.set(value)
            elif kind == "error":
                failed, installing = True, False
                status.set(f"준비 실패: {value}")
            else:
                installing = False
                configure_runtime()
                deadline = time.monotonic() + 150
                start()
        if not failed and not installing and not opened and server.ready():
            opened = True
            status.set("실행 중 · 이 창을 닫으면 편집 서버도 종료됩니다.")
            open_button.configure(state="normal")
            if smoke_report:
                pid = server.process.pid
                server.stop()
                Path(smoke_report).write_text(json.dumps({"gui_created": True, "health": "ok", "server_pid": pid, "server_stopped": True}), encoding="utf-8")
                window.after(100, lambda: close(force=True))
                return
            webbrowser.open(server.url)
        if not failed and not installing and not opened:
            if (server.process and server.process.poll() is not None) or time.monotonic() > deadline:
                failed = True
                status.set(f"편집 화면을 시작하지 못했습니다.\n로그: {server.log_path}")
                if smoke_report:
                    close(force=True)
                    raise RuntimeError("Streamlit startup failed")
        window.after(300, check)
    window.after(200, check)
    try:
        window.mainloop()
    finally:
        server.stop()
    if smoke_report and not Path(smoke_report).is_file():
        return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="FILM UNIT desktop launcher")
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=8501, help=argparse.SUPPRESS)
    parser.add_argument("--start-gate", help=argparse.SUPPRESS)
    parser.add_argument("--server-log", help=argparse.SUPPRESS)
    parser.add_argument("--self-test", metavar="DIRECTORY")
    parser.add_argument("--smoke-gui", metavar="REPORT")
    args = parser.parse_args(argv)
    root = configure_runtime()
    # Windowed frozen executables have no console streams. Streamlit and native
    # dependencies still need writable stdout/stderr for logs.
    if sys.stdout is None or sys.stderr is None:
        log_path = Path(args.server_log) if args.serve and args.server_log else root / "logs" / f"launcher-{os.getpid()}.log"
        stream = log_path.open("a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = stream
    if args.serve:
        return serve(args.port, args.start_gate)
    if args.self_test:
        from desktop.smoke import run
        run(Path(args.self_test))
        return 0
    return gui(root, args.smoke_gui)


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    raise SystemExit(main())
