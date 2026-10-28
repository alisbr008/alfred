"""
AlfredWebUI — drop-in replacement for JarvisUI that renders the batcave web
dashboard (web_ui/index.html) inside an embedded QWebEngineView.

The brain (JarvisLocal) keeps calling the same methods it always did
(write_log, set_state, muted, mark_startup_ready, …); each one now forwards
to JavaScript instead of painting PyQt widgets.  All cross-thread calls are
marshalled onto the GUI thread via a Qt signal, so the brain can call from
its worker threads exactly as before.
"""
from __future__ import annotations

import functools
import http.server
import json
import socketserver
import sys
import threading
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot, QUrl, Qt, QFile, QIODevice
from PyQt6.QtWidgets import QApplication, QMainWindow
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEngineSettings
from PyQt6.QtWebChannel import QWebChannel

BASE_DIR  = Path(__file__).resolve().parent.parent
WEB_ROOT  = BASE_DIR / "web_ui"
INDEX     = WEB_ROOT / "index.html"


def _ensure_qwebchannel_js() -> None:
    """The dashboard is served over http://127.0.0.1 (YouTube live embeds refuse
    file:// pages — 'error 153'), and an http page can't load qrc:// resources.
    So extract Qt's qwebchannel.js to web_ui/ once and serve it as a normal file."""
    dst = WEB_ROOT / "qwebchannel.js"
    if dst.exists():
        return
    try:
        f = QFile(":/qtwebchannel/qwebchannel.js")
        if f.open(QIODevice.OpenModeFlag.ReadOnly):
            dst.write_bytes(bytes(f.readAll()))
            f.close()
            print("[UI] qwebchannel.js extracted for http serving")
    except Exception as e:
        print(f"[UI] couldn't extract qwebchannel.js: {e}")


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:   # keep Alfred's console clean
        pass


class _UIServer(socketserver.ThreadingTCPServer):
    """Threaded (the dashboard loads many assets in parallel) and quiet about
    clients hanging up mid-transfer (video seeks, page reloads)."""
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        import sys as _s
        exc = _s.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def _start_ui_server(root: Path) -> int:
    """Serve web_ui/ on a random free localhost port; returns the port."""
    handler = functools.partial(_QuietHandler, directory=str(root))
    httpd = _UIServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return port


class _Bridge(QObject):
    """Registered as `bridge` in JS. JS → Python calls land here (GUI thread)."""

    def __init__(self, ui: "AlfredWebUI"):
        super().__init__()
        self._ui = ui

    @pyqtSlot(str)
    def textCommand(self, text: str) -> None:
        cb = self._ui._text_cb
        if cb and text.strip():
            threading.Thread(target=cb, args=(text,), daemon=True).start()

    @pyqtSlot()
    def toggleMute(self) -> None:
        self._ui.muted = not self._ui._muted

    @pyqtSlot(bool)
    def setTyping(self, on: bool) -> None:
        # mic gates on this so keyboard noise / ambient isn't transcribed while typing
        self._ui._typing = bool(on)

    @pyqtSlot()
    def gestureEngineFailed(self) -> None:
        cb = getattr(self._ui, "_gesture_fallback_cb", None)
        if cb:
            threading.Thread(target=cb, daemon=True).start()

    @pyqtSlot(str)
    def gestureCameraFrame(self, data_url: str) -> None:
        # webcam frame from the browser (has camera permission) → gesture tracker
        try:
            from core.gestures import push_frame
            push_frame(str(data_url))
        except Exception:
            pass

    @pyqtSlot(str, str)
    def addEvidenceFile(self, name: str, data_url: str) -> None:
        cb = getattr(self._ui, "_evidence_cb", None)
        if cb:
            threading.Thread(target=cb, args=(str(name), str(data_url)), daemon=True).start()

    @pyqtSlot(bool)
    def overlayOpen(self, on: bool) -> None:
        # a fullscreen tab opened/closed → hands auto-engage/disengage (no command)
        cb = getattr(self._ui, "_overlay_cb", None)
        if cb:
            threading.Thread(target=cb, args=(bool(on),), daemon=True).start()

    @pyqtSlot(str, str)
    def cameraFrame(self, b64: str, question: str) -> None:
        # a frame grabbed from the live self-view → analyse with the vision model
        cb = getattr(self._ui, "_vision_cb", None)
        if cb and b64:
            threading.Thread(target=cb, args=(b64, question), daemon=True).start()

    @pyqtSlot()
    def requestCouncil(self) -> None:
        cb = getattr(self._ui, "_council_cb", None)
        if cb:
            threading.Thread(target=cb, daemon=True).start()

    @pyqtSlot()
    def requestIntegrations(self) -> None:
        cb = getattr(self._ui, "_integrations_cb", None)
        if cb:
            threading.Thread(target=cb, daemon=True).start()

    @pyqtSlot()
    def requestMissions(self) -> None:
        cb = getattr(self._ui, "_missions_cb", None)
        if cb:
            threading.Thread(target=cb, daemon=True).start()

    @pyqtSlot()
    def attachFile(self) -> None:
        # Runs on the GUI thread (bridge slot) — safe to open a native dialog.
        self._ui._open_attach_dialog()

    @pyqtSlot()
    def clearAttachment(self) -> None:
        self._ui._attachment = None

    @pyqtSlot()
    def reconfigure(self) -> None:
        cb = self._ui._reconfigure_cb
        if cb:
            threading.Thread(target=cb, daemon=True).start()


class AlfredWebUI(QObject):
    """Implements the JarvisUI contract over an embedded web dashboard."""

    _js = pyqtSignal(str)   # any thread → GUI thread (queued)

    def __init__(self, face_path: str | None = None):
        if QApplication.instance() is None:
            QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
        self._app = QApplication.instance() or QApplication(sys.argv or ["alfred"])
        super().__init__()

        self._muted          = False
        self._typing         = False
        self._attachment     = None     # {name, path, text} staged for the next message
        self._text_cb        = None
        self._council_cb     = None
        self._integrations_cb = None
        self._missions_cb    = None
        self._vision_cb      = None      # (b64, question) → analyse a webcam frame
        self._overlay_cb     = None      # (open: bool) → auto gesture engage/disengage
        self._evidence_cb    = None      # (name, data_url) → attach a file to the case
        self._gesture_fallback_cb = None # in-page vision engine failed → use Python
        self._council_cb     = None
        self._reconfigure_cb = None
        self._current_file   = None
        self._ready          = False
        self._alive          = True
        self._pending: list[str] = []

        self._win = QMainWindow()
        self._win.setWindowTitle("ALFRED")
        self._win.resize(1320, 840)

        self._view = QWebEngineView()
        self._win.setCentralWidget(self._view)

        # A local file:// page is blocked from loading CDN scripts (Three.js, D3)
        # unless we explicitly allow it. Also ensure WebGL is on for the 3D core.
        _st = self._view.settings()
        _st.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        _st.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        _st.setAttribute(QWebEngineSettings.WebAttribute.WebGLEnabled, True)
        _st.setAttribute(QWebEngineSettings.WebAttribute.Accelerated2dCanvasEnabled, True)
        # The mission board's LIVE BROADCAST screen autoplays (muted) — no click needed.
        _st.setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)
        # English everywhere — otherwise YouTube/embeds localise to the IP's locale (Malay).
        # And a clean Chrome UA: the default advertises "QtWebEngine", which YouTube's
        # player rejects outright ("An error occurred / player configuration error").
        try:
            prof = self._view.page().profile()
            prof.setHttpAcceptLanguage("en-US,en;q=0.9")
            prof.setHttpUserAgent("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                                  "Chrome/126.0.0.0 Safari/537.36")
        except Exception:
            pass

        self._channel = QWebChannel()
        self._bridge  = _Bridge(self)
        self._channel.registerObject("bridge", self._bridge)
        self._view.page().setWebChannel(self._channel)

        # Let the dashboard's live self-view use the webcam (localhost = secure context).
        try:
            self._view.page().featurePermissionRequested.connect(self._on_feature_perm)
        except Exception:
            pass
        self._view.loadFinished.connect(self._on_loaded)
        # Serve the dashboard over localhost instead of file:// — required for the
        # mission board's live YouTube broadcasts (they reject file:// with error 153).
        _ensure_qwebchannel_js()
        self._port = _start_ui_server(WEB_ROOT)
        # NB: must be the hostname "localhost", not 127.0.0.1 — YouTube's embed player
        # rejects raw-IP origins with error 153 but accepts localhost.
        self._view.load(QUrl(f"http://localhost:{self._port}/index.html"))

        self._js.connect(self._run_js)   # GUI-thread slot
        try:
            self._app.aboutToQuit.connect(self._on_quit)   # stop driving a dying UI
        except Exception:
            pass
        self.root = self                  # main.py calls ui.root.mainloop()

    # ── page lifecycle ──────────────────────────────────────────────────────
    def _on_quit(self) -> None:
        # Window/app is closing — worker threads must stop touching the UI object.
        self._alive = False

    def _on_loaded(self, ok: bool) -> None:
        self._ready = True
        for js in self._pending:
            self._view.page().runJavaScript(js)
        self._pending.clear()

    def _run_js(self, js: str) -> None:
        if not self._alive:
            return
        try:
            if self._ready:
                self._view.page().runJavaScript(js)
            else:
                self._pending.append(js)
        except RuntimeError:
            self._alive = False   # underlying Qt object was deleted

    def _call(self, fn: str, *args) -> None:
        if not self._alive:
            return
        payload = ", ".join(json.dumps(a) for a in args)
        try:
            self._js.emit(f"window.AlfredUI && window.AlfredUI.{fn}({payload});")
        except RuntimeError:
            self._alive = False

    # ── contract: visual updates (thread-safe) ──────────────────────────────
    def write_log(self, text: str) -> None:          self._call("writeLog", str(text))
    def set_state(self, state: str) -> None:          self._call("setState", str(state))
    def set_startup_status(self, text: str) -> None:  self._call("setStartupStatus", str(text))
    def mark_startup_ready(self, key: str, error: bool = False) -> None:
        self._call("markReady", str(key), bool(error))
    def show_startup_panel(self) -> None:             self._call("showStartup")
    def hide_startup_panel(self) -> None:             self._call("hideStartup")
    def set_memory(self, count: int, items=None) -> None:
        self._call("setMemory", int(count), list(items or []))

    def show_scheme(self) -> None:
        self._call("showScheme")

    def show_visualization(self, payload: dict) -> None:
        """Render an interactive visual (3d / diagram / timeline / architecture)
        in the webview. `payload` is JSON-encoded into a JS object by _call."""
        self._call("showViz", dict(payload or {}))

    def show_visualization_loading(self, title: str, label: str = "") -> None:
        """Show the 'MODELING…' state while a real 3D model is generated (Meshy)."""
        self._call("vizLoading", str(title), str(label))

    def show_agents(self, payload: dict) -> None:
        """Render the Agent Council architecture (Sentinel · Oracle · Echo)."""
        self._call("showAgents", dict(payload or {}))

    def show_council(self, payload: dict) -> None:
        """Render the full command-centre dashboard (Sentinel · Oracle · Echo)."""
        self._call("showCouncil", dict(payload or {}))

    def show_integrations(self, payload: dict) -> None:
        """Render the Integrations tab (all apps + connected status)."""
        self._call("showIntegrations", dict(payload or {}))

    def update_world(self, payload: dict) -> None:
        """Push live world-model feed + markers to the globe."""
        self._call("updateWorld", dict(payload or {}))

    def set_night(self, on: bool) -> None:
        """Toggle the NIGHT WATCH batcave theme (dim + amber, slow orb)."""
        self._call("setNight", bool(on))

    def show_missions(self, payload: dict) -> None:
        """Render the MISSION BOARD batcomputer wall."""
        self._call("showMissions", dict(payload or {}))

    def close_overlays(self) -> None:
        """Close every fullscreen overlay (showcase uses this between scenes)."""
        self._call("closeAll")

    def show_dev_council(self) -> None:
        """Engineering council terminal — GLM · Claude Code · Kimi building (showcase)."""
        self._call("showDevCouncil")

    def show_camera(self, question: str = "") -> None:
        """Open the live webcam self-view tab; JS grabs a frame back for vision."""
        self._call("showCamera", str(question or ""))

    def _on_feature_perm(self, origin, feature) -> None:
        try:
            from PyQt6.QtWebEngineCore import QWebEnginePage
            F = QWebEnginePage.Feature
            if feature in (F.MediaVideoCapture, F.MediaAudioCapture, F.MediaAudioVideoCapture):
                self._view.page().setFeaturePermission(
                    origin, feature, QWebEnginePage.PermissionPolicy.PermissionGrantedByUser)
        except Exception:
            pass

    # ── file attachment ──────────────────────────────────────────────────────
    def _open_attach_dialog(self) -> None:
        """Native file picker → read the file → stage it for the next message."""
        try:
            from PyQt6.QtWidgets import QFileDialog
            path, _ = QFileDialog.getOpenFileName(self._win, "Attach a file", str(Path.home()))
        except Exception as e:
            print(f"[Attach] dialog failed: {e}")
            return
        if not path:
            return
        p = Path(path)
        self._attachment = {"name": p.name, "path": str(p), "text": self._read_attachment(p)}
        self._call("showAttachment", p.name)

    def _read_attachment(self, p: Path) -> str:
        """Extract usable text from the file (text/code/markdown/json/csv + PDF)."""
        suf = p.suffix.lower()
        try:
            if suf == ".pdf":
                return self._read_pdf(p)
            if suf in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".bmp", ".tiff"):
                return (f"(image file: {p.name}, {p.stat().st_size} bytes, at {p}. "
                        f"No vision model is wired yet — refer to it by path.)")
            return p.read_text(encoding="utf-8", errors="ignore")[:20000]
        except Exception as e:
            return f"(could not read {p.name}: {e})"

    def _read_pdf(self, p: Path) -> str:
        for mod in ("pypdf", "PyPDF2"):
            try:
                m = __import__(mod)
                reader = m.PdfReader(str(p))
                txt = "\n".join((pg.extract_text() or "") for pg in reader.pages)
                return (txt[:20000] or f"(PDF {p.name} had no extractable text.)")
            except Exception:
                continue
        return f"(PDF attached: {p.name}; install 'pypdf' to read its text. Path: {p})"

    def take_attachment(self):
        """Brain calls this once per turn; returns + clears the staged file."""
        a = self._attachment
        self._attachment = None
        if a:
            self._call("clearAttachment")
        return a

    # ── properties ──────────────────────────────────────────────────────────
    @property
    def muted(self) -> bool:
        return self._muted

    @muted.setter
    def muted(self, v: bool) -> None:
        self._muted = bool(v)
        self._call("setMuted", self._muted)

    @property
    def typing(self) -> bool:
        return self._typing

    @property
    def current_file(self):
        return self._current_file

    def clear_file(self) -> None:
        self._current_file = None

    @property
    def on_text_command(self):
        return self._text_cb

    @on_text_command.setter
    def on_text_command(self, cb) -> None:
        self._text_cb = cb

    @property
    def on_request_council(self):
        return self._council_cb

    @on_request_council.setter
    def on_request_council(self, cb) -> None:
        self._council_cb = cb

    @property
    def on_reconfigure(self):
        return self._reconfigure_cb

    @on_reconfigure.setter
    def on_reconfigure(self, cb) -> None:
        self._reconfigure_cb = cb

    # ── lifecycle (tkinter-compatible surface main.py expects) ──────────────
    def wait_for_api_key(self) -> None:
        # Config already exists on subsequent runs → no setup overlay needed.
        return

    def protocol(self, *_args) -> None:
        return

    def mainloop(self) -> None:
        self._win.show()
        self._app.exec()
