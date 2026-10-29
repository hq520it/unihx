#!/usr/bin/env python3
"""unihx 发行控制台 — 项目 / 发行模式 / 离线打包 / 上传 / Git / 终端。"""
import fcntl
import glob
import json
import os
import pty
import struct
import termios
import re
import select
import shutil
import signal
import subprocess
import sys
import threading
import time

from PySide6.QtCore import Qt, QThread, Signal, QUrl, QEvent, QTimer
from PySide6.QtGui import (QColor, QDesktopServices, QFont, QIcon, QKeySequence,
                           QPainter, QShortcut, QTextCursor)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QSplitter,
    QMenu, QScrollArea, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

import pyte

ROOT = os.environ.get("UNIAPP_ROOT", "/mnt/225GB/uni-app")
CONFIG_DIR = os.path.expanduser("~/.local/share/unihx")
PROJECTS_FILE = os.path.join(CONFIG_DIR, "projects.json")
UNIHX = shutil.which("unihx") or os.path.expanduser("~/.local/bin/unihx")
MPCI = shutil.which("miniprogram-ci") or "miniprogram-ci"

ACCENT = "#a6e3a1"
BASE = "#1e1e2e"
MANTLE = "#181825"
SURFACE = "#313244"
TEXT = "#cdd6f4"
SUBTEXT = "#a6adc8"

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

STYLE = f"""
QWidget {{ background: {BASE}; color: {TEXT}; font-family: "Noto Sans CJK SC"; font-size: 13px; }}
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QListWidget {{
    background: {MANTLE}; border: 1px solid {SURFACE}; border-radius: 6px; padding: 4px 8px;
    selection-background-color: {ACCENT}; selection-color: #11111b;
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border: 1px solid {ACCENT}; }}
QListWidget::item {{ padding: 6px 8px; border-radius: 5px; }}
QListWidget::item:selected {{ background: {SURFACE}; color: {ACCENT}; }}
QPushButton {{
    background: {SURFACE}; border: 1px solid #45475a; border-radius: 6px; padding: 6px 12px;
}}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:disabled {{ color: #6c7086; border-color: #45475a; }}
QPushButton#primary {{ background: {ACCENT}; color: #11111b; font-weight: 600; border: none; }}
QPushButton#primary:hover {{ background: #94e2d5; }}
QPushButton#primary:disabled {{ background: #45475a; color: #6c7086; }}
QPushButton#danger {{ color: #f38ba8; }}
QPlainTextEdit {{ font-family: "JetBrains Mono"; font-size: 12px; }}
QLabel#title {{ font-size: 18px; font-weight: 700; color: {ACCENT}; }}
QLabel#hint {{ color: {SUBTEXT}; }}
QTabBar::tab {{ padding: 6px 16px; border-radius: 6px; margin-right: 4px; }}
QTabBar::tab:selected {{ background: {SURFACE}; color: {ACCENT}; }}
"""


def load_project_config():
    try:
        d = json.load(open(PROJECTS_FILE, encoding="utf-8"))
    except Exception:
        d = {}
    return {"extra": [x for x in d.get("extra", []) if isinstance(x, str)],
            "hidden": [x for x in d.get("hidden", []) if isinstance(x, str)]}


def save_project_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(PROJECTS_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def read_manifest(project):
    path = os.path.join(project, "manifest.json")
    if not os.path.isfile(path):
        return {}
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
        try:
            import json5
            d = json5.loads(text)
        except Exception:
            text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
            text = re.sub(r"//[^\n]*", "", text)
            text = re.sub(r",(\s*[}\]])", r"\1", text)
            d = json.loads(text)
        mp = d.get("mp-weixin") or {}
        return {"name": d.get("name") or os.path.basename(project),
                "appid": mp.get("appid") or "",
                "uni_appid": d.get("appid") or "",
                "version": d.get("versionName") or "1.0.0"}
    except Exception:
        return {}


def read_scripts(project):
    path = os.path.join(project, "package.json")
    if not os.path.isfile(path):
        return []
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
        try:
            import json5
            d = json5.loads(text)
        except Exception:
            d = json.loads(text)
        scripts = (d.get("uni-app") or {}).get("scripts") or {}
        return [{"name": k, "title": v.get("title") or k,
                 "platform": (v.get("env") or {}).get("UNI_PLATFORM") or ""}
                for k, v in scripts.items()]
    except Exception:
        return []


def strip_ansi(text):
    return ANSI_RE.sub("", text)


class Runner(QThread):
    line = Signal(str)
    done = Signal(bool, str)

    def __init__(self, cmd, cwd=None, env_extra=None):
        super().__init__()
        self.cmd = cmd
        self.cwd = cwd
        self.env_extra = env_extra or {}
        self.proc = None

    def run(self):
        env = os.environ.copy()
        env.update(self.env_extra)
        try:
            p = subprocess.Popen(self.cmd, cwd=self.cwd, env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, bufsize=1, encoding="utf-8", errors="replace",
                                 start_new_session=True)
            self.proc = p
        except Exception as e:
            self.done.emit(False, str(e))
            return
        for line in p.stdout:
            self.line.emit(line.rstrip("\n"))
        self.done.emit(p.wait() == 0, "")

    def stop(self):
        """终止整个进程组（含 dev watch 子进程）。"""
        p = self.proc
        if not p or p.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except Exception:
            try:
                p.terminate()
            except Exception:
                return
        for _ in range(30):
            if p.poll() is not None:
                return
            time.sleep(0.1)
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass


class DevWatcher(QThread):
    line = Signal(str)
    ready = Signal()
    finished_dev = Signal()

    def __init__(self, cmd, cwd, env_extra):
        super().__init__()
        self.cmd = cmd
        self.cwd = cwd
        self.env_extra = env_extra
        self.proc = None
        self._ready = False

    def run(self):
        env = os.environ.copy()
        env.update(self.env_extra)
        self.proc = subprocess.Popen(self.cmd, cwd=self.cwd, env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, bufsize=1, encoding="utf-8", errors="replace")
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            self.line.emit(line)
            if not self._ready and any(k in line for k in ("编译成功", "Build complete", "DONE", "App running at")):
                self._ready = True
                self.ready.emit()
        self.proc.wait()
        self.finished_dev.emit()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


KEY_SEQUENCES = {
    Qt.Key_Return: b"\r", Qt.Key_Enter: b"\r", Qt.Key_Backspace: b"\x7f",
    Qt.Key_Tab: b"\t", Qt.Key_Escape: b"\x1b",
    Qt.Key_Up: b"\x1b[A", Qt.Key_Down: b"\x1b[B", Qt.Key_Right: b"\x1b[C", Qt.Key_Left: b"\x1b[D",
    Qt.Key_Home: b"\x1b[H", Qt.Key_End: b"\x1b[F", Qt.Key_Delete: b"\x1b[3~",
    Qt.Key_PageUp: b"\x1b[5~", Qt.Key_PageDown: b"\x1b[6~",
}


def encode_key(event):
    key, mods = event.key(), event.modifiers()
    ctrl = bool(mods & Qt.ControlModifier)
    shift = bool(mods & Qt.ShiftModifier)
    if ctrl and shift and key == Qt.Key_C:
        return None  # 交给上层复制
    if ctrl and key == Qt.Key_V:
        text = QApplication.clipboard().text()
        return text.encode() if text else b""
    if key in KEY_SEQUENCES:
        if key == Qt.Key_Tab and shift:
            return b"\x1b[Z"
        return KEY_SEQUENCES[key]
    if ctrl and Qt.Key_A <= key <= Qt.Key_Z:
        return bytes([key - Qt.Key_A + 1])
    text = event.text()
    return text.encode() if text else b""


class TerminalView(QPlainTextEdit):
    send_bytes = Signal(bytes)
    resized = Signal()

    def __init__(self):
        super().__init__()
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setFont(QFont("JetBrains Mono", 11))
        self.setCursorWidth(2)
        self.setStyleSheet("QPlainTextEdit{background:#181825;border:none;padding:4px;}")
        self.setFocusPolicy(Qt.StrongFocus)
        self._cursor_pos = None
        self._cursor_on = True

    def set_block_cursor(self, pos):
        self._cursor_pos = pos
        self._refresh_cursor()

    def toggle_cursor(self):
        pass

    def _refresh_cursor(self):
        self.viewport().update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._cursor_pos is None:
            return
        cur = QTextCursor(self.document())
        cur.setPosition(self._cursor_pos)
        rect = self.cursorRect(cur)
        w = max(6, self.fontMetrics().horizontalAdvance("M"))
        rect.setWidth(w)
        painter = QPainter(self.viewport())
        painter.fillRect(rect, QColor("#a6e3a1"))

    def keyPressEvent(self, event):
        mods = event.modifiers()
        if mods & Qt.ControlModifier and mods & Qt.ShiftModifier and event.key() == Qt.Key_C:
            self.copy()
            return
        data = encode_key(event)
        if data is None:
            super().keyPressEvent(event)
            return
        if data:
            self.send_bytes.emit(data)
        event.accept()

    def inputMethodEvent(self, event):
        text = event.commitString()
        if text:
            self.send_bytes.emit(text.encode())
        event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resized.emit()
        self._refresh_cursor()


class TerminalWidget(QWidget):
    data_received = Signal(bytes)

    def __init__(self):
        super().__init__()
        self.master = None
        self.proc = None
        self._stop = True
        self.cwd = os.path.expanduser("~")
        self.screen = pyte.Screen(80, 24)
        self.stream = pyte.ByteStream(self.screen)

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        self.lbl_cwd = QLabel(self.cwd)
        self.lbl_cwd.setObjectName("hint")
        head.addWidget(QLabel("工作目录:"))
        head.addWidget(self.lbl_cwd, 1)
        btn_int = QPushButton("Ctrl+C")
        btn_int.clicked.connect(self.interrupt)
        btn_clear = QPushButton("清屏")
        btn_clear.clicked.connect(self.clear_screen)
        head.addWidget(btn_int)
        head.addWidget(btn_clear)
        v.addLayout(head)

        self.view = TerminalView()
        self.view.send_bytes.connect(self.write_bytes)
        self.view.resized.connect(self._on_resize)
        v.addWidget(self.view, 1)
        self.data_received.connect(self._on_data)
        # 光标常亮，避免闪烁时截图/观察不到

    def start(self, cwd=None):
        self.stop()
        if cwd:
            self.cwd = cwd
        self.lbl_cwd.setText(self.cwd)
        master, slave = pty.openpty()
        self.master = master
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        self.proc = subprocess.Popen(["/bin/bash", "--noprofile", "--norc"],
                                     stdin=slave, stdout=slave, stderr=slave,
                                     cwd=self.cwd, env=env, preexec_fn=os.setsid)
        os.close(slave)
        self.screen = pyte.Screen(80, 24)
        self.stream = pyte.ByteStream(self.screen)
        self._stop = False
        self._on_resize()
        threading.Thread(target=self._reader, daemon=True).start()
        self.view.setFocus()

    def _reader(self):
        while not self._stop:
            fd = self.master
            if fd is None:
                break
            try:
                r, _, _ = select.select([fd], [], [], 0.2)
                if not r:
                    continue
                data = os.read(fd, 65536)
                if not data:
                    break
                self.data_received.emit(data)
            except OSError:
                break

    def stop(self):
        self._stop = True
        if self.master is not None:
            try:
                os.close(self.master)
            except OSError:
                pass
            self.master = None
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.proc = None

    def write_bytes(self, data):
        if self.master is None:
            self.start(self.cwd)
        try:
            os.write(self.master, data)
        except OSError:
            pass

    def interrupt(self):
        self.write_bytes(b"\x03")

    def clear_screen(self):
        self.screen.reset()
        self.view.setPlainText("")

    def _on_resize(self):
        fm = self.view.fontMetrics()
        cw = max(6, fm.horizontalAdvance("M"))
        ch = max(10, fm.height())
        cols = max(20, (self.view.viewport().width() - 8) // cw)
        rows = max(8, (self.view.viewport().height() - 8) // ch)
        if (self.screen.columns, self.screen.lines) != (cols, rows):
            self.screen.resize(rows, cols)
        if self.master is not None:
            try:
                fcntl.ioctl(self.master, termios.TIOCSWINSZ,
                            struct.pack("HHHH", rows, cols, 0, 0))
            except OSError:
                pass

    def _on_data(self, data):
        try:
            self.stream.feed(data)
        except Exception:
            pass
        self._render()

    def _render(self):
        lines = [line.rstrip() for line in self.screen.display]
        cy, cx = self.screen.cursor.y, self.screen.cursor.x
        if 0 <= cy < len(lines) and len(lines[cy]) <= cx:
            lines[cy] = lines[cy] + " " * (cx - len(lines[cy]) + 1)
        text = "\n".join(lines).rstrip("\n")
        if self.view.toPlainText() != text:
            self.view.setPlainText(text)
        cursor = self.view.textCursor()
        cursor.movePosition(QTextCursor.Start)
        for _ in range(min(cy, max(0, len(lines) - 1))):
            cursor.movePosition(QTextCursor.Down)
        line_text = lines[cy] if lines and cy < len(lines) else ""
        cursor.movePosition(QTextCursor.Right, QTextCursor.MoveAnchor,
                            min(cx, len(line_text)))
        self.view.setTextCursor(cursor)
        self.view._cursor_on = True
        self.view.set_block_cursor(cursor.position())
        self.view.ensureCursorVisible()


class DiffDialog(QDialog):
    def __init__(self, title, text, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(1000, 680)
        self.setMinimumSize(560, 360)
        self.setSizeGripEnabled(True)
        v = QVBoxLayout(self)
        edit = QPlainTextEdit()
        edit.setReadOnly(True)
        edit.setPlainText(text)
        v.addWidget(edit)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        v.addWidget(close)


class MainWindow(QMainWindow):
    PLATFORM_TARGET = {"mp-weixin": 0, "h5": 3, "app-plus": 1}

    def __init__(self):
        super().__init__()
        self.setWindowTitle("unihx 发行控制台")
        self.resize(1320, 860)
        self.move(100, 70)
        self.projects = []
        self.worker = None
        self.dev = None
        self.artifact = ""
        self.script_platforms = {}
        self._h5_opened = False

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(8)

        title = QLabel("unihx 发行控制台")
        title.setObjectName("title")
        sub = QLabel("离线打包 · 自定义发行模式 · 本地调试 · Git · 终端")
        sub.setObjectName("hint")
        root.addWidget(title)
        root.addWidget(sub)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, 1)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 8, 0)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索项目…")
        self.search.textChanged.connect(self.filter_projects)
        lv.addWidget(self.search)
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self.on_select)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self.project_menu)
        lv.addWidget(self.list, 1)
        row = QHBoxLayout()
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.scan_projects)
        add_btn = QPushButton("添加项目")
        add_btn.clicked.connect(self.add_project)
        remove_btn = QPushButton("移除")
        remove_btn.clicked.connect(self.remove_project)
        row.addWidget(refresh)
        row.addWidget(add_btn)
        row.addWidget(remove_btn)
        lv.addLayout(row)
        splitter.addWidget(left)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(8, 0, 0, 0)
        rv.setSpacing(8)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.build_tab(), "发版")
        self.tabs.addTab(self.git_tab(), "Git")
        self.tabs.addTab(self.android_tab(), "运行")
        self.terminal = TerminalWidget()
        self.tabs.addTab(self.terminal, "终端")
        self.tabs.currentChanged.connect(self.on_tab_changed)
        for i in range(4):
            QShortcut(QKeySequence(f"Ctrl+{i + 1}"), self,
                      activated=lambda idx=i: self.tabs.setCurrentIndex(idx))

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(6000)
        self.log.setMinimumHeight(70)

        self.right_split = QSplitter(Qt.Vertical)
        right_split = self.right_split
        right_split.addWidget(self.tabs)
        right_split.addWidget(self.log)
        right_split.setStretchFactor(0, 3)
        right_split.setStretchFactor(1, 1)
        right_split.setSizes([620, 220])
        rv.addWidget(right_split, 1)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)

        self.status = self.statusBar()
        self.status.showMessage("就绪")
        self.scan_projects()
        self.refresh_compiler()
        self.refresh_devices()
        self.refresh_bases()

    def build_tab(self):
        page = QWidget()
        v = QVBoxLayout(page)
        info = QFrame()
        form = QFormLayout(info)
        form.setLabelAlignment(Qt.AlignRight)
        self.lbl_project = QLabel("—")
        self.lbl_uni_appid = QLabel("—")
        self.lbl_version = QLabel("—")
        self.lbl_mp_appid = QLabel("—")
        form.addRow("项目：", self.lbl_project)
        form.addRow("uni-appid：", self.lbl_uni_appid)
        form.addRow("版本：", self.lbl_version)
        form.addRow("小程序 appid：", self.lbl_mp_appid)
        v.addWidget(info)

        opts = QFrame()
        of = QFormLayout(opts)
        of.setLabelAlignment(Qt.AlignRight)
        self.target = QComboBox()
        self.target.addItems(["微信小程序", "WGT 离线包", "App 资源", "H5"])
        self.target.currentIndexChanged.connect(self.update_fields)
        of.addRow("发行目标：", self.target)

        self.mode = QComboBox()
        self.mode.setMinimumWidth(240)
        self.mode.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.mode.setToolTip("来自 package.json 的 uni-app.scripts；平台由脚本决定")
        self.mode.currentIndexChanged.connect(self.apply_mode)
        of.addRow("发行模式：", self.mode)

        self.version = QLineEdit()
        of.addRow("版本号：", self.version)
        self.appid = QLineEdit()
        of.addRow("小程序 appid：", self.appid)
        self.name = QLineEdit()
        self.name.setPlaceholderText("比如 mom.wgt")
        of.addRow("产物名称：", self.name)

        self.upload = QCheckBox("编译后上传开发版（miniprogram-ci）")
        of.addRow("", self.upload)
        self.install_deps = QCheckBox("缺少 node_modules 时自动 npm install")
        self.install_deps.setChecked(True)
        of.addRow("", self.install_deps)

        keyrow = QWidget()
        kh = QHBoxLayout(keyrow)
        kh.setContentsMargins(0, 0, 0, 0)
        self.key = QLineEdit()
        self.key.setPlaceholderText("代码上传密钥 private.wx….key")
        pick = QPushButton("选择…")
        pick.clicked.connect(self.pick_key)
        kh.addWidget(self.key, 1)
        kh.addWidget(pick)
        of.addRow("上传密钥：", keyrow)

        self.desc = QLineEdit()
        self.desc.setPlaceholderText("版本描述")
        of.addRow("版本描述：", self.desc)
        self.robot = QSpinBox()
        self.robot.setRange(1, 30)
        of.addRow("CI 机器人：", self.robot)
        v.addWidget(opts)

        comp_row = QHBoxLayout()
        self.lbl_compiler = QLabel("编译器: 检测中…")
        self.lbl_compiler.setObjectName("hint")
        self.btn_compiler = QPushButton("安装/更新编译器")
        self.btn_compiler.clicked.connect(self.install_compiler)
        comp_row.addWidget(self.lbl_compiler)
        comp_row.addStretch(1)
        comp_row.addWidget(self.btn_compiler)
        v.addLayout(comp_row)

        btns = QHBoxLayout()
        self.go = QPushButton("开始构建")
        self.go.setObjectName("primary")
        self.go.clicked.connect(self.start_build)
        self.upload_btn = QPushButton("上传开发版")
        self.upload_btn.clicked.connect(self.start_upload)
        self.run_btn = QPushButton("运行（调试）")
        self.run_btn.clicked.connect(self.start_dev)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_dev)
        self.open_btn = QPushButton("打开产物目录")
        self.open_btn.setEnabled(False)
        self.open_btn.clicked.connect(self.open_artifact)
        clear = QPushButton("清空日志")
        clear.clicked.connect(lambda: self.log.clear())
        btns.addWidget(self.go)
        btns.addWidget(self.upload_btn)
        btns.addWidget(self.run_btn)
        btns.addWidget(self.stop_btn)
        btns.addWidget(self.open_btn)
        btns.addStretch(1)
        btns.addWidget(clear)
        v.addStretch(1)

        outer = QWidget()
        ov = QVBoxLayout(outer)
        ov.setContentsMargins(0, 0, 0, 0)
        ov.addWidget(self._wrap_scroll(page), 1)
        bar = QWidget()
        bar.setLayout(btns)
        ov.addWidget(bar)          # 操作按钮常显，不随内容滚动
        return outer

    def _wrap_scroll(self, inner):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(inner)
        return scroll

    def android_tab(self):
        page = QWidget()
        v = QVBoxLayout(page)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("设备"))
        self.dev_combo = QComboBox()
        self.dev_combo.setMinimumWidth(260)
        row1.addWidget(self.dev_combo, 1)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh_devices)
        row1.addWidget(refresh)
        v.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("基座"))
        self.base_combo = QComboBox()
        self.base_combo.setMinimumWidth(320)
        row2.addWidget(self.base_combo, 1)
        pick = QPushButton("选择 APK…")
        pick.clicked.connect(self.pick_base_apk)
        row2.addWidget(pick)
        self.custom_base = QCheckBox("自定义基座")
        row2.addWidget(self.custom_base)
        row2.addWidget(QLabel("appid"))
        self.and_appid = QLineEdit()
        self.and_appid.setPlaceholderText("留空自动读取 manifest")
        row2.addWidget(self.and_appid, 1)
        v.addLayout(row2)

        row3 = QHBoxLayout()
        self.watch_check = QCheckBox("热更新（--watch）")
        row3.addWidget(self.watch_check)
        row3.addStretch(1)
        self.and_start = QPushButton("开始运行")
        self.and_start.clicked.connect(self.start_android)
        self.and_stop = QPushButton("停止")
        self.and_stop.setEnabled(False)
        self.and_stop.clicked.connect(self.stop_android)
        clear = QPushButton("清屏")
        clear.clicked.connect(lambda: self.and_log.clear())
        row3.addWidget(self.and_start)
        row3.addWidget(self.and_stop)
        row3.addWidget(clear)
        v.addLayout(row3)

        self.and_log = QPlainTextEdit()
        self.and_log.setReadOnly(True)
        self.and_log.setMaximumBlockCount(8000)
        self.and_log.setFont(QFont("JetBrains Mono, monospace", 10))
        v.addWidget(self.and_log, 1)
        return page

    def refresh_devices(self):
        if not hasattr(self, "dev_combo"):
            return
        old = self.dev_combo.currentData()
        self.dev_combo.clear()
        try:
            out = subprocess.run(["adb", "devices", "-l"], capture_output=True, text=True).stdout
        except Exception:
            out = ""
        for line in out.splitlines()[1:]:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            serial, state = parts[0], parts[1] if len(parts) > 1 else "?"
            model = ""
            for f in parts[2:]:
                if f.startswith("model:"):
                    model = f.split(":", 1)[1].replace("_", " ")
            label = f"{model or serial}  ({serial})"
            if state != "device":
                label += f"  [{state}]"
            self.dev_combo.addItem(label, serial)
        if old is not None:
            i = self.dev_combo.findData(old)
            if i >= 0:
                self.dev_combo.setCurrentIndex(i)

    def refresh_bases(self):
        if not hasattr(self, "base_combo"):
            return
        old = self.base_combo.currentData()
        self.base_combo.clear()
        self.base_combo.addItem("标准基座  io.dcloud.HBuilder",
                                {"pkg": "io.dcloud.HBuilder"})
        p = self.current_project()
        bases = []
        if p:
            for sub in ("unpackage/debug", "unpackage/release"):
                d = os.path.join(p["path"], sub)
                if os.path.isdir(d):
                    for n in sorted(os.listdir(d)):
                        if n.lower().endswith(".apk"):
                            bases.append(os.path.join(d, n))
        for apk in bases:
            self.base_combo.addItem(f"自定义基座  {os.path.basename(apk)}", {"apk": apk})
        if old is not None:
            for i in range(self.base_combo.count()):
                if self.base_combo.itemData(i) == old:
                    self.base_combo.setCurrentIndex(i)
                    break

    def pick_base_apk(self):
        apk, _ = QFileDialog.getOpenFileName(self, "选择自定义基座 APK", os.path.expanduser("~"),
                                             "Android APK (*.apk)")
        if not apk:
            return
        self.base_combo.addItem(f"自定义基座  {os.path.basename(apk)}", {"apk": apk})
        self.base_combo.setCurrentIndex(self.base_combo.count() - 1)
        self.custom_base.setChecked(True)

    def start_android(self):
        p = self.current_project()
        if not p:
            self.and_log.appendPlainText("⚠️ 先选择一个项目")
            return
        serial = self.dev_combo.currentData()
        base = self.base_combo.currentData() or {"pkg": "io.dcloud.HBuilder"}
        cmd = [UNIHX, "android", "--project", p["path"]]
        if base.get("apk"):
            cmd += ["--base-apk", base["apk"]]
        else:
            cmd += ["--base-package", base.get("pkg") or "io.dcloud.HBuilder"]
            if self.custom_base.isChecked():
                cmd.append("--custom-base")
        if serial:
            cmd += ["--device", serial]
        appid = self.and_appid.text().strip()
        if appid:
            cmd += ["--appid", appid]
        if self.watch_check.isChecked():
            cmd.append("--watch")
        self.and_log.appendPlainText("[运行] " + " ".join(cmd) + "\n")
        self.and_worker = Runner(cmd)
        self.and_worker.line.connect(self.on_android_line)
        self.and_worker.done.connect(self.on_android_done)
        self._and_stopping = False
        self.and_start.setEnabled(False)
        self.and_stop.setEnabled(True)
        self.and_worker.start()

    def on_android_line(self, text):
        self.and_log.appendPlainText(text)
        self.and_log.verticalScrollBar().setValue(self.and_log.verticalScrollBar().maximum())

    def on_android_done(self, ok, err):
        self.and_start.setEnabled(True)
        self.and_stop.setEnabled(False)
        if getattr(self, "_and_stopping", False):
            self._and_stopping = False
            self.and_log.appendPlainText("\n⏹ 已停止")
            return
        if err:
            self.and_log.appendPlainText("\n[错误] " + err)
        self.and_log.appendPlainText("\n✅ 已结束" if ok else "\n❌ 异常退出")

    def stop_android(self):
        self._and_stopping = True
        w = getattr(self, "and_worker", None)
        if w:
            try:
                w.stop()
            except Exception as e:
                self.and_log.appendPlainText(f"[停止失败] {e}")
        self.and_stop.setEnabled(False)
        self.and_log.appendPlainText("\n[运行] 正在停止…")

    def git_tab(self):
        page = QWidget()
        v = QVBoxLayout(page)
        head = QHBoxLayout()
        self.lbl_branch = QLabel("分支: —")
        head.addWidget(self.lbl_branch)
        head.addStretch(1)
        for text, slot in (("刷新", self.git_status),
                           ("全部暂存", lambda: self.run_git(["add", "-A"], "暂存全部")),
                           ("暂存选中", self.git_stage_selected),
                           ("取消暂存", self.git_unstage_selected),
                           ("拉取", lambda: self.run_git(["pull", "--ff-only"], "拉取")),
                           ("推送", lambda: self.run_git(["push"], "推送")),
                           ("新建分支", self.git_new_branch),
                           ("切换分支", self.git_switch_branch),
                           ("日志", self.git_log)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            head.addWidget(b)
        v.addLayout(head)

        self.git_list = QListWidget()
        self.git_list.setSelectionMode(QListWidget.ExtendedSelection)
        self.git_list.itemDoubleClicked.connect(self.git_diff)
        v.addWidget(self.git_list, 1)

        row = QHBoxLayout()
        self.git_msg = QLineEdit()
        self.git_msg.setPlaceholderText("提交信息…")
        self.git_push_after = QCheckBox("提交后推送")
        self.git_amend = QCheckBox("修正上次提交")
        commit = QPushButton("提交")
        commit.setObjectName("primary")
        commit.clicked.connect(self.git_commit)
        discard = QPushButton("丢弃选中更改")
        discard.setObjectName("danger")
        discard.clicked.connect(self.git_discard)
        row.addWidget(self.git_msg, 1)
        row.addWidget(self.git_amend)
        row.addWidget(self.git_push_after)
        row.addWidget(commit)
        row.addWidget(discard)
        v.addLayout(row)
        return self._wrap_scroll(page)

    def scan_projects(self):
        self.projects = []
        cfg = load_project_config()
        hidden = {os.path.realpath(x) for x in cfg["hidden"]}
        seen = set()

        def add(p):
            rp = os.path.realpath(p)
            if rp in hidden or rp in seen:
                return
            if not os.path.isfile(os.path.join(p, "manifest.json")):
                return
            seen.add(rp)
            info = read_manifest(p)
            info["path"] = os.path.abspath(p)
            info["dir"] = os.path.basename(os.path.normpath(p))
            info["scripts"] = read_scripts(p)
            self.projects.append(info)

        for p in sorted(glob.glob(os.path.join(ROOT, "*"))):
            add(p)
        for p in cfg["extra"]:
            if os.path.isdir(p):
                add(p)
        self.list.clear()
        for p in self.projects:
            item = QListWidgetItem(f"{p['name']}   ·   {p['dir']}")
            item.setData(Qt.UserRole, p["path"])
            self.list.addItem(item)
        self.status.showMessage(f"共 {len(self.projects)} 个 uni-app 项目（{ROOT}）")
        if self.list.count() and not self.list.currentItem():
            self.list.setCurrentRow(0)

    def project_menu(self, pos):
        item = self.list.itemAt(pos)
        if not item:
            return
        self.list.setCurrentRow(self.list.row(item))
        menu = self._build_project_menu()
        menu.exec(self.list.mapToGlobal(pos))

    def _build_project_menu(self):
        p = self.current_project()
        menu = QMenu(self)
        if not p:
            return menu
        menu.addAction("打开项目目录", lambda: self.open_project_dir(p))
        menu.addAction("在终端中打开", lambda: self.open_project_terminal(p))
        menu.addSeparator()
        git_menu = menu.addMenu("Git")
        git_menu.addAction("状态", lambda: self.git_status())
        git_menu.addAction("日志", lambda: self.git_log())
        git_menu.addSeparator()
        git_menu.addAction("全部暂存", lambda: self.run_git(["add", "-A"], "暂存全部"))
        git_menu.addAction("提交…", self.focus_commit)
        git_menu.addSeparator()
        git_menu.addAction("拉取", lambda: self.run_git(["pull", "--ff-only"], "拉取"))
        git_menu.addAction("推送", lambda: self.run_git(["push"], "推送"))
        git_menu.addSeparator()
        git_menu.addAction("切换分支", self.git_switch_branch)
        git_menu.addAction("新建分支", self.git_new_branch)
        menu.addSeparator()
        menu.addAction("移除项目", lambda: self.remove_project())
        return menu

    def open_project_dir(self, p):
        QDesktopServices.openUrl(QUrl.fromLocalFile(p["path"]))

    def open_project_terminal(self, p):
        self.tabs.setCurrentIndex(3)
        if self.terminal.master is not None and self.terminal.cwd != p["path"]:
            self.terminal.start(p["path"])
        elif self.terminal.master is None:
            self.terminal.start(p["path"])
        self.terminal.view.setFocus()

    def focus_commit(self):
        self.tabs.setCurrentIndex(1)
        self.git_msg.setFocus()

    def add_project(self):
        path = QFileDialog.getExistingDirectory(self, "选择 uni-app 项目目录", os.path.expanduser("~"))
        if not path:
            return
        if not os.path.isfile(os.path.join(path, "manifest.json")):
            QMessageBox.warning(self, "不是 uni-app 项目", "该目录下没有 manifest.json")
            return
        cfg = load_project_config()
        rp = os.path.realpath(path)
        if rp not in {os.path.realpath(x) for x in cfg["extra"]} and \
                not rp.startswith(os.path.realpath(ROOT) + os.sep):
            cfg["extra"].append(os.path.abspath(path))
        cfg["hidden"] = [x for x in cfg["hidden"] if os.path.realpath(x) != rp]
        save_project_config(cfg)
        self.scan_projects()
        for i in range(self.list.count()):
            if os.path.realpath(self.list.item(i).data(Qt.UserRole)) == rp:
                self.list.setCurrentRow(i)
                break

    def remove_project(self):
        p = self.current_project()
        if not p:
            return
        if QMessageBox.question(self, "移除项目",
                                f"从列表中移除 {p['name']}？\n（不会删除项目文件）") != QMessageBox.Yes:
            return
        rp = os.path.realpath(p["path"])
        cfg = load_project_config()
        cfg["extra"] = [x for x in cfg["extra"] if os.path.realpath(x) != rp]
        if rp not in {os.path.realpath(x) for x in cfg["hidden"]}:
            cfg["hidden"].append(os.path.abspath(p["path"]))
        save_project_config(cfg)
        self.scan_projects()

    def filter_projects(self, text):
        text = text.strip().lower()
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setHidden(bool(text) and text not in it.text().lower())

    def current_project(self):
        it = self.list.currentItem()
        if not it:
            return None
        path = it.data(Qt.UserRole)
        for p in self.projects:
            if p["path"] == path:
                return p
        return None

    def on_select(self, *_):
        p = self.current_project()
        if not p:
            return
        self.lbl_project.setText(p["name"] + "   " + p["path"])
        self.lbl_uni_appid.setText(p.get("uni_appid") or "—")
        self.lbl_version.setText(p.get("version") or "—")
        self.lbl_mp_appid.setText(p.get("appid") or "—")
        self.version.setText(p.get("version") or "1.0.0")
        self.appid.setText(p.get("appid") or "")
        self.name.setText(p["dir"] + ".wgt")
        self.refresh_bases()

        self.script_platforms = {sc["name"]: sc.get("platform", "") for sc in (p.get("scripts") or [])}
        self.mode.clear()
        self.mode.addItem("默认发行", "")
        for sc in p.get("scripts") or []:
            tag = f"  [{sc['platform']}]" if sc.get("platform") else ""
            self.mode.addItem(sc["title"] + tag, sc["name"])
        self.apply_mode()
        self.status.showMessage(f"已选择 {p['name']}，自定义模式 {len(p.get('scripts') or [])} 个")
        self.git_status()
        self.refresh_compiler()
        if self.terminal.master is not None and p["path"] != self.terminal.cwd:
            self.terminal.start(p["path"])

    def apply_mode(self, *_):
        name = self.mode.currentData()
        platform = self.script_platforms.get(name, "")
        if name and platform:
            self.target.setCurrentIndex(self.PLATFORM_TARGET.get(platform, self.target.currentIndex()))
            self.target.setEnabled(False)
            self.target.setToolTip(f"平台由自定义模式决定：{platform}")
        else:
            self.target.setEnabled(True)
            self.target.setToolTip("")

    def update_fields(self):
        t = self.target.currentIndex()
        mp = t == 0
        for w in (self.appid, self.key, self.desc, self.robot):
            w.setEnabled(mp)
        self.upload.setEnabled(mp)
        if not mp:
            self.upload.setChecked(False)
        self.name.setEnabled(t == 1)

    def pick_key(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择上传密钥", os.path.expanduser("~"))
        if path:
            self.key.setText(path)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "log") and hasattr(self, "right_split"):
            max_h = max(120, self.height() // 2)
            self.log.setMaximumHeight(max_h)
            sizes = self.right_split.sizes()
            if len(sizes) == 2 and sizes[1] > max_h:
                total = sum(sizes)
                self.right_split.setSizes([total - max_h, max_h])

    def on_tab_changed(self, idx):
        if self.tabs.widget(idx) is self.terminal:
            if self.terminal.master is None:
                p = self.current_project()
                self.terminal.start(p["path"] if p else os.path.expanduser("~"))
            self.terminal.view.setFocus()

    def refresh_compiler(self):
        try:
            r = subprocess.run([UNIHX, "compiler", "path"], capture_output=True, text=True, timeout=10)
            path = (r.stdout or "").strip()
        except Exception:
            path = ""
        if not path or not os.path.isdir(path):
            self.lbl_compiler.setText("编译器: 未安装（首次构建会自动安装）")
            return
        ver = ""
        try:
            vf = os.path.join(path, "VERSION")
            if os.path.isfile(vf):
                ver = open(vf).read().strip()
        except Exception:
            pass
        name = os.path.basename(path)
        self.lbl_compiler.setText(f"编译器: {ver or name}")

    def install_compiler(self):
        self.log.clear()
        self.log.appendPlainText("[编译器] 安装/更新中…\n")
        self.run_worker([UNIHX, "compiler", "install"], ok_markers=("已安装", "已是最新", "已从本地复制"))

    def start_build(self):
        p = self.current_project()
        if not p:
            self.log.appendPlainText("⚠️ 请先选择一个项目")
            return
        t = self.target.currentIndex()
        script = self.mode.currentData()
        self.log.clear()
        self.open_btn.setEnabled(False)
        self.artifact = ""
        cmd = [UNIHX, "build", "--project", p["path"]]
        if t == 0:
            self.artifact = os.path.join(p["path"], "unpackage/dist/build/mp-weixin")
            cmd += ["--target", "mp-weixin"]
        elif t == 1:
            name = self.name.text().strip() or (p["dir"] + ".wgt")
            if not name.endswith(".wgt"):
                name += ".wgt"
            self.artifact = os.path.join(p["path"], "unpackage/release", name)
            cmd += ["--target", "wgt", "--wgt-name", name]
        elif t == 2:
            self.artifact = os.path.join(p["path"], "unpackage/dist/build/app-plus")
            cmd += ["--target", "app"]
        else:
            self.artifact = os.path.join(p["path"], "unpackage/dist/build/h5")
            cmd += ["--target", "h5"]
        if script:
            cmd += ["--script", script]
            self.log.appendPlainText(f"[发行模式] {self.mode.currentText()}  (UNI_SCRIPT={script})")
        if self.install_deps.isChecked():
            cmd += ["--install"]
        self.log.appendPlainText(f"[离线打包] {' '.join(cmd)}\n")
        self.run_worker(cmd, ok_markers=("Build complete", "DONE"))

    def start_dev(self):
        p = self.current_project()
        if not p:
            self.log.appendPlainText("⚠️ 请先选择一个项目")
            return
        t = self.target.currentIndex()
        script = self.mode.currentData()
        platform = self.script_platforms.get(script, "") if script else ""
        if not platform:
            platform = {0: "mp-weixin", 3: "h5"}.get(t, "")
        self._h5_opened = False
        if platform == "h5":
            cmd = [UNIHX, "serve", "--project", p["path"]]
            out = os.path.join(p["path"], "unpackage/dist/dev/h5")
            self.log.appendPlainText(f"[运行] H5 开发服务器 → {out}")
        elif platform == "mp-weixin":
            cmd = [UNIHX, "run", "--project", p["path"]]
            out = os.path.join(p["path"], "unpackage/dist/dev/mp-weixin")
            self.log.appendPlainText(f"[运行] watch → {out}")
        else:
            self.log.appendPlainText("⚠️ 该目标不支持运行调试（WGT/App 资源请用「开始构建」）")
            return
        if script:
            cmd += ["--script", script]
        if self.install_deps.isChecked():
            cmd += ["--install"]
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.artifact = out
        self.dev = DevWatcher(cmd, None, {})
        self.dev.line.connect(self.on_line)
        self.dev.ready.connect(lambda: self.status.showMessage("已启动"))
        self.dev.finished_dev.connect(self.on_dev_finished)
        self.dev.start()

    def start_upload(self):
        p = self.current_project()
        if not p:
            self.log.appendPlainText("⚠️ 请先选择一个项目")
            return
        key = self.key.text().strip()
        appid = self.appid.text().strip() or p.get("appid", "")
        version = self.version.text().strip() or "1.0.0"
        if not key or not appid:
            self.log.appendPlainText("⚠️ 上传需要密钥路径和 appid")
            return
        dist = os.path.join(p["path"], "unpackage/dist/build/mp-weixin")
        if not os.path.isdir(dist):
            self.log.appendPlainText(f"⚠️ 产物目录不存在：{dist}（先构建）")
            return
        cmd = [MPCI, "upload", "--appid", appid, "--pp", dist, "--pkp", key,
               "--uv", version, "--robot", str(self.robot.value())]
        if self.desc.text().strip():
            cmd += ["--ud", self.desc.text().strip()]
        self.log.appendPlainText("[上传] " + " ".join(cmd) + "\n")
        self.run_worker(cmd, ok_markers=("上传成功", "success", "Success"))

    def run_worker(self, cmd, cwd=None, env_extra=None, ok_markers=()):
        self.go.setEnabled(False)
        self.upload_btn.setEnabled(False)
        self.status.showMessage("运行中…")
        self.worker = Runner(cmd, cwd, env_extra)
        self.worker.line.connect(self.on_line)
        self.worker.done.connect(lambda ok, err: self.on_done(ok, err))
        self.worker.start()

    def on_line(self, text):
        self.log.appendPlainText(text)
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())
        if (self.dev is not None and self.target.currentIndex() == 3
                and not self._h5_opened and "http://" in text):
            m = re.search(r"https?://(?:localhost|127\.0\.0\.1)[:0-9/]*", text)
            if m:
                self._h5_opened = True
                QDesktopServices.openUrl(QUrl(m.group(0)))

    def on_done(self, ok, err):
        self.refresh_compiler()
        self.go.setEnabled(True)
        self.upload_btn.setEnabled(True)
        if err:
            self.log.appendPlainText("\n[错误] " + err)
        if ok:
            self.status.showMessage("完成")
            self.open_btn.setEnabled(bool(self.artifact) and os.path.exists(self.artifact))
            self.log.appendPlainText("\n✅ 完成")
        else:
            self.status.showMessage("失败")
            self.log.appendPlainText("\n❌ 失败，请查看日志")

    def stop_dev(self):
        if self.dev:
            self.dev.stop()
        self.log.appendPlainText("\n[运行] 已停止")

    def on_dev_finished(self):
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status.showMessage("调试已停止")

    def open_artifact(self):
        if not self.artifact:
            return
        folder = self.artifact if os.path.isdir(self.artifact) else os.path.dirname(self.artifact)
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    # ---------- Git ----------
    def git(self, *args):
        p = self.current_project()
        if not p:
            return None
        return subprocess.run(["git", "-C", p["path"]] + list(args),
                              capture_output=True, text=True)

    def git_status(self):
        p = self.current_project()
        if not p:
            return
        r = self.git("status", "--porcelain=v1", "-uall")
        self.git_list.clear()
        if r is None or r.returncode != 0:
            self.git_list.addItem("（不是 Git 仓库或 git 不可用）")
            self.lbl_branch.setText("分支: —")
            return
        br = self.git("rev-parse", "--abbrev-ref", "HEAD")
        ab = self.git("status", "-sb")
        ahead = ""
        if ab and ab.stdout:
            m = re.search(r"\[(.+?)\]", ab.stdout.splitlines()[0])
            if m:
                ahead = f"  ({m.group(1)})"
        self.lbl_branch.setText(f"分支: {br.stdout.strip() if br else '?'}{ahead}")
        lines = [l for l in r.stdout.splitlines() if l.strip()]
        if not lines:
            self.git_list.addItem("（工作区干净）")
        for l in lines:
            code, path = l[:2], l[3:]
            item = QListWidgetItem(f"{code}  {path}")
            item.setData(Qt.UserRole, path)
            item.setData(Qt.UserRole + 1, code)
            self.git_list.addItem(item)

    def selected_paths(self):
        return [it.data(Qt.UserRole) for it in self.git_list.selectedItems()
                if it.data(Qt.UserRole)]

    def git_stage_selected(self):
        paths = self.selected_paths()
        if paths:
            self.run_git(["add", "--"] + paths, "暂存选中")

    def git_unstage_selected(self):
        paths = self.selected_paths()
        if paths:
            self.run_git(["reset", "HEAD", "--"] + paths, "取消暂存")

    def git_log(self):
        r = self.git("log", "--graph", "--oneline", "--decorate",
                     "--date=short", "--pretty=format:%h %ad %d %s", "-80")
        text = (r.stdout if r and r.returncode == 0 else "") or "（没有提交记录）"
        DiffDialog("Git 日志", text, self).exec()

    def git_diff(self, item):
        path = item.data(Qt.UserRole)
        if not path:
            return
        r = self.git("diff", "HEAD", "--", path)
        text = (r.stdout if r and r.stdout else "")
        r2 = self.git("diff", "--cached", "--", path)
        if r2 and r2.stdout:
            text += "\n===== 已暂存 =====\n" + r2.stdout
        DiffDialog(f"差异: {path}", text or "（无差异）", self).exec()

    def git_commit(self):
        msg = self.git_msg.text().strip()
        if not msg:
            self.log.appendPlainText("⚠️ 请填写提交信息")
            return
        if self.git_amend.isChecked():
            self.run_git(["commit", "--amend", "-m", msg], "修正提交")
        else:
            r = self.git("commit", "-m", msg)
            self.log.appendPlainText((r.stdout or r.stderr).strip())
        self.git_msg.clear()
        if self.git_push_after.isChecked():
            self.run_git(["push"], "推送")
        self.git_status()

    def git_discard(self):
        paths = self.selected_paths()
        if not paths:
            return
        if QMessageBox.question(self, "确认", f"丢弃 {len(paths)} 个文件的更改？此操作不可恢复。") != QMessageBox.Yes:
            return
        self.run_git(["checkout", "--"] + paths, "丢弃更改")
        for pth in paths:
            self.git("clean", "-f", "--", pth)
        self.git_status()

    def git_new_branch(self):
        name, ok = QInputDialog.getText(self, "新建分支", "分支名:")
        if ok and name.strip():
            self.run_git(["checkout", "-b", name.strip()], "新建分支")

    def git_switch_branch(self):
        r = self.git("branch", "--format=%(refname:short)")
        branches = [b for b in (r.stdout.splitlines() if r else []) if b.strip()]
        if not branches:
            return
        name, ok = QInputDialog.getItem(self, "切换分支", "选择分支:", branches, 0, False)
        if ok and name:
            self.run_git(["checkout", name], "切换分支")

    def run_git(self, args, label):
        p = self.current_project()
        if not p:
            return
        r = subprocess.run(["git", "-C", p["path"]] + args, capture_output=True, text=True)
        self.log.appendPlainText(f"[git {label}] " + (r.stdout.strip() or r.stderr.strip()))
        self.git_status()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("unihx 发行控制台")
    app.setStyleSheet(STYLE)
    icon = os.path.expanduser("~/.local/share/icons/unihx.svg")
    if os.path.isfile(icon):
        app.setWindowIcon(QIcon(icon))
    win = MainWindow()
    win.show()
    rc = app.exec()
    win.terminal.stop()
    sys.exit(rc)


if __name__ == "__main__":
    main()
