from __future__ import annotations

import importlib.util
import ctypes
import ctypes.wintypes
import os
import subprocess
import sys
import time

APP_NAME = "NovaLauncher"


def _show_windows_message(text: str, title: str = APP_NAME) -> None:
    try:
        message_box = ctypes.windll.user32.MessageBoxW
        message_box.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.LPCWSTR,
                                ctypes.wintypes.LPCWSTR, ctypes.c_uint]
        message_box.restype = ctypes.c_int
        message_box(None, text, title, 0x10)
    except Exception:
        pass


def _ensure_admin() -> None:
    """Relaunch elevated on Windows if needed, ensuring low-level hooks work."""
    if os.name != "nt":
        return
    try:
        if ctypes.windll.shell32.IsUserAnAdmin():
            return
    except (AttributeError, OSError):
        pass
    if getattr(sys, "frozen", False):
        executable = sys.executable
        parameters = subprocess.list2cmdline(sys.argv[1:])
    else:
        executable = sys.executable
        parameters = subprocess.list2cmdline([os.path.abspath(sys.argv[0]), *sys.argv[1:]])
    try:
        shell32 = ctypes.windll.shell32
        shell32.ShellExecuteW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.LPCWSTR,
                                          ctypes.wintypes.LPCWSTR, ctypes.wintypes.LPCWSTR,
                                          ctypes.wintypes.LPCWSTR, ctypes.c_int]
        shell32.ShellExecuteW.restype = ctypes.c_void_p
        result = shell32.ShellExecuteW(None, "runas", executable, parameters, None, 1)
    except Exception as exc:
        _show_windows_message(f"Could not request Administrator access: {exc}", "NovaLauncher Startup")
        raise SystemExit(1)
    if result and result > 32:
        raise SystemExit(0)
    _show_windows_message("Administrator permission is required to start NovaLauncher.", "NovaLauncher Startup")
    raise SystemExit(1)


_ensure_admin()


def _ensure_dependencies() -> None:
    if getattr(sys, "frozen", False):
        return
    packages = {"PySide6": "PySide6", "keyboard": "keyboard"}
    missing = [pip_name for module, pip_name in packages.items()
               if importlib.util.find_spec(module) is None]
    if not missing:
        return
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--quiet", *missing],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        raise RuntimeError("Could not install required packages: " + ", ".join(missing)) from exc


try:
    _ensure_dependencies()
except RuntimeError as exc:
    _show_windows_message(str(exc), "NovaLauncher Setup")
    raise SystemExit(1)

import ast
import heapq
import re
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import keyboard
from PySide6.QtCore import (
    QEasingCurve, QEvent, QPropertyAnimation, QPoint, QRect, QSize, QObject, QRunnable,
    Qt, QThreadPool, QTimer, Signal, Slot,
)
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QVBoxLayout,
)

_INSTANCE_MUTEX = None
NOVA_ACTIVATE_MESSAGE = 0x8000 + 0x4A


def _activate_existing_window() -> bool:
    """Enforce single instance via Windows Mutex and bring existing window to foreground."""
    global _INSTANCE_MUTEX
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.wintypes.BOOL, ctypes.wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = ctypes.wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
    kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
    ctypes.set_last_error(0)
    try:
        _INSTANCE_MUTEX = kernel32.CreateMutexW(None, False, "Local\\NovaLauncher.SingleInstance.v1")
        if not _INSTANCE_MUTEX:
            raise ctypes.WinError(ctypes.get_last_error(), "Could not create NovaLauncher mutex")
        if ctypes.get_last_error() != 183:  # ERROR_ALREADY_EXISTS
            return False

        user32 = ctypes.windll.user32
        user32.FindWindowW.argtypes = [ctypes.wintypes.LPCWSTR, ctypes.wintypes.LPCWSTR]
        user32.FindWindowW.restype = ctypes.wintypes.HWND
        user32.PostMessageW.argtypes = [ctypes.wintypes.HWND, ctypes.c_uint,
                                       ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
        user32.PostMessageW.restype = ctypes.wintypes.BOOL
        hwnd = 0
        for _ in range(60):
            hwnd = user32.FindWindowW(None, APP_NAME)
            if hwnd and user32.PostMessageW(hwnd, NOVA_ACTIVATE_MESSAGE, 0, 0):
                kernel32.CloseHandle(_INSTANCE_MUTEX)
                _INSTANCE_MUTEX = None
                return True
            time.sleep(0.05)
        raise RuntimeError("NovaLauncher is already running, but its window could not be activated.")
    except Exception:
        if _INSTANCE_MUTEX:
            try:
                kernel32.CloseHandle(_INSTANCE_MUTEX)
            except Exception:
                pass
            _INSTANCE_MUTEX = None
        raise


@dataclass(frozen=True)
class AppEntry:
    name: str
    target: str
    kind: str = "app"
    detail: str = ""


class WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(str)


class Worker(QRunnable):
    def __init__(self, fn: Callable, *args):
        super().__init__()
        self.fn, self.args = fn, args
        self.signals = WorkerSignals()

    @Slot()
    def run(self):
        try:
            if os.name == "nt":
                try:
                    kernel32 = ctypes.windll.kernel32
                    kernel32.GetCurrentThread.argtypes = []
                    kernel32.GetCurrentThread.restype = ctypes.wintypes.HANDLE
                    kernel32.SetThreadPriority.argtypes = [ctypes.wintypes.HANDLE, ctypes.c_int]
                    kernel32.SetThreadPriority.restype = ctypes.wintypes.BOOL
                    kernel32.SetThreadPriority(kernel32.GetCurrentThread(), -2)  # THREAD_PRIORITY_LOWEST
                except (AttributeError, OSError):
                    pass
            self.signals.result.emit(self.fn(*self.args))
        except Exception as exc:
            self.signals.error.emit(str(exc))


def index_shortcuts() -> list[AppEntry]:
    """Background scanner for Start Menu and Desktop shortcuts."""
    roots: list[Path] = []
    appdata = os.environ.get("APPDATA")
    programdata = os.environ.get("PROGRAMDATA")
    userprofile = os.environ.get("USERPROFILE")
    if appdata:
        roots.append(Path(appdata) / "Microsoft/Windows/Start Menu/Programs")
    if programdata:
        roots.append(Path(programdata) / "Microsoft/Windows/Start Menu/Programs")
    if userprofile:
        roots.append(Path(userprofile) / "Desktop")
    public = os.environ.get("PUBLIC")
    if public:
        roots.append(Path(public) / "Desktop")
    entries: dict[str, AppEntry] = {}
    max_shortcuts = 4000
    max_scanned_entries = 20000
    max_depth = 8
    visited_entries = 0
    for root in roots:
        if not root.is_dir():
            continue
        stack = [(root, 0)]
        while stack and len(entries) < max_shortcuts and visited_entries < max_scanned_entries:
            folder, depth = stack.pop()
            try:
                with os.scandir(folder) as contents:
                    for child in contents:
                        if visited_entries >= max_scanned_entries or len(entries) >= max_shortcuts:
                            break
                        visited_entries += 1
                        try:
                            if child.is_dir(follow_symlinks=False):
                                if depth < max_depth and not child.name.startswith("."):
                                    stack.append((Path(child.path), depth + 1))
                            elif child.is_file(follow_symlinks=False) and child.name.casefold().endswith(".lnk"):
                                label = Path(child.name).stem.replace("_", " ").strip()
                                if not label or label.casefold().startswith("uninstall"):
                                    continue
                                key = label.casefold()
                                entries.setdefault(key, AppEntry(label, child.path, "app", child.path))
                        except (OSError, PermissionError):
                            continue
                        if visited_entries % 64 == 0:
                            time.sleep(0.01)
            except (OSError, PermissionError):
                continue
            time.sleep(0.004)
    return sorted(entries.values(), key=lambda entry: entry.name.casefold())


def fuzzy_score(query: str, candidate: str) -> int:
    q, c = query.casefold().strip(), candidate.casefold()
    if not q:
        return 0
    if q == c:
        return 1000
    if c.startswith(q):
        return 800 - (len(c) - len(q))
    if q in c:
        return 600 - c.index(q) * 2 - (len(c) - len(q))
    pos, score, streak, last = 0, 0, 0, -2
    for char in q:
        found = c.find(char, pos)
        if found < 0:
            return -1
        streak = streak + 1 if found == last + 1 else 0
        boundary = found == 0 or c[found - 1] in " _-."
        score += 8 + (12 if boundary else 0) + streak * 3 - max(0, found - pos)
        last, pos = found, found + 1
    return score - max(0, len(c) - len(q))


def _looks_like_url(text: str) -> bool:
    text = text.strip()
    if not text or " " in text:
        return False
    if re.match(r"^(https?://|www\.)", text, re.I):
        return True
    return bool(re.match(r"^[\w.-]+\.[a-z]{2,}(?::\d+)?(?:/.*)?$", text, re.I))


def _calculator(text: str) -> str | None:
    candidate = text.strip()
    if not candidate or len(candidate) > 256 or not re.fullmatch(r"[\d\s.+\-*/()%]+", candidate):
        return None
    try:
        tree = ast.parse(candidate, mode="eval")
        allowed_binary = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
                          ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b,
                          ast.FloorDiv: lambda a, b: a // b, ast.Mod: lambda a, b: a % b}
        allowed_unary = {ast.UAdd: lambda a: a, ast.USub: lambda a: -a}

        def calculate(node):
            if isinstance(node, ast.Expression):
                return calculate(node.body)
            if isinstance(node, ast.Constant) and type(node.value) in (int, float):
                if isinstance(node.value, int) and node.value.bit_length() > 256:
                    raise ValueError("Number too large")
                return node.value
            if isinstance(node, ast.BinOp) and type(node.op) in allowed_binary:
                left, right = calculate(node.left), calculate(node.right)
                if abs(left) > 1e12 or abs(right) > 1e12:
                    raise ValueError("Number too large")
                result = allowed_binary[type(node.op)](left, right)
                if abs(result) > 1e100:
                    raise ValueError("Result too large")
                return result
            if isinstance(node, ast.UnaryOp) and type(node.op) in allowed_unary:
                return allowed_unary[type(node.op)](calculate(node.operand))
            raise ValueError("Unsupported expression")

        value = calculate(tree)
        if isinstance(value, (int, float)) and abs(value) < 1e100:
            return f"{value:g}" if isinstance(value, float) else str(value)
    except (ArithmeticError, SyntaxError, ValueError, TypeError):
        pass
    return None


class NovaWindow(QMainWindow):
    toggleRequested = Signal()
    hideRequested = Signal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAutoFillBackground(False)
        self.setFixedWidth(680)
        self._bar_height = 60
        self._expanded_height = self._bar_height
        self._resize_animation = QPropertyAnimation(self, b"geometry", self)
        self._resize_animation.setDuration(150)
        self._resize_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.resize(680, self._bar_height)
        self.pool = QThreadPool.globalInstance()
        self.toggleRequested.connect(self.toggle)
        self.hideRequested.connect(self.hide)
        self.apps: list[AppEntry] = []
        self._indexing = False
        self._index_worker = None
        self._mouse_buttons_down: set[int] = set()
        self._user32 = None
        self._outside_click_timer = QTimer(self)
        self._outside_click_timer.setInterval(25)
        self._outside_click_timer.timeout.connect(self._poll_outside_click)
        if os.name == "nt":
            try:
                self._user32 = ctypes.windll.user32
                self._user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
                self._user32.GetAsyncKeyState.restype = ctypes.c_short
                self._user32.GetCursorPos.argtypes = [ctypes.POINTER(ctypes.wintypes.POINT)]
                self._user32.GetCursorPos.restype = ctypes.wintypes.BOOL
            except Exception:
                self._user32 = None
        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(25)
        self._search_debounce.timeout.connect(lambda: self.refresh_results(self.search.text()))
        self._build_ui()
        self._index_apps()
        self.search.textChanged.connect(lambda _text: self._search_debounce.start())
        self.results.itemActivated.connect(self.activate_item)
        self.search.returnPressed.connect(self.activate_current)

    def _build_ui(self):
        self.shell = QFrame()
        self.shell.setObjectName("shell")
        self.shell.setProperty("expanded", False)
        layout = QVBoxLayout(self.shell)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        search_row = QFrame()
        search_row.setObjectName("searchRow")
        search_row.setFixedHeight(self._bar_height)
        row = QHBoxLayout(search_row)
        row.setContentsMargins(22, 0, 20, 0)
        row.setSpacing(13)

        icon = QLabel()
        icon.setObjectName("searchIcon")
        icon.setFixedSize(20, 20)
        icon.setPixmap(self._make_search_icon())
        row.addWidget(icon, 0, Qt.AlignmentFlag.AlignVCenter)

        self.search = QLineEdit()
        self.search.setObjectName("search")
        self.search.setPlaceholderText("Nova search")
        self.search.setMaxLength(256)
        self.search.setClearButtonEnabled(True)
        self.search.installEventFilter(self)
        row.addWidget(self.search, 1)
        layout.addWidget(search_row)

        self.results = QListWidget()
        self.results.setObjectName("results")
        self.results.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.results.setFrameShape(QFrame.Shape.NoFrame)
        self.results.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.results.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.results.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.results.setUniformItemSizes(True)
        self.results.setFixedHeight(0)
        self.results.hide()
        layout.addWidget(self.results)

        self.setCentralWidget(self.shell)
        self.setStyleSheet("""
            QFrame#shell {
                background: rgba(30, 35, 45, 220);
                border: 1px solid rgba(255, 255, 255, 40);
                border-radius: 26px;
            }
            QFrame#shell[expanded="true"] { border-radius: 24px; }
            QFrame#searchRow, QLabel#searchIcon { background: transparent; border: none; }
            QLineEdit#search {
                background: transparent;
                border: none;
                padding: 0;
                color: rgba(248, 249, 252, 245);
                font-family: "Segoe UI", "SF Pro Display", sans-serif;
                font-size: 16px;
                selection-background-color: rgba(139, 168, 255, 115);
            }
            QLineEdit#search::placeholder { color: rgba(205, 211, 222, 180); }
            QListWidget#results {
                background: transparent;
                border: none;
                outline: none;
                color: rgba(245, 247, 251, 245);
                font-family: "Segoe UI", "SF Pro Display", sans-serif;
                font-size: 13px;
                padding: 5px 12px 10px 12px;
            }
            QListWidget#results::item {
                border: 1px solid transparent;
                border-radius: 11px;
                padding: 8px 12px;
                margin: 1px 0;
            }
            QListWidget#results::item:selected {
                background: rgba(139, 168, 255, 40);
                border: 1px solid rgba(170, 190, 255, 38);
                color: white;
            }
            QListWidget#results::item:hover { background: rgba(255, 255, 255, 14); }
            QScrollBar:vertical { width: 5px; background: transparent; margin: 5px 2px; }
            QScrollBar::handle:vertical { background: rgba(220, 226, 238, 75); border-radius: 2px; min-height: 20px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
        """)

    @staticmethod
    def _make_search_icon():
        pixmap = QPixmap(20, 20)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(205, 213, 228, 210), 1.8,
                            Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawEllipse(3, 3, 11, 11)
        painter.drawLine(12, 12, 17, 17)
        painter.end()
        return pixmap

    def _enable_acrylic(self):
        """Native Windows Acrylic blur with safe fallback."""
        class AccentPolicy(ctypes.Structure):
            _fields_ = [("state", ctypes.c_int), ("flags", ctypes.c_int),
                        ("gradient_color", ctypes.c_uint), ("animation_id", ctypes.c_int)]

        class CompositionAttributeData(ctypes.Structure):
            _fields_ = [("attribute", ctypes.c_int), ("data", ctypes.POINTER(AccentPolicy)),
                        ("size", ctypes.c_size_t)]
        try:
            policy = AccentPolicy(4, 2, 0xDC2D231E, 0)
            data = CompositionAttributeData(19, ctypes.pointer(policy), ctypes.sizeof(policy))
            api = ctypes.windll.user32.SetWindowCompositionAttribute
            api.argtypes = [ctypes.wintypes.HWND, ctypes.POINTER(CompositionAttributeData)]
            api.restype = ctypes.c_int
            api(self.winId(), ctypes.byref(data))
        except Exception:
            pass

    def _index_apps(self):
        if self._indexing:
            return
        self._indexing = True
        worker = Worker(index_shortcuts)
        self._index_worker = worker
        worker.signals.result.connect(self._on_indexed)
        worker.signals.error.connect(self._on_index_error)
        self.pool.start(worker)

    @Slot(object)
    def _on_indexed(self, entries):
        self.apps = entries
        self._indexing = False
        self._index_worker = None
        self.refresh_results(self.search.text())

    @Slot(str)
    def _on_index_error(self, message):
        self._indexing = False
        self._index_worker = None
        self.search.setToolTip("Shortcut indexing failed; built-in actions are available.")

    @Slot(str)
    def refresh_results(self, text):
        query = text.strip()
        self.results.clear()
        if query:
            matches = ((score, entry) for entry in self.apps
                       if (score := fuzzy_score(query, entry.name)) >= 0)
            best = heapq.nsmallest(5, matches,
                                   key=lambda pair: (-pair[0], pair[1].name.casefold()))
            for _, entry in best:
                self._add_result(entry.name, "Application", entry)

            number = _calculator(query)
            if number is not None:
                self._add_result(f"{query}  =  {number}", "Calculator · Enter to copy",
                                 AppEntry(number, "", "calc"))

            key = query.casefold()
            if key in {"shutdown", "restart", "taskmgr", "lock"}:
                details = {"shutdown": "Shut down Windows", "restart": "Restart Windows",
                           "taskmgr": "Open Task Manager", "lock": "Lock this PC"}
                self._add_result(key, details[key], AppEntry(key, "", "command"))

            if key == "google":
                self._add_result("Open Google", "google.com",
                                 AppEntry("https://www.google.com", "", "url"))
            elif key == "youtube":
                self._add_result("Open YouTube", "youtube.com",
                                 AppEntry("https://www.youtube.com", "", "url"))
            elif key.startswith(("yt ", "youtube ")):
                term = re.sub(r"^(yt|youtube)\s+", "", query, flags=re.I)
                self._add_result(f"Search YouTube for ‘{term}’", "YouTube Search", AppEntry(term, "", "yt"))
            elif key.startswith(("g ", "google ")):
                term = re.sub(r"^(g|google)\s+", "", query, flags=re.I)
                self._add_result(f"Search Google for ‘{term}’", "Google Search", AppEntry(term, "", "websearch"))
            elif _looks_like_url(query):
                shown = query if re.match(r"^https?://", query, re.I) else "https://" + query
                self._add_result(f"Go to {shown}", "Open Website", AppEntry(shown, "", "url"))

            if self.results.count() == 0 or (" " in query and not key.startswith(("yt ", "youtube ", "g ", "google "))):
                self._add_result(f"Search the web for ‘{query}’", "Google Search",
                                 AppEntry(query, "", "websearch"))

        if self.results.count():
            self.results.setCurrentRow(0)
        self._update_expansion(bool(query))

    def _update_expansion(self, expanded: bool):
        if expanded:
            rows = min(max(1, self.results.count()), 6)
            self.results.setFixedHeight(rows * 49 + 14)
            self.results.show()
            target_height = self._bar_height + rows * 49 + 14
        else:
            self.results.hide()
            self.results.setFixedHeight(0)
            target_height = self._bar_height

        self._expanded_height = target_height
        if self.shell.property("expanded") != expanded:
            self.shell.setProperty("expanded", expanded)
            self.shell.style().unpolish(self.shell)
            self.shell.style().polish(self.shell)

        if not self.isVisible() or self.height() == target_height:
            self.resize(self.width(), target_height)
            return
        start = self.geometry()
        end = QRect(start.x(), start.y(), self.width(), target_height)
        self._resize_animation.stop()
        self._resize_animation.setStartValue(start)
        self._resize_animation.setEndValue(end)
        self._resize_animation.start()

    def _add_result(self, title, detail, entry):
        item = QListWidgetItem(f"{title}\n{detail}")
        item.setSizeHint(QSize(self.width() - 46, 49))
        item.setData(Qt.ItemDataRole.UserRole, entry)
        self.results.addItem(item)

    @Slot()
    def activate_current(self):
        item = self.results.currentItem()
        if item:
            self.activate_item(item)

    @Slot(QListWidgetItem)
    def activate_item(self, item):
        entry: AppEntry = item.data(Qt.ItemDataRole.UserRole)
        try:
            import webbrowser
            if entry.kind == "app":
                os.startfile(entry.detail or entry.target)
            elif entry.kind == "url":
                os.startfile(entry.name)
            elif entry.kind == "websearch":
                webbrowser.open("https://www.google.com/search?q=" + urllib.parse.quote_plus(entry.name))
            elif entry.kind == "yt":
                webbrowser.open("https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(entry.name))
            elif entry.kind == "calc":
                QApplication.clipboard().setText(entry.name)
                return
            elif entry.kind == "command":
                commands = {
                    "shutdown": ["shutdown", "/s", "/t", "0"],
                    "restart": ["shutdown", "/r", "/t", "0"],
                    "taskmgr": ["taskmgr.exe"],
                    "lock": ["rundll32.exe", "user32.dll,LockWorkStation"],
                }
                subprocess.Popen(commands[entry.target.casefold()], shell=False,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.hide()
        except Exception:
            pass

    def _place(self):
        screen = QGuiApplication.primaryScreen()
        if not screen:
            return
        area = screen.availableGeometry()
        x = area.x() + (area.width() - self.width()) // 2
        y = area.y() + int(area.height() * 0.20)
        self.setGeometry(x, y, self.width(), self._expanded_height)

    def toggle(self):
        if self.isVisible():
            self.hide()
        else:
            self.refresh_results(self.search.text())
            self._place()
            self.show()
            self._activate_foreground()
            self.search.selectAll()

    def _activate_from_second_launch(self):
        """Show and focus this instance without toggling it back to hidden."""
        self._place()
        self.show()
        self._activate_foreground()
        self.search.selectAll()

    def _activate_foreground(self, retry: int = 0):
        """Ask both Qt and Windows to make the search field immediately type-ready."""
        if retry and not self.isVisible():
            return
        try:
            self.showNormal()
            self.raise_()
            self.activateWindow()
            self.search.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

            hwnd = self.winId()
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            user32.GetForegroundWindow.restype = ctypes.wintypes.HWND
            user32.GetWindowThreadProcessId.argtypes = [ctypes.wintypes.HWND,
                                                        ctypes.POINTER(ctypes.wintypes.DWORD)]
            user32.GetWindowThreadProcessId.restype = ctypes.wintypes.DWORD
            user32.AttachThreadInput.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.DWORD,
                                                 ctypes.wintypes.BOOL]
            user32.ShowWindow.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
            user32.ShowWindow.restype = ctypes.wintypes.BOOL
            user32.SetForegroundWindow.argtypes = [ctypes.wintypes.HWND]
            user32.SetForegroundWindow.restype = ctypes.wintypes.BOOL
            user32.BringWindowToTop.argtypes = [ctypes.wintypes.HWND]
            user32.BringWindowToTop.restype = ctypes.wintypes.BOOL
            user32.SetActiveWindow.argtypes = [ctypes.wintypes.HWND]
            user32.SetActiveWindow.restype = ctypes.wintypes.HWND
            user32.SetFocus.argtypes = [ctypes.wintypes.HWND]
            user32.SetFocus.restype = ctypes.wintypes.HWND
            kernel32.GetCurrentThreadId.restype = ctypes.wintypes.DWORD

            foreground = user32.GetForegroundWindow()
            foreground_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
            current_thread = kernel32.GetCurrentThreadId()
            attached = bool(foreground_thread and foreground_thread != current_thread and
                            user32.AttachThreadInput(current_thread, foreground_thread, True))
            try:
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
                user32.SetActiveWindow(hwnd)
                user32.SetFocus(hwnd)
            finally:
                if attached:
                    user32.AttachThreadInput(current_thread, foreground_thread, False)

            self.search.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
            if not self.isActiveWindow() and retry < 3:
                QTimer.singleShot(35, lambda: self._activate_foreground(retry + 1))
        except Exception:
            # Qt's activation path remains available when a Windows API is restricted.
            try:
                self.activateWindow()
                self.search.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
            except Exception:
                pass

    def _poll_outside_click(self):
        """Hide only on a new mouse click whose screen position is outside this window."""
        if not self.isVisible() or self._user32 is None:
            return
        try:
            buttons = (0x01, 0x02, 0x04, 0x05, 0x06)  # left, right, middle, and side buttons
            current_down: set[int] = set()
            new_click = False
            for button in buttons:
                button_state = self._user32.GetAsyncKeyState(button)
                if button_state & 0x8000:
                    current_down.add(button)
                if ((button_state & 0x8000 and button not in self._mouse_buttons_down) or
                        (button_state & 0x0001)):
                    new_click = True
            if new_click:
                cursor = ctypes.wintypes.POINT()
                if self._user32.GetCursorPos(ctypes.byref(cursor)):
                    if not self.frameGeometry().contains(QPoint(cursor.x, cursor.y)):
                        self.hide()
            self._mouse_buttons_down = current_down
        except Exception:
            self._mouse_buttons_down.clear()

    def hide(self):
        self._outside_click_timer.stop()
        super().hide()

    def showEvent(self, event):
        super().showEvent(event)
        if self._user32 is not None:
            try:
                self._mouse_buttons_down = {
                    button for button in (0x01, 0x02, 0x04, 0x05, 0x06)
                    if self._user32.GetAsyncKeyState(button) & 0x8000
                }
                self._outside_click_timer.start()
            except Exception:
                self._outside_click_timer.stop()

    def nativeEvent(self, event_type, message):
        """Handle activation requests sent by a second process through the mutex path."""
        try:
            native_message = ctypes.cast(int(message), ctypes.POINTER(ctypes.wintypes.MSG)).contents
            if native_message.message == NOVA_ACTIVATE_MESSAGE:
                QTimer.singleShot(0, self._activate_from_second_launch)
                return True, 0
        except Exception:
            pass
        try:
            return super().nativeEvent(event_type, message)
        except Exception:
            return False, 0

    def eventFilter(self, watched, event):
        if watched is self.search and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape:
                self.hide()
                return True
            if event.key() in (Qt.Key.Key_Down, Qt.Key.Key_Up) and self.results.count():
                delta = 1 if event.key() == Qt.Key.Key_Down else -1
                self.results.setCurrentRow((self.results.currentRow() + delta) % self.results.count())
                return True
        return super().eventFilter(watched, event)


def main() -> int:
    if os.name != "nt":
        raise SystemExit("NovaLauncher is designed for Windows.")
    try:
        if _activate_existing_window():
            return 0
    except Exception as exc:
        _show_windows_message(str(exc), "NovaLauncher Startup")
        return 1
        
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    window = NovaWindow()
    
    try:
        keyboard.add_hotkey("alt+space", window.toggleRequested.emit, suppress=True)
        keyboard.add_hotkey("esc", window.hideRequested.emit, suppress=False)
    except Exception as exc:
        window.search.setToolTip(f"Global hotkey unavailable: {exc}")
        
    # تم إزالة window.show() و window._activate_foreground() 
    # عشان يفضل البرنامج شغال بصمت في الخلفية ومش يظهر على الشاشة نهائياً.
    
    code = app.exec()
    
    try:
        keyboard.unhook_all_hotkeys()
    except Exception:
        pass
    return code


if __name__ == "__main__":
    raise SystemExit(main())
