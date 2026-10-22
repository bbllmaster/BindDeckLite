"""Desktop shell for the BindDeckLite host.

The console is still a Flask app served on 127.0.0.1, so "minimise to tray"
means: keep the server alive in the background behind a tray icon instead of
a taskbar window.  This module owns that shell, plus the *real* desktop window:

* ``run_webview`` — the frozen exe is a native pywebview window rendering the
  existing Web UI (no console, no browser popup).  Flask runs in a background
  thread; pywebview creates the window on the main thread and
  ``webview.start()`` blocks until it closes.  With a tray available the window
  minimises to the tray instead of closing.
* ``run_desktop`` — the legacy tray-only mode (no window): Flask in the
  background, a tray icon, and a browser tab opened on demand.  Still available
  for direct invocation on headless boxes where no GUI is wanted.
* ``_serve_headless`` — the fallback ``run_webview`` uses when pywebview or its
  GUI backend is unavailable: keeps the already-running Flask server alive in
  the tray (or blocks until /api/quit / Ctrl+C when there is no tray).
* native file dialogs used by the "浏览…" button in the web UI,
* ``/api/quit`` support so the web UI can stop the app when there is no tray,
* single-instance behaviour (a second double-click focuses the running app).

Nothing here is imported at module import time except the standard library, so
``run.sh`` on a headless Linux box keeps working without pystray, Pillow or
pywebview.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

STATE = {
    "desktop": False,
    "tray_active": False,
    "url": "",
    "error": "",
    "window_hidden": False,  # True while the native window is hidden (minimized w/o tray)
}

CREATE_NO_WINDOW = 0x08000000 if sys.platform.startswith("win") else 0
BROWSE_TIMEOUT = 120  # seconds the dialog child may stay open

_quit_callback = None

# Mutable holder for the native pywebview window, so the web UI can restore
# it (``/api/show-window``) when the tray is unavailable and the user clicked X
# with "minimize to tray" checked.  Cleared on shutdown.
_window_ref = [None]


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> str:
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def open_url(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception as e:  # noqa: BLE001
        STATE["error"] = f"{type(e).__name__}: {e}"


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        return s.connect_ex((host, port)) == 0


def wait_until_ready(url: str, timeout: float = 5.0) -> bool:
    """Poll the API until Flask answers, instead of sleeping a fixed time."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/api/status", timeout=0.5) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.1)
    return False


def tray_available() -> tuple[bool, str]:
    """Whether a tray icon can be created at all (pystray + Pillow present)."""
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    return True, ""


def hide_console() -> None:
    """Hide the console window of a frozen Windows build (no-op elsewhere)."""
    if not (is_frozen() and sys.platform.startswith("win")):
        return
    try:
        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass


def show_console() -> None:
    """Bring the console back when the tray failed after we hid it."""
    if not (is_frozen() and sys.platform.startswith("win")):
        return
    try:
        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 5)  # SW_SHOW
    except Exception:
        pass


def set_quit_callback(callback) -> None:
    global _quit_callback
    _quit_callback = callback


def request_quit() -> bool:
    """Shut the desktop app down from the web UI (used when no tray exists)."""
    callback = _quit_callback
    if callback is None:
        return False

    def _run():
        time.sleep(0.4)  # let the HTTP response reach the browser first
        try:
            callback()
        except Exception:
            pass

    threading.Thread(target=_run, name="quit", daemon=True).start()
    return True


def setup_stdio(log_path: str) -> None:
    """A windowed exe has no stdout; send prints/logs to a file instead."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        stream = open(log_path, "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = stream
        if sys.stderr is None:
            sys.stderr = stream
    except Exception:
        pass


def show_window() -> bool:
    """Restore the native window from the web UI (recovery path).

    Used when the tray is unavailable and the user clicked X with "minimize to
    tray" checked: the window was hidden instead of closed, the process keeps
    serving, and this is how the user brings the window back.
    """
    win = _window_ref[0]
    if win is None:
        return False
    try:
        win.show()
        STATE["window_hidden"] = False
        return True
    except Exception:
        return False


# --- native file dialog ---------------------------------------------------
def _dialog_command(kind: str) -> list[str]:
    if is_frozen():
        return [sys.executable, "--file-dialog", kind]
    here = os.path.dirname(os.path.abspath(__file__))
    return [sys.executable, os.path.join(here, "app.py"), "--file-dialog", kind]


def dialog_main(kind: str) -> int:
    """Entry point of the short-lived child process that owns the dialog."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "path": "",
                          "error": f"当前 Python 未包含 tkinter，无法打开文件对话框：{e}"}))
        return 2

    root = None
    path = ""
    try:
        root = tk.Tk()
        root.withdraw()
        root.title("BindDeckLite")
        try:
            root.attributes("-topmost", True)
            root.update()
        except Exception:
            pass
        if kind == "dir":
            path = filedialog.askdirectory(parent=root, title="选择文件夹", mustexist=True)
        else:
            path = filedialog.askopenfilename(
                parent=root,
                title="选择要启动的程序或文件",
                filetypes=[
                    ("程序 / 快捷方式", "*.exe *.lnk *.bat *.cmd *.com *.msi"),
                    ("所有文件", "*.*"),
                ],
            )
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "path": "", "error": f"文件对话框失败：{e}"}))
        return 3
    finally:
        try:
            if root is not None:
                root.destroy()
        except Exception:
            pass

    print(json.dumps({"ok": True, "path": path or ""}))
    return 0


def browse(kind: str = "file") -> dict:
    """Open a native dialog in a child process and return the picked path.

    A child process is used because Tk must own the main thread on macOS and
    because the frozen exe can re-enter itself with ``--file-dialog``.
    """
    if kind not in ("file", "dir"):
        kind = "file"
    try:
        proc = subprocess.run(
            _dialog_command(kind),
            capture_output=True,
            text=True,
            timeout=BROWSE_TIMEOUT,
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "path": "",
                "error": f"文件对话框 {BROWSE_TIMEOUT} 秒无操作，已取消"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "path": "", "error": f"{type(e).__name__}: {e}"}

    payload = {}
    for line in reversed((proc.stdout or "").strip().splitlines()):
        try:
            payload = json.loads(line)
            break
        except ValueError:
            continue
    if not isinstance(payload, dict):
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        detail = detail[-1] if detail else "无输出"
        return {"ok": False, "path": "",
                "error": f"文件对话框不可用：{detail}（退出码 {proc.returncode}）"}
    if not payload.get("ok"):
        err = payload.get("error") or "文件对话框不可用"
        if proc.returncode not in (0, None):
            err += f"（退出码 {proc.returncode}）"
        return {"ok": False, "path": "", "error": err}
    return {"ok": True, "path": payload.get("path") or ""}


# --- tray -----------------------------------------------------------------
def _icon_image(size: int = 64):
    from PIL import Image, ImageDraw

    path = os.path.join(resource_dir(), "assets", "app.ico")
    if os.path.exists(path):
        try:
            img = Image.open(path).convert("RGBA")
            return img.resize((size, size), Image.LANCZOS)
        except Exception:
            pass

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([2, 2, size - 3, size - 3], radius=14, fill=(53, 224, 194, 255))
    draw.rectangle([10, 10, 54, 26], fill=(9, 24, 32, 255))
    draw.rectangle([10, 30, 38, 54], fill=(9, 24, 32, 255))
    draw.rectangle([42, 30, 54, 54], fill=(9, 24, 32, 255))
    return img


def _build_tray(url: str, on_quit, window=None):
    """Build the pystray icon.

    ``window`` is the native pywebview window when running in windowed mode;
    the "显示窗口" item then raises it instead of opening a browser tab.
    """
    import pystray

    def show(_icon=None, _item=None):
        if window is not None:
            try:
                window.show()
                STATE["window_hidden"] = False
                return
            except Exception:
                pass
        open_url(url)

    def quit_app(_icon=None, _item=None):
        on_quit()

    menu = pystray.Menu(
        pystray.MenuItem("显示窗口", show, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出 BindDeckLite", quit_app),
    )
    return pystray.Icon("BindDeckLite", _icon_image(), "BindDeckLite · 控制台", menu)


def _serve_forever(thread) -> None:
    """Block until the server stops (via /api/quit) or Ctrl+C."""
    try:
        thread.join()
    except KeyboardInterrupt:
        pass


def run_desktop(flask_app, port: int, url: str, start_minimized: bool = False,
                use_tray: bool = True, log_path: str = "", server=None) -> int:
    """Serve the app in the background and live in the tray until 退出.

    When ``server`` is supplied it is reused instead of binding a new socket,
    so a caller that already started Flask can hand the running server over
    without fighting for the port.
    """
    STATE["desktop"] = True
    STATE["url"] = url
    if log_path:
        setup_stdio(log_path)

    if server is None and port_in_use(port):
        print(f"[desktop] already running at {url}, opening browser")
        open_url(url)
        return 0

    if server is None:
        from werkzeug.serving import make_server
        try:
            server = make_server("127.0.0.1", port, flask_app, threaded=True)
        except OSError as e:
            STATE["error"] = str(e)
            show_console()
            print("[desktop] cannot bind port:", e)
            return 1

    icon = None

    def stop_all():
        nonlocal icon
        try:
            if icon is not None:
                icon.stop()
        finally:
            server.shutdown()

    thread = threading.Thread(target=server.serve_forever, name="flask", daemon=True)
    thread.start()
    set_quit_callback(stop_all)

    tray_ok, tray_error = tray_available() if use_tray else (False, "托盘已禁用")
    if use_tray and not tray_ok:
        STATE["error"] = "托盘不可用：" + tray_error
        print("[desktop] pystray/Pillow unavailable, falling back:", tray_error)
    use_tray = use_tray and tray_ok

    if use_tray:
        hide_console()

    if not start_minimized:
        wait_until_ready(url)
        open_url(url)

    if not use_tray:
        print("[desktop] running without tray; use 退出上位机 in the UI or Ctrl+C")
        _serve_forever(thread)
        server.shutdown()
        set_quit_callback(None)
        return 0

    try:
        icon = _build_tray(url, stop_all)
    except Exception as e:  # noqa: BLE001
        # Backend missing at runtime (headless X, etc.): keep the console
        # visible and keep serving so /api/quit and Ctrl+C still work.
        show_console()
        STATE["tray_active"] = False
        STATE["error"] = f"{type(e).__name__}: {e}"
        print("[desktop] tray unavailable:", e)
        _serve_forever(thread)
        server.shutdown()
        set_quit_callback(None)
        return 0

    STATE["tray_active"] = True
    print("[desktop] tray icon ready")
    try:
        icon.run()
    except KeyboardInterrupt:
        pass
    except Exception as e:  # noqa: BLE001
        show_console()
        STATE["tray_active"] = False
        STATE["error"] = f"{type(e).__name__}: {e}"
        print("[desktop] tray failed:", e)
        _serve_forever(thread)
    finally:
        try:
            icon.stop()
        except Exception:
            pass
        server.shutdown()
        STATE["tray_active"] = False
        set_quit_callback(None)
    return 0


def _serve_headless(server, flask_thread, url, use_tray):
    """Keep the already-running Flask server alive without a native window.

    Fallback for when pywebview or its GUI backend is unavailable (headless
    Linux, or Windows without pythonnet).  With a tray the app lives in the
    tray; otherwise it blocks until /api/quit or Ctrl+C.  Reuses the server
    and quit callback the caller already set up, and owns its own cleanup.
    """
    icon = None
    state = {"stopped": False}

    def stop_all():
        if state["stopped"]:
            return
        state["stopped"] = True
        try:
            if icon is not None:
                icon.stop()
        except Exception:
            pass
        try:
            server.shutdown()
        except Exception:
            pass

    set_quit_callback(stop_all)

    if use_tray:
        try:
            icon = _build_tray(url, stop_all)
        except Exception as e:  # noqa: BLE001
            STATE["tray_active"] = False
            STATE["error"] = f"{type(e).__name__}: {e}"
            print("[webview] tray unavailable:", e)
            icon = None
            use_tray = False

    try:
        if icon is not None:
            STATE["tray_active"] = True
            print("[webview] tray icon ready")
            icon.run()
        else:
            print("[webview] running headless; use 退出上位机 in the UI or Ctrl+C")
            _serve_forever(flask_thread)
    except KeyboardInterrupt:
        pass
    except Exception as e:  # noqa: BLE001
        STATE["error"] = f"{type(e).__name__}: {e}"
        print("[webview] tray failed:", e)
        _serve_forever(flask_thread)
    finally:
        stop_all()
        STATE["tray_active"] = False
        set_quit_callback(None)
    return 0


def run_webview(flask_app, port: int, url: str, start_minimized: bool = False,
                use_tray: bool = True, log_path: str = "",
                close_ref: dict | None = None) -> int:
    """Serve Flask behind a native pywebview window — the real desktop app.

    Unlike ``run_desktop`` (tray + browser), this renders the existing Web UI
    in a real window: Flask runs in a background thread, pywebview creates the
    window on the main thread and ``webview.start()`` blocks until it closes.
    Closing the window exits the app; with a tray available the window
    minimises to the tray instead.

    ``close_ref`` is a mutable ``{"behavior": "minimize" | "exit"}`` dict that
    the caller (``app.py``) owns and updates through the settings API.  The
    ``_on_closing`` closure reads it at close time so a setting change takes
    effect immediately without restarting the app.  When omitted it defaults
    to a local ``{"behavior": "minimize"}`` dict (static minimize-to-tray).

    When pywebview or its GUI backend is unavailable (headless Linux, or
    Windows without pythonnet) it falls back to ``_serve_headless`` so
    ``python app.py --desktop`` keeps working without a console crash.
    """
    STATE["desktop"] = True
    STATE["url"] = url
    if log_path:
        setup_stdio(log_path)

    if port_in_use(port):
        # Second double-click: the running instance is already up.  We cannot
        # raise another process's window, so just open the URL in a browser.
        print(f"[webview] already running at {url}, opening browser")
        open_url(url)
        return 0

    # 1. Start Flask in a background thread so the window can load the page.
    from werkzeug.serving import make_server
    try:
        server = make_server("127.0.0.1", port, flask_app, threaded=True)
    except OSError as e:
        STATE["error"] = str(e)
        print("[webview] cannot bind port:", e)
        return 1

    flask_thread = threading.Thread(target=server.serve_forever, name="flask", daemon=True)
    flask_thread.start()

    icon = [None]          # mutable holder for the pystray icon
    window_ref = [None]    # mutable holder for the pywebview window
    state = {"force_quit": False, "stopped": False}

    def stop_all():
        """Stop the tray, close the window and shut the server down."""
        if state["stopped"]:
            return
        state["stopped"] = True
        state["force_quit"] = True
        try:
            if window_ref[0] is not None:
                window_ref[0].destroy()
        except Exception:
            pass
        try:
            server.shutdown()
        except Exception:
            pass

    set_quit_callback(stop_all)

    # 2. Probe the tray (minimize-to-tray needs it).
    tray_ok, _tray_error = tray_available() if use_tray else (False, "托盘已禁用")
    use_tray = use_tray and tray_ok

    # 3. Create the native window (hidden when the user asked to start minimized).
    try:
        import webview
        window = webview.create_window(
            "BindDeckLite", url,
            width=1280, height=800, min_size=(960, 600),
            hidden=start_minimized,
        )
    except Exception as e:  # noqa: BLE001
        STATE["error"] = f"pywebview 不可用: {type(e).__name__}: {e}"
        print("[webview] native window unavailable, falling back to tray-only:", e)
        return _serve_headless(server, flask_thread, url, use_tray)

    window_ref[0] = window
    _window_ref[0] = window

    if close_ref is None:
        close_ref = {"behavior": "minimize"}

    def _on_closing(win):
        """Fired when the user hits the window close button.

        Return False to cancel the close (minimize to tray), True to allow it.
        pywebview's WinForms backend runs this on the GUI thread, so hiding the
        window here is safe and immediate.

        ``close_ref`` is read at close time so the user can switch the behavior
        at runtime through the settings page without restarting the app.

        When the user asks to minimize but no tray is available (old exe,
        missing pystray/Pillow, headless backend) the app must not silently
        degrade that into an exit: hide the window, keep serving, and let the
        web UI be the recovery path (``/api/show-window`` / ``/api/quit``).
        """
        if close_ref["behavior"] == "exit":
            return True
        if not state["force_quit"] and use_tray:
            try:
                win.hide()
            except Exception:
                pass
            STATE["window_hidden"] = True
            return False
        # Tray unavailable but user chose minimize-to-tray: keep the process
        # alive instead of silently exiting.  The web UI at the served URL
        # remains the recovery path — "显示窗口" restores the window,
        # "退出上位机" stops the app.
        if close_ref["behavior"] == "minimize":
            try:
                win.hide()
            except Exception:
                pass
            STATE["window_hidden"] = True
            return False
        return True

    window.events.closing += _on_closing

    # 4. Start the tray icon (if available).  pywebview's
    #    Window.show()/hide()/destroy() marshal onto the GUI thread, so the
    #    tray callbacks are safe to call from the pystray thread.
    if use_tray:
        try:
            icon[0] = _build_tray(url, stop_all, window)
            threading.Thread(target=icon[0].run, name="tray", daemon=True).start()
            STATE["tray_active"] = True
            print("[webview] tray icon ready")
        except Exception as e:  # noqa: BLE001
            STATE["tray_active"] = False
            STATE["error"] = f"{type(e).__name__}: {e}"
            print("[webview] tray unavailable:", e)

    # 5. Block on the native GUI loop until the window closes.  A missing GUI
    #    backend (headless Linux) falls back to serving headlessly instead of
    #    crashing; a normal close stops the server and tray below.
    try:
        webview.start()
    except Exception as e:  # noqa: BLE001
        STATE["error"] = f"pywebview GUI 不可用: {type(e).__name__}: {e}"
        print("[webview] GUI backend unavailable, serving headless:", e)
        return _serve_headless(server, flask_thread, url, use_tray)

    stop_all()
    try:
        if icon[0] is not None:
            icon[0].stop()
    except Exception:
        pass
    STATE["tray_active"] = False
    STATE["window_hidden"] = False
    _window_ref[0] = None
    set_quit_callback(None)
    return 0
