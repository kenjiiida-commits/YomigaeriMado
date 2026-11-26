#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
よみがえり窓 (Yomigaeri-mado) v0.2.1
Ubuntu の再インストール後に、前のアプリをまとめて入れ直すためのソフト。

  1. 今のパソコンに入っているアプリ（Snap / apt / Flatpak）をリストに書き出す
  2. 再インストール後にリストを開き、チェックを入れて選ぶ
  3. 「まとめてインストール」でパスワード1回だけで一括インストール
  4. GitHub で配布されている DEB アプリは、最新版を自動で取ってきて入れる

License: MIT  (c) 2026 飯田顕治
"""

import datetime
import gzip
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

from PySide6.QtCore import QProcess, Qt
from PySide6.QtGui import QAction, QColor, QFont
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QHBoxLayout, QHeaderView,
    QInputDialog, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QToolBar,
    QVBoxLayout, QWidget,
)

APP_NAME = "よみがえり窓"
APP_VERSION = "0.2.1"
LIST_FORMAT = 1

KIND_LABEL = {
    "snap": "Snap",
    "apt": "apt",
    "flatpak": "Flatpak",
    "github": "GitHub",
    "deb-local": "DEBファイル",
}

GITHUB_RE = re.compile(r"github\.com[/:]([\w.-]+)/([\w.-]+)")


def parse_github(text):
    """'https://github.com/owner/repo' や 'owner/repo' から 'owner/repo' を取り出す。"""
    text = (text or "").strip()
    m = GITHUB_RE.search(text)
    if m:
        repo = m.group(2)
        if repo.endswith(".git"):
            repo = repo[:-4]
        return f"{m.group(1)}/{repo}"
    m = re.fullmatch(r"([\w.-]+)/([\w.-]+)", text)
    if m:
        return text
    return ""


# root で動かす「GitHub から最新の DEB を取ってくる」小さなプログラム
GITHUB_FETCHER = r'''
import json, subprocess, sys, urllib.request, os
repo, dest = sys.argv[1], sys.argv[2]
def log(*a): print(*a, file=sys.stderr, flush=True)
req = urllib.request.Request(
    "https://api.github.com/repos/%s/releases/latest" % repo,
    headers={"Accept": "application/vnd.github+json",
             "User-Agent": "yomigaeri-mado"})
try:
    data = json.load(urllib.request.urlopen(req, timeout=30))
except Exception as e:
    log("GitHub に接続できませんでした:", repo, e); sys.exit(1)
debs = [a for a in data.get("assets", []) if a["name"].endswith(".deb")]
if not debs:
    log("最新リリースに DEB ファイルがありません:", repo); sys.exit(1)
arch = subprocess.run(["dpkg", "--print-architecture"],
                      capture_output=True, text=True).stdout.strip()
def score(a):
    n = a["name"]
    return (arch in n) * 2 + ("_all" in n or "-all" in n)
asset = max(debs, key=score)
path = os.path.join(dest, os.path.basename(asset["name"]))
log("ダウンロード:", data.get("tag_name", "?"), asset["name"])
try:
    with urllib.request.urlopen(asset["browser_download_url"], timeout=300) as r, \
            open(path, "wb") as f:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
except Exception as e:
    log("ダウンロードに失敗しました:", e); sys.exit(1)
os.chmod(path, 0o644)
print(path)
'''

# Snap の「土台」部品（アプリではないので書き出さない）
SNAP_SKIP_NOTES = ("base", "snapd", "core", "gadget", "kernel")
SNAP_SKIP_NAMES = ("bare", "snapd")

# apt で「自分で入れた」とみなさないもの（カーネルなど）
APT_SKIP_PREFIX = ("linux-image", "linux-headers", "linux-modules",
                   "linux-generic", "linux-firmware", "linux-signed")


# ---------------------------------------------------------------- 調べる処理
def run(cmd):
    """コマンドを実行して標準出力を返す。無いコマンドなら空文字。"""
    if shutil.which(cmd[0]) is None:
        return ""
    try:
        # 日本語環境だと apt の表示が翻訳されて読み取れないので英語表示で動かす
        env = dict(os.environ, LC_ALL="C.UTF-8", LANG="C.UTF-8", LANGUAGE="")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                           env=env)
        return r.stdout
    except Exception:
        return ""


def list_snaps():
    """[(name, channel, classic)] を返す。"""
    out = run(["snap", "list"])
    result = []
    for line in out.splitlines()[1:]:
        cols = line.split()
        if len(cols) < 6:
            continue
        name, tracking, notes = cols[0], cols[3], cols[5]
        if name in SNAP_SKIP_NAMES:
            continue
        if any(n in notes.split(",") for n in SNAP_SKIP_NOTES):
            continue
        channel = tracking if tracking not in ("-", "") else ""
        result.append((name, channel, "classic" in notes))
    return result


def installed_snap_names():
    out = run(["snap", "list"])
    return {l.split()[0] for l in out.splitlines()[1:] if l.split()}


def initial_packages():
    """Ubuntu を入れた直後から入っていたパッケージ名の集合。"""
    names = set()
    path = "/var/log/installer/initial-status.gz"
    try:
        with gzip.open(path, "rt", errors="ignore") as f:
            for line in f:
                if line.startswith("Package: "):
                    names.add(line[9:].strip())
    except Exception:
        pass
    return names


def local_deb_packages():
    """どのリポジトリにも無い（DEBファイルから入れた）パッケージ名。"""
    out = run(["apt", "list", "--installed"])
    names = set()
    for line in out.splitlines():
        if "local]" in line and "/" in line:
            names.add(line.split("/")[0])
    return names


def list_apt():
    """自分で入れた apt パッケージ: (repo_pkgs, local_pkgs, filtered)"""
    manual = set(run(["apt-mark", "showmanual"]).split())
    initial = initial_packages()
    filtered = bool(initial)
    mine = {p for p in manual - initial if not p.startswith(APT_SKIP_PREFIX)}
    local = local_deb_packages()
    return sorted(mine - local), sorted(mine & local), filtered


def deb_homepage(name):
    return run(["dpkg-query", "-W", "-f=${Homepage}", name]).strip()


def installed_dpkg_names():
    out = run(["dpkg-query", "-W", "-f=${Package} ${db:Status-Status}\n"])
    return {l.split()[0] for l in out.splitlines()
            if l.endswith(" installed")}


def list_flatpaks():
    """[(app_id, origin)]"""
    out = run(["flatpak", "list", "--app", "--columns=application,origin"])
    result = []
    for line in out.splitlines():
        cols = line.split()
        if len(cols) >= 2 and "." in cols[0]:
            result.append((cols[0], cols[1]))
    return result


def os_name():
    try:
        with open("/etc/os-release", encoding="utf-8") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    return "不明"


def collect_items():
    items = []
    for name, channel, classic in list_snaps():
        items.append({"kind": "snap", "name": name,
                      "channel": channel, "classic": classic})
    repo, local, filtered = list_apt()
    for name in repo:
        items.append({"kind": "apt", "name": name})
    for name in local:
        repo_ = parse_github(deb_homepage(name))
        if repo_:
            items.append({"kind": "github", "name": name, "repo": repo_})
        else:
            items.append({"kind": "deb-local", "name": name})
    for app_id, origin in list_flatpaks():
        items.append({"kind": "flatpak", "name": app_id, "origin": origin})
    return items, filtered


# ------------------------------------------------------- インストール用の台本
def build_install_script(items, fetcher_path="", work_dir=""):
    """root で1回だけ実行する bash 台本を作る。
    進み具合は ##START / ##OK / ##NG の行で知らせる。"""
    q = shlex.quote
    lines = [
        "#!/bin/bash",
        "export DEBIAN_FRONTEND=noninteractive",
        "say(){ echo \"$@\"; }",
    ]
    apts = [i for i in items if i["kind"] == "apt"]
    snaps = [i for i in items if i["kind"] == "snap"]
    flats = [i for i in items if i["kind"] == "flatpak"]
    ghs = [i for i in items if i["kind"] == "github"]

    if apts or ghs:
        lines += ["say '##INFO apt の一覧を更新しています…'", "apt-get update"]
    for i in apts:
        n = q(i["name"])
        lines += [
            f"say '##START apt' {n}",
            f"if apt-get install -y {n}; then say '##OK apt' {n};"
            f" else say '##NG apt' {n}; fi",
        ]
    for i in ghs:
        n = q(i["name"])
        lines += [
            f"say '##START github' {n}",
            f"if f=$(python3 {q(fetcher_path)} {q(i['repo'])} {q(work_dir)})"
            f' && apt-get install -y "$f"; then say \'##OK github\' {n};'
            f" else say '##NG github' {n}; fi",
        ]
    for i in snaps:
        n = q(i["name"])
        opts = ""
        if i.get("channel"):
            opts += " --channel=" + q(i["channel"])
        if i.get("classic"):
            opts += " --classic"
        lines += [
            f"say '##START snap' {n}",
            f"if snap install {n}{opts}; then say '##OK snap' {n};"
            f" else say '##NG snap' {n}; fi",
        ]
    if flats:
        lines += [
            "if ! command -v flatpak >/dev/null; then",
            "  say '##INFO Flatpak 本体を入れています…'",
            "  apt-get update && apt-get install -y flatpak",
            "fi",
            "flatpak remote-add --if-not-exists flathub "
            "https://dl.flathub.org/repo/flathub.flatpakrepo",
        ]
    for i in flats:
        n = q(i["name"])
        origin = q(i.get("origin") or "flathub")
        lines += [
            f"say '##START flatpak' {n}",
            f"if flatpak install -y --noninteractive {origin} {n};"
            f" then say '##OK flatpak' {n}; else say '##NG flatpak' {n}; fi",
        ]
    if ghs and work_dir:
        lines.append(f"rm -rf {q(work_dir)}")
    lines.append("say '##DONE'")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------- 画面
COL_CHECK, COL_KIND, COL_NAME, COL_STATE = range(4)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}")
        self.resize(900, 640)
        self.items = []          # 表に並んでいるもの
        self.row_of = {}         # (kind, name) -> 行番号
        self.proc = None
        self.script_path = None
        self.done_count = 0
        self.total_count = 0
        self.ok_count = 0
        self.ng_count = 0
        self.list_path = ""
        self.list_meta = {}
        self.dirty = False
        self.work_dir = ""

        self._build_ui()
        self._set_info("まずは「今のアプリを書き出す」か「リストを開く」を押してください。")

    # ---- 画面づくり
    def _build_ui(self):
        tb = QToolBar()
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.addToolBar(tb)

        self.act_export = QAction("① 今のアプリを書き出す", self)
        self.act_export.triggered.connect(self.export_list)
        self.act_open = QAction("② リストを開く", self)
        self.act_open.triggered.connect(self.open_list)
        self.act_install = QAction("③ 選んだものをまとめてインストール", self)
        self.act_install.triggered.connect(self.install_selected)
        self.act_retry = QAction("失敗したものをやり直す", self)
        self.act_retry.triggered.connect(self.retry_failed)
        self.act_save = QAction("リストを保存", self)
        self.act_save.triggered.connect(self.save_list)
        for a in (self.act_export, self.act_open, self.act_install,
                  self.act_retry, self.act_save):
            tb.addAction(a)
            tb.addSeparator()

        central = QWidget()
        v = QVBoxLayout(central)

        self.info = QLabel()
        self.info.setWordWrap(True)
        v.addWidget(self.info)

        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("名前でしぼりこみ…")
        self.search.textChanged.connect(self.apply_filter)
        row.addWidget(self.search, 1)
        self.kind_boxes = {}
        for k in ("snap", "apt", "flatpak", "github", "deb-local"):
            cb = QCheckBox(KIND_LABEL[k])
            cb.setChecked(True)
            cb.toggled.connect(self.apply_filter)
            self.kind_boxes[k] = cb
            row.addWidget(cb)
        b_all = QPushButton("表示中を全部選ぶ")
        b_all.clicked.connect(lambda: self.set_all_checked(True))
        b_none = QPushButton("全部はずす")
        b_none.clicked.connect(lambda: self.set_all_checked(False))
        b_gh = QPushButton("GitHub の場所を設定…")
        b_gh.setToolTip("「DEBファイル」か「GitHub」の行を選んでから押してください")
        b_gh.clicked.connect(
            lambda: self.edit_github(self.table.currentRow(), 0, ask=True))
        row.addWidget(b_all)
        row.addWidget(b_none)
        row.addWidget(b_gh)
        v.addLayout(row)

        splitter = QSplitter(Qt.Vertical)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["選ぶ", "種類", "名前", "状態"])
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(COL_CHECK, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(COL_KIND, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        h.setSectionResizeMode(COL_STATE, QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSortingEnabled(False)
        self.table.cellDoubleClicked.connect(self.edit_github)
        splitter.addWidget(self.table)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        f = QFont("Monospace")
        f.setStyleHint(QFont.TypeWriter)
        self.log.setFont(f)
        self.log.setPlaceholderText("インストール中の細かい記録がここに出ます")
        splitter.addWidget(self.log)
        splitter.setSizes([440, 160])
        v.addWidget(splitter, 1)

        self.progress = QProgressBar()
        self.progress.setFormat("%v / %m")
        self.progress.setValue(0)
        self.progress.setMaximum(1)
        v.addWidget(self.progress)

        self.setCentralWidget(central)

    def _set_info(self, text):
        self.info.setText(text)

    def _busy(self, busy):
        for a in (self.act_export, self.act_open, self.act_install,
                  self.act_retry, self.act_save):
            a.setEnabled(not busy)

    # ---- 表
    def fill_table(self, items):
        self.items = items
        self.row_of = {}
        self.table.setRowCount(0)
        inst_snap = installed_snap_names()
        inst_dpkg = installed_dpkg_names()
        inst_flat = {a for a, _ in list_flatpaks()}
        order = {"snap": 0, "flatpak": 1, "apt": 2, "github": 3, "deb-local": 4}
        items.sort(key=lambda i: (order.get(i["kind"], 9), i["name"]))
        self.table.setRowCount(len(items))
        for r, it in enumerate(items):
            self.row_of[(it["kind"], it["name"])] = r
            self.set_row(r, it, self.is_installed(
                it, (inst_snap, inst_dpkg, inst_flat)))
        self.apply_filter()

    def set_row(self, r, it, installed):
        """1行ぶんを表に書く。"""
        kind, name = it["kind"], it["name"]
        chk = QTableWidgetItem()
        if kind == "deb-local":
            chk.setFlags(Qt.ItemIsEnabled)
            state = ("インストール済み" if installed else
                     "ダブルクリックで GitHub の場所を設定")
        else:
            chk.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            chk.setCheckState(Qt.Unchecked if installed else Qt.Checked)
            state = "インストール済み" if installed else "未インストール"
        self.table.setItem(r, COL_CHECK, chk)
        self.table.setItem(r, COL_KIND, QTableWidgetItem(KIND_LABEL[kind]))
        label = name
        if kind == "snap" and it.get("classic"):
            label += "  (classic)"
        if kind == "github":
            label += f"   ← github.com/{it['repo']}"
        name_item = QTableWidgetItem(label)
        if kind in ("github", "deb-local"):
            name_item.setToolTip("ダブルクリックで GitHub の場所を変更できます")
        self.table.setItem(r, COL_NAME, name_item)
        self.set_state(r, state)

    def is_installed(self, it, inst):
        inst_snap, inst_dpkg, inst_flat = inst
        kind, name = it["kind"], it["name"]
        return ((kind == "snap" and name in inst_snap) or
                (kind in ("apt", "github", "deb-local") and name in inst_dpkg) or
                (kind == "flatpak" and name in inst_flat))

    def edit_github(self, row, _col, ask=False):
        if not (0 <= row < len(self.items)) or \
                self.items[row]["kind"] not in ("github", "deb-local"):
            if ask:
                QMessageBox.information(
                    self, APP_NAME,
                    "種類が「DEBファイル」か「GitHub」の行を1つクリックしてから押してください。")
            return
        it = self.items[row]
        text, ok = QInputDialog.getText(
            self, "GitHub の場所",
            f"「{it['name']}」を配布している GitHub の場所を入力してください。\n"
            "例: kenjiiida-commits/Soramado  または  https://github.com/…\n"
            "（空にすると設定を消します）",
            text=it.get("repo", ""))
        if not ok:
            return
        repo = parse_github(text)
        if text.strip() and not repo:
            QMessageBox.warning(self, APP_NAME,
                                "「持ち主/リポジトリ名」の形で入力してください。")
            return
        del self.row_of[(it["kind"], it["name"])]
        if repo:
            it["kind"], it["repo"] = "github", repo
        else:
            it["kind"] = "deb-local"
            it.pop("repo", None)
        self.row_of[(it["kind"], it["name"])] = row
        installed = it["name"] in installed_dpkg_names()
        self.set_row(row, it, installed)
        self.apply_filter()
        self.dirty = True
        self._set_info("GitHub の場所を変えました。忘れずに「リストを保存」してください。")

    def set_state(self, row, text):
        item = QTableWidgetItem(text)
        color = {
            "インストール済み": "#2e7d32", "完了": "#2e7d32",
            "失敗": "#c62828", "インストール中…": "#1565c0",
            "待ち": "#6d6d6d",
        }.get(text)
        if color:
            item.setForeground(QColor(color))
        if text == "インストール中…":
            item.setFont(QFont("", -1, QFont.Bold))
        self.table.setItem(row, COL_STATE, item)

    def state_of(self, row):
        it = self.table.item(row, COL_STATE)
        return it.text() if it else ""

    def apply_filter(self):
        word = self.search.text().strip().lower()
        for r, it in enumerate(self.items):
            show = self.kind_boxes[it["kind"]].isChecked() and \
                (not word or word in it["name"].lower())
            self.table.setRowHidden(r, not show)

    def set_all_checked(self, on):
        for r in range(self.table.rowCount()):
            if self.table.isRowHidden(r):
                continue
            c = self.table.item(r, COL_CHECK)
            if c.flags() & Qt.ItemIsUserCheckable:
                c.setCheckState(Qt.Checked if on else Qt.Unchecked)

    def checked_items(self):
        out = []
        for r, it in enumerate(self.items):
            c = self.table.item(r, COL_CHECK)
            if c.flags() & Qt.ItemIsUserCheckable and \
                    c.checkState() == Qt.Checked:
                out.append(it)
        return out

    # ---- ① 書き出し
    def export_list(self):
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            items, filtered = collect_items()
        finally:
            QApplication.restoreOverrideCursor()
        if not items:
            QMessageBox.warning(self, APP_NAME, "アプリが見つかりませんでした。")
            return
        default = os.path.expanduser(
            f"~/よみがえりリスト_{platform.node()}_"
            f"{datetime.date.today():%Y%m%d}.json")
        path, _ = QFileDialog.getSaveFileName(
            self, "リストの保存先（USBメモリやクラウドがおすすめ）",
            default, "よみがえりリスト (*.json)")
        if not path:
            return
        if not path.endswith(".json"):
            path += ".json"
        self.list_meta = {
            "created": datetime.datetime.now().isoformat(timespec="seconds"),
            "hostname": platform.node(),
            "os": os_name(),
        }
        self.fill_table(items)
        if not self.write_list(path):
            return
        counts = {k: sum(1 for i in items if i["kind"] == k) for k in KIND_LABEL}
        msg = (f"書き出しました：{path}\n"
               f"Snap {counts['snap']}個 / apt {counts['apt']}個 / "
               f"Flatpak {counts['flatpak']}個 / GitHub {counts['github']}個 / "
               f"DEBファイル {counts['deb-local']}個")
        if counts["deb-local"]:
            msg += ("\n「DEBファイル」の行はダブルクリックで GitHub の場所を設定すると、"
                    "次からは最新版を自動で入れられます（設定後は「リストを保存」）。")
        if not filtered:
            msg += ("\n※最初から入っていたパッケージの記録が見つからなかったため、"
                    "apt には元々入っていたものも含まれています。")
        self._set_info(msg)
        QMessageBox.information(
            self, APP_NAME,
            msg + "\n\nこのファイルを USBメモリやクラウドにコピーしておけば、"
                  "再インストール後に「② リストを開く」で使えます。")

    def write_list(self, path):
        data = {"format": LIST_FORMAT, "app": f"{APP_NAME} {APP_VERSION}"}
        data.update(self.list_meta)
        data["items"] = self.items
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            QMessageBox.critical(self, APP_NAME, f"保存できませんでした。\n{e}")
            return False
        self.list_path = path
        self.dirty = False
        return True

    def save_list(self):
        if not self.items:
            QMessageBox.information(self, APP_NAME, "保存するリストがありません。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "リストを保存", self.list_path or os.path.expanduser(
                "~/よみがえりリスト.json"), "よみがえりリスト (*.json)")
        if not path:
            return
        if not path.endswith(".json"):
            path += ".json"
        if self.write_list(path):
            self._set_info(f"保存しました：{path}")

    # ---- ② 読み込み
    def open_list(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "よみがえりリストを開く", os.path.expanduser("~"),
            "よみがえりリスト (*.json);;すべてのファイル (*)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            items = [i for i in data["items"]
                     if i.get("kind") in KIND_LABEL and i.get("name")]
        except Exception as e:
            QMessageBox.critical(self, APP_NAME,
                                 f"リストを読み込めませんでした。\n{e}")
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.fill_table(items)
        finally:
            QApplication.restoreOverrideCursor()
        self.list_path = path
        self.dirty = False
        self.list_meta = {k: data.get(k, "?")
                          for k in ("created", "hostname", "os")}
        n = len(self.checked_items())
        self._set_info(
            f"{os.path.basename(path)}（{data.get('hostname', '?')} / "
            f"{data.get('os', '?')} / {data.get('created', '?')} 作成）\n"
            f"まだ入っていない {n} 個にチェックを入れました。"
            "いらないものはチェックをはずして「③」を押してください。")

    # ---- ③ インストール
    def install_selected(self):
        targets = self.checked_items()
        if not targets:
            QMessageBox.information(self, APP_NAME, "チェックが入っているものがありません。")
            return
        ans = QMessageBox.question(
            self, APP_NAME,
            f"{len(targets)} 個をインストールします。\n"
            "このあとパスワードを1回だけ聞かれます。よろしいですか？\n\n"
            "（数が多いと時間がかかります。途中でパソコンを切らないでください）")
        if ans != QMessageBox.Yes:
            return
        self.start_install(targets)

    def retry_failed(self):
        targets = [it for r, it in enumerate(self.items)
                   if self.state_of(r) == "失敗"]
        if not targets:
            QMessageBox.information(self, APP_NAME, "失敗したものはありません。")
            return
        self.start_install(targets)

    def start_install(self, targets):
        if shutil.which("pkexec") is None:
            QMessageBox.critical(self, APP_NAME,
                                 "pkexec が見つかりません（policykit-1 が必要です）。")
            return
        work_dir = tempfile.mkdtemp(prefix="yomigaeri_")
        fetcher = os.path.join(work_dir, "fetch_github_deb.py")
        with open(fetcher, "w", encoding="utf-8") as f:
            f.write(GITHUB_FETCHER)
        self.work_dir = work_dir
        self.script_path = os.path.join(work_dir, "install.sh")
        with open(self.script_path, "w", encoding="utf-8") as f:
            f.write(build_install_script(targets, fetcher, work_dir))
        os.chmod(self.script_path, 0o755)

        for it in targets:
            self.set_state(self.row_of[(it["kind"], it["name"])], "待ち")
        self.done_count = 0
        self.total_count = len(targets)
        self.progress.setMaximum(self.total_count)
        self.progress.setValue(0)
        self.ok_count = 0
        self.ng_count = 0
        self.log.clear()
        self._busy(True)
        self._set_info("インストールしています…（パスワードの画面が出たら入力してください）")

        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self.on_output)
        self.proc.finished.connect(self.on_finished)
        self._buf = ""
        self.proc.start("pkexec", ["/bin/bash", self.script_path])

    def on_output(self):
        self._buf += bytes(self.proc.readAllStandardOutput()).decode(
            "utf-8", errors="replace")
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self.handle_line(line.rstrip("\r"))

    def handle_line(self, line):
        if not line.startswith("##"):
            if line.strip():
                self.log.appendPlainText(line)
            return
        parts = line[2:].split(" ", 2)
        tag = parts[0]
        if tag == "INFO":
            self._set_info(line[7:])
            self.log.appendPlainText("▶ " + line[7:])
            return
        if tag == "DONE" or len(parts) < 3:
            return
        kind, name = parts[1], parts[2]
        row = self.row_of.get((kind, name))
        if row is None:
            return
        if tag == "START":
            self.set_state(row, "インストール中…")
            self.table.scrollToItem(self.table.item(row, COL_NAME))
            self._set_info(f"インストール中：{name}"
                           f"（{self.done_count + 1} / {self.total_count}）")
            self.log.appendPlainText(f"▶ {name} をインストールしています")
        elif tag in ("OK", "NG"):
            ok = tag == "OK"
            self.set_state(row, "完了" if ok else "失敗")
            chk = self.table.item(row, COL_CHECK)
            chk.setCheckState(Qt.Unchecked if ok else Qt.Checked)
            self.done_count += 1
            if ok:
                self.ok_count += 1
            else:
                self.ng_count += 1
            self.progress.setValue(self.done_count)

    def on_finished(self, code, _status):
        self.on_output()
        self._busy(False)
        shutil.rmtree(self.work_dir, ignore_errors=True)
        if code in (126, 127) and self.done_count == 0:
            self._set_info("パスワード入力がキャンセルされたので、何もしませんでした。")
            for it in self.items:
                r = self.row_of[(it["kind"], it["name"])]
                if self.state_of(r) == "待ち":
                    self.set_state(r, "未インストール")
            return
        msg = f"終わりました。成功 {self.ok_count} 個 / 失敗 {self.ng_count} 個"
        if self.ng_count:
            msg += ("\n失敗したものは「失敗したものをやり直す」で再挑戦できます。"
                    "何度も失敗する場合は下の記録を見てください"
                    "（PPA など追加の入手先が必要なソフトの可能性があります）。")
        self._set_info(msg)
        QMessageBox.information(self, APP_NAME, msg)

    def closeEvent(self, ev):
        if self.proc and self.proc.state() != QProcess.NotRunning:
            QMessageBox.warning(self, APP_NAME,
                                "インストール中です。終わるまで待ってください。")
            ev.ignore()
            return
        if self.dirty:
            ans = QMessageBox.question(
                self, APP_NAME, "リストの変更が保存されていません。保存しますか？",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
            if ans == QMessageBox.Cancel:
                ev.ignore()
                return
            if ans == QMessageBox.Save:
                self.save_list()
        ev.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName("yomigaeri-mado")
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
