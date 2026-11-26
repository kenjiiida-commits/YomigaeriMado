#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
よみがえり窓 (Yomigaeri-mado) v0.3.0
Ubuntu の再インストール後に、前のアプリをまとめて入れ直すためのソフト。

  1. 今のパソコンに入っているアプリ（Snap / apt / Flatpak）をリストに書き出す
  2. 再インストール後にリストを開き、チェックを入れて選ぶ
  3. 「まとめてインストール」でパスワード1回だけで一括インストール
  4. GitHub で配布されている DEB アプリは、最新版を自動で取ってきて入れる
  5. 定番カタログ・目的別セットから、入っていない定番ソフトを選んで入れる
  6. リストの自動書き出し（毎日 / 毎週）と「これ1つで復元」スクリプト
  7. インストール後の設定（Mozc を入力ソースに追加 など）

コマンドで使う場合:
  yomigaeri-mado --export フォルダ   リストと復元スクリプトを書き出す（画面なし）
  yomigaeri-mado --open リスト.json  リストを開いた状態で起動

License: MIT  (c) 2026 飯田顕治
"""

import argparse
import ast
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

APP_NAME = "よみがえり窓"
APP_VERSION = "0.3.0"
LIST_FORMAT = 1
HOME_REPO = "kenjiiida-commits/YomigaeriMado"

HERE = os.path.dirname(os.path.abspath(__file__))
CATALOG_PATH = os.path.join(HERE, "catalog.json")
CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "yomigaeri-mado")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
SYSTEMD_DIR = os.path.expanduser("~/.config/systemd/user")
UNIT = "yomigaeri-mado-export"
RESTORE_SCRIPT = "よみがえり.sh"

KIND_LABEL = {
    "snap": "Snap",
    "apt": "apt",
    "flatpak": "Flatpak",
    "github": "GitHub",
    "deb-local": "DEBファイル",
}

# Snap の「土台」部品（アプリではないので書き出さない）
SNAP_SKIP_NOTES = ("base", "snapd", "core", "gadget", "kernel")
SNAP_SKIP_NAMES = ("bare", "snapd")

# apt で「自分で入れた」とみなさないもの（カーネルなど）
APT_SKIP_PREFIX = ("linux-image", "linux-headers", "linux-modules",
                   "linux-generic", "linux-firmware", "linux-signed")

# ---- インストール前後の作業（パッケージごと）
# root で、インストールの前に実行するもの
PRE_ROOT = {
    ("apt", "ubuntu-restricted-extras"): [
        "echo ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula"
        " select true | debconf-set-selections",
    ],
}
# root で、インストールに成功した後に実行するもの
POST_ROOT = {
    ("apt", "flatpak"): [
        "flatpak remote-add --if-not-exists flathub "
        "https://dl.flathub.org/repo/flathub.flatpakrepo",
    ],
}
# 自分（ユーザー）の設定として、全部終わった後に行うもの
POST_USER = {
    ("apt", "ibus-mozc"): "mozc_input",
}
POST_USER_LABEL = {
    "mozc_input": "Mozc を入力ソースに追加",
}


# ------------------------------------------------------------------ 設定
def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


# ---------------------------------------------------------------- 調べる処理
def run(cmd, timeout=120):
    """コマンドを実行して標準出力を返す。無いコマンドなら空文字。"""
    if shutil.which(cmd[0]) is None:
        return ""
    try:
        # 日本語環境だと apt の表示が翻訳されて読み取れないので英語表示で動かす
        env = dict(os.environ, LC_ALL="C.UTF-8", LANG="C.UTF-8", LANGUAGE="")
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, env=env)
        return r.stdout
    except Exception:
        return ""


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
    if re.fullmatch(r"([\w.-]+)/([\w.-]+)", text):
        return text
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
    try:
        with gzip.open("/var/log/installer/initial-status.gz", "rt",
                       errors="ignore") as f:
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
    mine = {p for p in manual - initial if not p.startswith(APT_SKIP_PREFIX)}
    local = local_deb_packages()
    return sorted(mine - local), sorted(mine & local), bool(initial)


def deb_homepage(name):
    return run(["dpkg-query", "-W", "-f=${Homepage}", name]).strip()


def installed_dpkg_names():
    out = run(["dpkg-query", "-W", "-f=${Package} ${db:Status-Status}\n"])
    return {l.split()[0] for l in out.splitlines() if l.endswith(" installed")}


def list_flatpaks():
    """[(app_id, origin)]"""
    out = run(["flatpak", "list", "--app", "--columns=application,origin"])
    result = []
    for line in out.splitlines():
        cols = line.split()
        if len(cols) >= 2 and "." in cols[0]:
            result.append((cols[0], cols[1]))
    return result


def installed_sets():
    return (installed_snap_names(), installed_dpkg_names(),
            {a for a, _ in list_flatpaks()})


def is_installed(it, inst):
    inst_snap, inst_dpkg, inst_flat = inst
    kind, name = it["kind"], it["name"]
    return ((kind == "snap" and name in inst_snap) or
            (kind in ("apt", "github", "deb-local") and name in inst_dpkg) or
            (kind == "flatpak" and name in inst_flat))


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
    """今のパソコンのアプリ一覧。GitHub の場所は設定ファイルの記録も使う。"""
    gh_map = load_config().get("github_map", {})
    items = []
    for name, channel, classic in list_snaps():
        items.append({"kind": "snap", "name": name,
                      "channel": channel, "classic": classic})
    repo, local, filtered = list_apt()
    for name in repo:
        items.append({"kind": "apt", "name": name})
    for name in local:
        gh = gh_map.get(name) or parse_github(deb_homepage(name))
        if gh:
            items.append({"kind": "github", "name": name, "repo": gh})
        else:
            items.append({"kind": "deb-local", "name": name})
    for app_id, origin in list_flatpaks():
        items.append({"kind": "flatpak", "name": app_id, "origin": origin})
    return items, filtered


def load_catalog():
    with open(CATALOG_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return [i for i in data["items"]
            if i.get("kind") in KIND_LABEL and i.get("name")]


# ------------------------------------------------------- 書き出し・復元スクリプト
RESTORE_TEMPLATE = r'''#!/bin/bash
# よみがえり窓 復元スクリプト（@CREATED@ 作成）
#
# まっさらな Ubuntu で、このファイルがあるフォルダを開いて
#     bash よみがえり.sh
# と実行すると、よみがえり窓をインストールして、
# 同じフォルダのリスト「@LIST@」を開きます。

DIR="$(cd "$(dirname "$0")" && pwd)"
LIST="$DIR/@LIST@"

if [ ! -f "$LIST" ]; then
  echo "リストが見つかりません: $LIST"
  exit 1
fi

if ! command -v yomigaeri-mado >/dev/null 2>&1; then
  echo "よみがえり窓をダウンロードしています…"
  TMP="$(mktemp -d)"
  chmod 755 "$TMP"
  DEB="$(python3 - "$TMP" <<'PY'
import json, os, sys, urllib.request
d = sys.argv[1]
req = urllib.request.Request(
    "https://api.github.com/repos/@REPO@/releases/latest",
    headers={"User-Agent": "yomigaeri-mado"})
data = json.load(urllib.request.urlopen(req, timeout=30))
asset = [a for a in data["assets"] if a["name"].endswith(".deb")][0]
path = os.path.join(d, asset["name"])
urllib.request.urlretrieve(asset["browser_download_url"], path)
os.chmod(path, 0o644)
print(path)
PY
)"
  if [ -z "$DEB" ] || [ ! -f "$DEB" ]; then
    echo "ダウンロードに失敗しました。インターネットにつながっているか確認してください。"
    exit 1
  fi
  echo "インストールします（パスワードを聞かれます）"
  sudo apt-get update && sudo apt-get install -y "$DEB" || {
    echo "インストールに失敗しました。"
    exit 1
  }
fi

nohup yomigaeri-mado --open "$LIST" >/dev/null 2>&1 &
echo "よみがえり窓を開きました。画面の「③」を押すと、まとめてインストールが始まります。"
'''


def write_restore_script(folder, list_name):
    path = os.path.join(folder, RESTORE_SCRIPT)
    text = (RESTORE_TEMPLATE
            .replace("@LIST@", list_name)
            .replace("@REPO@", HOME_REPO)
            .replace("@CREATED@", datetime.datetime.now()
                     .strftime("%Y-%m-%d %H:%M")))
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        os.chmod(path, 0o755)
    except Exception:
        pass   # USB メモリ（FAT）などでは実行属性を付けられない
    return path


def write_list_file(path, items, meta):
    data = {"format": LIST_FORMAT, "app": f"{APP_NAME} {APP_VERSION}"}
    data.update(meta)
    data["items"] = items
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    write_restore_script(os.path.dirname(os.path.abspath(path)),
                         os.path.basename(path))


def new_meta():
    return {
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "hostname": platform.node(),
        "os": os_name(),
    }


def auto_list_name():
    return f"よみがえりリスト_{platform.node()}.json"


def cli_export(folder):
    """画面なしの書き出し（自動書き出しから呼ばれる）。"""
    cfg = load_config()
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        if not os.path.isdir(folder):
            raise RuntimeError(f"保存先が見つかりません（USB を外していませんか？）: {folder}")
        items, _ = collect_items()
        path = os.path.join(folder, auto_list_name())
        write_list_file(path, items, new_meta())
        cfg["last_export"] = f"{stamp} 成功（{len(items)} 個）"
        save_config(cfg)
        print(f"書き出しました: {path}")
        return 0
    except Exception as e:
        cfg["last_export"] = f"{stamp} 失敗: {e}"
        save_config(cfg)
        print(f"書き出しに失敗しました: {e}", file=sys.stderr)
        return 1


# ------------------------------------------------------------ 自動書き出し
def launcher_command():
    """systemd から自分を起動するためのコマンド。"""
    exe = shutil.which("yomigaeri-mado")
    if exe and os.path.realpath(exe).startswith("/usr/"):
        return [exe]
    return [sys.executable, os.path.abspath(__file__)]


def systemd_quote(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') \
        .replace("%", "%%") + '"'


def set_auto_export(enabled, folder="", period="weekly"):
    """systemd のユーザータイマーを作る／止める。(成功, メッセージ)"""
    if shutil.which("systemctl") is None:
        return False, "systemctl が見つかりません。"
    service = os.path.join(SYSTEMD_DIR, UNIT + ".service")
    timer = os.path.join(SYSTEMD_DIR, UNIT + ".timer")
    if not enabled:
        subprocess.run(["systemctl", "--user", "disable", "--now",
                        UNIT + ".timer"], capture_output=True)
        for p in (service, timer):
            if os.path.exists(p):
                os.remove(p)
        subprocess.run(["systemctl", "--user", "daemon-reload"],
                       capture_output=True)
        return True, "自動書き出しを止めました。"
    os.makedirs(SYSTEMD_DIR, exist_ok=True)
    cmd = " ".join(systemd_quote(c) for c in launcher_command())
    with open(service, "w", encoding="utf-8") as f:
        f.write("[Unit]\n"
                "Description=Yomigaeri-mado automatic list export\n\n"
                "[Service]\n"
                "Type=oneshot\n"
                f"ExecStart={cmd} --export {systemd_quote(folder)}\n")
    with open(timer, "w", encoding="utf-8") as f:
        f.write("[Unit]\n"
                "Description=Yomigaeri-mado automatic list export\n\n"
                "[Timer]\n"
                f"OnCalendar={'daily' if period == 'daily' else 'weekly'}\n"
                "Persistent=true\n"
                "RandomizedDelaySec=10min\n\n"
                "[Install]\n"
                "WantedBy=timers.target\n")
    subprocess.run(["systemctl", "--user", "daemon-reload"],
                   capture_output=True)
    r = subprocess.run(["systemctl", "--user", "enable", "--now",
                        UNIT + ".timer"], capture_output=True, text=True)
    if r.returncode != 0:
        return False, "タイマーを登録できませんでした。\n" + r.stderr
    return True, "自動書き出しを設定しました。"


def next_auto_export():
    out = run(["systemctl", "--user", "show", UNIT + ".timer",
               "-p", "NextElapseUSecRealtime", "--value"]).strip()
    return out if out and out != "n/a" else ""


# ------------------------------------------------- インストール後の設定（ユーザー）
def mozc_input():
    """GNOME の入力ソースに Mozc を追加する（すでにあれば何もしない）。"""
    key = ["org.gnome.desktop.input-sources", "sources"]
    cur = run(["gsettings", "get"] + key).strip()
    if not cur:
        return False, "gsettings が使えません"
    if cur.startswith("@a(ss)"):
        cur = cur[len("@a(ss)"):].strip()
    try:
        sources = list(ast.literal_eval(cur))
    except Exception:
        return False, f"入力ソースを読み取れません: {cur}"
    if ("ibus", "mozc-jp") in sources:
        return True, "すでに追加されています"
    sources.append(("ibus", "mozc-jp"))
    val = "[" + ", ".join(f"('{a}', '{b}')" for a, b in sources) + "]"
    r = subprocess.run(["gsettings", "set"] + key + [val],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return False, r.stderr.strip()
    return True, "追加しました（反映されないときはログインし直してください）"


USER_ACTIONS = {"mozc_input": mozc_input}


# ------------------------------------------------------- インストール用の台本
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


def build_install_script(items, fetcher_path="", work_dir=""):
    """root で1回だけ実行する bash 台本を作る。
    進み具合は ##START / ##OK / ##NG の行で知らせる。"""
    q = shlex.quote
    lines = [
        "#!/bin/bash",
        "export DEBIAN_FRONTEND=noninteractive",
        "say(){ echo \"$@\"; }",
    ]

    def step(kind, name, install_cmd):
        key = (kind, name)
        n = q(name)
        pre = PRE_ROOT.get(key, [])
        post = POST_ROOT.get(key, [])
        body = "; ".join(pre + [""]) if pre else ""
        ok = f"say '##OK {kind}' {n}"
        if post:
            ok = "; ".join(post + [ok])
        return [
            f"say '##START {kind}' {n}",
            f"{body}if {install_cmd}; then {ok}; else say '##NG {kind}' {n}; fi",
        ]

    apts = [i for i in items if i["kind"] == "apt"]
    ghs = [i for i in items if i["kind"] == "github"]
    snaps = [i for i in items if i["kind"] == "snap"]
    flats = [i for i in items if i["kind"] == "flatpak"]

    if apts or ghs:
        lines += ["say '##INFO apt の一覧を更新しています…'", "apt-get update"]
    for i in apts:
        lines += step("apt", i["name"], f"apt-get install -y {q(i['name'])}")
    for i in ghs:
        lines += step("github", i["name"],
                      f"f=$(python3 {q(fetcher_path)} {q(i['repo'])} "
                      f"{q(work_dir)}) && apt-get install -y \"$f\"")
    for i in snaps:
        opts = ""
        if i.get("channel"):
            opts += " --channel=" + q(i["channel"])
        if i.get("classic"):
            opts += " --classic"
        lines += step("snap", i["name"], f"snap install {q(i['name'])}{opts}")
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
        origin = q(i.get("origin") or "flathub")
        lines += step("flatpak", i["name"],
                      f"flatpak install -y --noninteractive {origin} "
                      f"{q(i['name'])}")
    lines.append("say '##DONE'")
    return "\n".join(lines) + "\n"


# ====================================================================== 画面
def run_gui(open_path=None):
    from PySide6.QtCore import QProcess, Qt
    from PySide6.QtGui import QAction, QColor, QFont
    from PySide6.QtWidgets import (
        QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
        QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QInputDialog,
        QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
        QProgressBar, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
        QToolBar, QVBoxLayout, QWidget,
    )

    COL_CHECK, COL_KIND, COL_NAME, COL_DESC, COL_STATE = range(5)
    SET_HINT = "セットで選ぶ…"

    class AutoExportDialog(QDialog):
        def __init__(self, parent):
            super().__init__(parent)
            self.setWindowTitle("自動書き出しの設定")
            cfg = load_config().get("auto_export", {})
            form = QFormLayout(self)
            self.enable = QCheckBox("リストを自動で書き出す")
            self.enable.setChecked(cfg.get("enabled", False))
            form.addRow(self.enable)
            row = QHBoxLayout()
            self.folder = QLineEdit(cfg.get("folder", ""))
            self.folder.setPlaceholderText("クラウドや USB のフォルダがおすすめ")
            b = QPushButton("選ぶ…")
            b.clicked.connect(self.pick)
            row.addWidget(self.folder, 1)
            row.addWidget(b)
            form.addRow("保存先", row)
            self.period = QComboBox()
            self.period.addItem("毎週", "weekly")
            self.period.addItem("毎日", "daily")
            self.period.setCurrentIndex(
                1 if cfg.get("period") == "daily" else 0)
            form.addRow("間隔", self.period)
            last = load_config().get("last_export", "まだありません")
            nxt = next_auto_export() if cfg.get("enabled") else ""
            info = QLabel(f"前回: {last}" + (f"\n次回: {nxt}" if nxt else ""))
            info.setWordWrap(True)
            form.addRow(info)
            note = QLabel(
                "保存先には「" + auto_list_name() + "」と復元スクリプト"
                f"「{RESTORE_SCRIPT}」が上書き保存されます。\n"
                "パソコンが切れていた日は、次に起動したときに書き出します。")
            note.setWordWrap(True)
            form.addRow(note)
            btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            btns.accepted.connect(self.accept)
            btns.rejected.connect(self.reject)
            form.addRow(btns)
            self.resize(520, 0)

        def pick(self):
            d = QFileDialog.getExistingDirectory(
                self, "保存先フォルダ", self.folder.text() or
                os.path.expanduser("~"))
            if d:
                self.folder.setText(d)

        def accept(self):
            on = self.enable.isChecked()
            folder = self.folder.text().strip()
            if on and not os.path.isdir(folder):
                QMessageBox.warning(self, APP_NAME, "保存先のフォルダを選んでください。")
                return
            ok, msg = set_auto_export(on, folder, self.period.currentData())
            if not ok:
                QMessageBox.critical(self, APP_NAME, msg)
                return
            cfg = load_config()
            cfg["auto_export"] = {"enabled": on, "folder": folder,
                                  "period": self.period.currentData()}
            save_config(cfg)
            if on:
                rc = cli_export(folder)
                msg += ("\n今すぐ1回書き出しました。" if rc == 0 else
                        "\n試しの書き出しに失敗しました: " +
                        load_config().get("last_export", ""))
            QMessageBox.information(self, APP_NAME, msg)
            super().accept()

    class MainWindow(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}")
            self.resize(1000, 680)
            self.items = []
            self.row_of = {}
            self.mode = "list"       # list / catalog
            self.proc = None
            self.work_dir = ""
            self.done_count = self.total_count = 0
            self.ok_count = self.ng_count = 0
            self.succeeded = []
            self.list_path = ""
            self.list_meta = {}
            self.dirty = False
            self._buf = ""
            self._build_ui()
            self._set_info("「① 今のアプリを書き出す」「② リストを開く」"
                           "「定番カタログ」のどれかから始めてください。")

        # ---- 画面づくり
        def _build_ui(self):
            tb = QToolBar()
            tb.setMovable(False)
            self.addToolBar(tb)
            self.acts = []

            def act(text, slot):
                a = QAction(text, self)
                a.triggered.connect(slot)
                tb.addAction(a)
                tb.addSeparator()
                self.acts.append(a)
                return a

            act("① 今のアプリを書き出す", self.export_list)
            act("② リストを開く", self.open_list_dialog)
            act("③ 選んだものをまとめてインストール", self.install_selected)
            act("失敗したものをやり直す", self.retry_failed)
            act("定番カタログ", self.open_catalog)
            act("リストを保存", self.save_list)
            act("自動書き出し…", self.auto_export_dialog)

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
            v.addLayout(row)

            row2 = QHBoxLayout()
            self.set_combo = QComboBox()
            self.set_combo.addItem(SET_HINT)
            self.set_combo.activated.connect(self.apply_set)
            row2.addWidget(self.set_combo)
            b_save_set = QPushButton("チェックをセットとして保存…")
            b_save_set.clicked.connect(self.save_set)
            row2.addWidget(b_save_set)
            row2.addStretch(1)
            for text, slot in (
                    ("表示中を全部選ぶ", lambda: self.set_all_checked(True)),
                    ("全部はずす", lambda: self.set_all_checked(False)),
                    ("GitHub の場所を設定…",
                     lambda: self.edit_github(self.table.currentRow(), 0, True))):
                b = QPushButton(text)
                b.clicked.connect(slot)
                row2.addWidget(b)
            v.addLayout(row2)

            splitter = QSplitter(Qt.Vertical)
            self.table = QTableWidget(0, 5)
            self.table.setHorizontalHeaderLabels(
                ["選ぶ", "種類", "名前", "説明", "状態"])
            h = self.table.horizontalHeader()
            for c, mode in ((COL_CHECK, QHeaderView.ResizeToContents),
                            (COL_KIND, QHeaderView.ResizeToContents),
                            (COL_NAME, QHeaderView.Interactive),
                            (COL_DESC, QHeaderView.Stretch),
                            (COL_STATE, QHeaderView.ResizeToContents)):
                h.setSectionResizeMode(c, mode)
            self.table.setColumnWidth(COL_NAME, 320)
            self.table.verticalHeader().setVisible(False)
            self.table.setSelectionBehavior(QTableWidget.SelectRows)
            self.table.setEditTriggers(QTableWidget.NoEditTriggers)
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
            splitter.setSizes([460, 160])
            v.addWidget(splitter, 1)

            self.progress = QProgressBar()
            self.progress.setFormat("%v / %m")
            self.progress.setMaximum(1)
            self.progress.setValue(0)
            v.addWidget(self.progress)
            self.setCentralWidget(central)

        def _set_info(self, text):
            self.info.setText(text)

        def _busy(self, busy):
            for a in self.acts:
                a.setEnabled(not busy)

        # ---- 表
        def fill_table(self, items, mode="list"):
            self.mode = mode
            self.items = items
            self.row_of = {}
            inst = installed_sets()
            if mode == "catalog":
                cats = []
                for i in items:
                    if i.get("category") not in cats:
                        cats.append(i.get("category"))
                items.sort(key=lambda i: cats.index(i.get("category")))
            else:
                order = {"snap": 0, "flatpak": 1, "apt": 2, "github": 3,
                         "deb-local": 4}
                items.sort(key=lambda i: (order.get(i["kind"], 9), i["name"]))
            self.table.setRowCount(len(items))
            for r, it in enumerate(items):
                self.row_of[(it["kind"], it["name"])] = r
                self.set_row(r, it, is_installed(it, inst))
            self.refresh_sets()
            self.apply_filter()

        def set_row(self, r, it, installed):
            kind, name = it["kind"], it["name"]
            chk = QTableWidgetItem()
            if kind == "deb-local":
                chk.setFlags(Qt.ItemIsEnabled)
                state = ("インストール済み" if installed else
                         "GitHub の場所を設定してください")
            else:
                chk.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                if self.mode == "catalog":
                    chk.setCheckState(Qt.Unchecked)
                else:
                    chk.setCheckState(Qt.Unchecked if installed else Qt.Checked)
                state = "インストール済み" if installed else "未インストール"
            self.table.setItem(r, COL_CHECK, chk)
            self.table.setItem(r, COL_KIND, QTableWidgetItem(KIND_LABEL[kind]))
            label = it.get("label") or name
            if it.get("label"):
                label += f"  ({name})"
            if kind == "snap" and it.get("classic"):
                label += "  [classic]"
            if kind == "github":
                label += f"   ← github.com/{it['repo']}"
            name_item = QTableWidgetItem(label)
            if kind in ("github", "deb-local"):
                name_item.setToolTip("ダブルクリックで GitHub の場所を変更できます")
            self.table.setItem(r, COL_NAME, name_item)
            desc = it.get("desc", "")
            act = POST_USER.get((kind, name))
            if act:
                desc += ("　" if desc else "") + f"［後で {POST_USER_LABEL[act]}］"
            d = QTableWidgetItem(desc)
            d.setToolTip(desc)
            self.table.setItem(r, COL_DESC, d)
            self.set_state(r, state)

        def set_state(self, row, text):
            item = QTableWidgetItem(text)
            color = {"インストール済み": "#2e7d32", "完了": "#2e7d32",
                     "失敗": "#c62828", "インストール中…": "#1565c0",
                     "待ち": "#6d6d6d"}.get(text)
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
                text = (it["name"] + " " + it.get("label", "") + " " +
                        it.get("desc", "")).lower()
                show = self.kind_boxes[it["kind"]].isChecked() and \
                    (not word or word in text)
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

        # ---- セット
        def refresh_sets(self):
            self.set_combo.clear()
            self.set_combo.addItem(SET_HINT)
            names = []
            for it in self.items:
                for s in it.get("sets", []):
                    if s not in names:
                        names.append(s)
            for s in names:
                self.set_combo.addItem(f"{s}（カタログ）", ("catalog", s))
            for s in load_config().get("custom_sets", {}):
                self.set_combo.addItem(f"{s}（自分のセット）", ("custom", s))

        def apply_set(self, index):
            data = self.set_combo.itemData(index)
            self.set_combo.setCurrentIndex(0)
            if not data:
                return
            kind, name = data
            if kind == "catalog":
                keys = {(i["kind"], i["name"]) for i in self.items
                        if name in i.get("sets", [])}
            else:
                members = load_config().get("custom_sets", {}).get(name, [])
                # 表にないものは足す
                have = set(self.row_of)
                extra = [dict(m) for m in members
                         if (m["kind"], m["name"]) not in have]
                if extra:
                    checked = {(i["kind"], i["name"])
                               for i in self.checked_items()}
                    self.fill_table(self.items + extra, self.mode)
                    for k in checked:
                        self._check(k, True)
                keys = {(m["kind"], m["name"]) for m in members}
            n = 0
            for k in keys:
                r = self.row_of.get(k)
                if r is not None and self.state_of(r) != "インストール済み":
                    self._check(k, True)
                    n += 1
            self._set_info(f"セット「{name}」のうち、まだ入っていない {n} 個に"
                           "チェックを入れました。")

        def _check(self, key, on):
            r = self.row_of.get(key)
            if r is None:
                return
            c = self.table.item(r, COL_CHECK)
            if c.flags() & Qt.ItemIsUserCheckable:
                c.setCheckState(Qt.Checked if on else Qt.Unchecked)

        def save_set(self):
            items = self.checked_items()
            if not items:
                QMessageBox.information(self, APP_NAME,
                                        "チェックが入っているものがありません。")
                return
            name, ok = QInputDialog.getText(
                self, "セットとして保存",
                f"チェックした {len(items)} 個をセットにします。名前を入れてください。")
            name = name.strip()
            if not ok or not name:
                return
            keep = ("kind", "name", "channel", "classic", "origin", "repo",
                    "label", "desc")
            cfg = load_config()
            cfg.setdefault("custom_sets", {})[name] = [
                {k: i[k] for k in keep if k in i} for i in items]
            save_config(cfg)
            self.refresh_sets()
            self._set_info(f"セット「{name}」を保存しました。"
                           "「セットで選ぶ」からいつでも呼び出せます。")

        # ---- GitHub の場所
        def edit_github(self, row, _col, ask=False):
            if not (0 <= row < len(self.items)) or \
                    self.items[row]["kind"] not in ("github", "deb-local"):
                if ask:
                    QMessageBox.information(
                        self, APP_NAME, "種類が「DEBファイル」か「GitHub」の行を"
                                        "1つクリックしてから押してください。")
                return
            it = self.items[row]
            text, ok = QInputDialog.getText(
                self, "GitHub の場所",
                f"「{it['name']}」を配布している GitHub の場所を入力してください。\n"
                "例: kenjiiida-commits/Soramado  または  https://github.com/…\n"
                "（空にすると設定を消します）", text=it.get("repo", ""))
            if not ok:
                return
            repo = parse_github(text)
            if text.strip() and not repo:
                QMessageBox.warning(self, APP_NAME,
                                    "「持ち主/リポジトリ名」の形で入力してください。")
                return
            del self.row_of[(it["kind"], it["name"])]
            cfg = load_config()
            gh_map = cfg.setdefault("github_map", {})
            if repo:
                it["kind"], it["repo"] = "github", repo
                gh_map[it["name"]] = repo
            else:
                it["kind"] = "deb-local"
                it.pop("repo", None)
                gh_map.pop(it["name"], None)
            save_config(cfg)     # 自動書き出しでも使えるように覚えておく
            self.row_of[(it["kind"], it["name"])] = row
            self.set_row(row, it, it["name"] in installed_dpkg_names())
            self.apply_filter()
            if self.mode == "list":
                self.dirty = True
                self._set_info("GitHub の場所を変えました（次からの書き出しにも使います）。"
                               "このリストにも残すには「リストを保存」してください。")

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
            self.list_meta = new_meta()
            self.fill_table(items, "list")
            if not self.write_list(path):
                return
            counts = {k: sum(1 for i in items if i["kind"] == k)
                      for k in KIND_LABEL}
            msg = (f"書き出しました：{path}\n"
                   f"Snap {counts['snap']}個 / apt {counts['apt']}個 / "
                   f"Flatpak {counts['flatpak']}個 / GitHub {counts['github']}個 / "
                   f"DEBファイル {counts['deb-local']}個\n"
                   f"同じフォルダに復元スクリプト「{RESTORE_SCRIPT}」も作りました。")
            if not filtered:
                msg += ("\n※最初から入っていたパッケージの記録が見つからなかったため、"
                        "apt には元々入っていたものも含まれています。")
            self._set_info(msg)
            QMessageBox.information(
                self, APP_NAME,
                msg + "\n\nリストと復元スクリプトを USBメモリやクラウドにコピーしておけば、"
                      f"再インストール後に「bash {RESTORE_SCRIPT}」だけで元に戻せます。")

        def write_list(self, path):
            try:
                write_list_file(path, self.items, self.list_meta or new_meta())
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
            if self.mode == "catalog":
                items = self.checked_items()
                if not items:
                    QMessageBox.information(
                        self, APP_NAME, "カタログでは、チェックしたものだけを"
                                        "リストとして保存します。チェックを入れてください。")
                    return
            path, _ = QFileDialog.getSaveFileName(
                self, "リストを保存", self.list_path or os.path.expanduser(
                    "~/よみがえりリスト.json"), "よみがえりリスト (*.json)")
            if not path:
                return
            if not path.endswith(".json"):
                path += ".json"
            if self.mode == "catalog":
                try:
                    write_list_file(path, self.checked_items(), new_meta())
                except Exception as e:
                    QMessageBox.critical(self, APP_NAME, f"保存できませんでした。\n{e}")
                    return
                self._set_info(f"チェックしたものを保存しました：{path}")
            elif self.write_list(path):
                self._set_info(f"保存しました：{path}（復元スクリプトも更新しました）")

        # ---- ② 読み込み
        def open_list_dialog(self):
            path, _ = QFileDialog.getOpenFileName(
                self, "よみがえりリストを開く", os.path.expanduser("~"),
                "よみがえりリスト (*.json);;すべてのファイル (*)")
            if path:
                self.open_list(path)

        def open_list(self, path):
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
                self.fill_table(items, "list")
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

        # ---- 定番カタログ
        def open_catalog(self):
            try:
                items = load_catalog()
            except Exception as e:
                QMessageBox.critical(self, APP_NAME,
                                     f"カタログを読み込めませんでした。\n{e}")
                return
            if self.dirty and not self.confirm_discard():
                return
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                self.fill_table(items, "catalog")
            finally:
                QApplication.restoreOverrideCursor()
            self.list_path = ""
            n = sum(1 for r in range(len(items))
                    if self.state_of(r) != "インストール済み")
            self._set_info(
                f"定番カタログ：{len(items)} 個のうち {n} 個がまだ入っていません。\n"
                "ほしいものにチェックを入れるか、「セットで選ぶ」からまとめて選んで、"
                "「③」を押してください。")

        def confirm_discard(self):
            ans = QMessageBox.question(
                self, APP_NAME, "リストの変更が保存されていません。保存しますか？",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
            if ans == QMessageBox.Cancel:
                return False
            if ans == QMessageBox.Save:
                self.save_list()
            self.dirty = False
            return True

        # ---- 自動書き出し
        def auto_export_dialog(self):
            AutoExportDialog(self).exec()

        # ---- ③ インストール
        def install_selected(self):
            targets = self.checked_items()
            if not targets:
                QMessageBox.information(self, APP_NAME,
                                        "チェックが入っているものがありません。")
                return
            extra = ""
            acts = {POST_USER[k] for k in
                    ((i["kind"], i["name"]) for i in targets) if k in POST_USER}
            if acts:
                extra = "\n\n終わったあとに次の設定もします：\n・" + \
                    "\n・".join(POST_USER_LABEL[a] for a in acts)
            if any((i["kind"], i["name"]) in PRE_ROOT for i in targets):
                extra += ("\n\n※動画・音楽コーデックは、Microsoftフォントの"
                          "利用規約に同意したものとして入れます。")
            ans = QMessageBox.question(
                self, APP_NAME,
                f"{len(targets)} 個をインストールします。\n"
                "このあとパスワードを1回だけ聞かれます。よろしいですか？\n\n"
                "（数が多いと時間がかかります。途中でパソコンを切らないでください）"
                + extra)
            if ans == QMessageBox.Yes:
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
            self.work_dir = tempfile.mkdtemp(prefix="yomigaeri_")
            os.chmod(self.work_dir, 0o755)
            fetcher = os.path.join(self.work_dir, "fetch_github_deb.py")
            with open(fetcher, "w", encoding="utf-8") as f:
                f.write(GITHUB_FETCHER)
            script = os.path.join(self.work_dir, "install.sh")
            with open(script, "w", encoding="utf-8") as f:
                f.write(build_install_script(targets, fetcher, self.work_dir))

            for it in targets:
                self.set_state(self.row_of[(it["kind"], it["name"])], "待ち")
            self.done_count = self.ok_count = self.ng_count = 0
            self.succeeded = []
            self.total_count = len(targets)
            self.progress.setMaximum(self.total_count)
            self.progress.setValue(0)
            self.log.clear()
            self._busy(True)
            self._set_info("インストールしています…（パスワードの画面が出たら入力してください）")

            self.proc = QProcess(self)
            self.proc.setProcessChannelMode(QProcess.MergedChannels)
            self.proc.readyReadStandardOutput.connect(self.on_output)
            self.proc.finished.connect(self.on_finished)
            self._buf = ""
            self.proc.start("pkexec", ["/bin/bash", script])

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
                self.table.item(row, COL_CHECK).setCheckState(
                    Qt.Unchecked if ok else Qt.Checked)
                self.done_count += 1
                if ok:
                    self.ok_count += 1
                    self.succeeded.append((kind, name))
                else:
                    self.ng_count += 1
                self.progress.setValue(self.done_count)

        def on_finished(self, code, _status):
            self.on_output()
            self._busy(False)
            shutil.rmtree(self.work_dir, ignore_errors=True)
            if code in (126, 127) and self.done_count == 0:
                self._set_info("パスワード入力がキャンセルされたので、何もしませんでした。")
                for r in range(len(self.items)):
                    if self.state_of(r) == "待ち":
                        self.set_state(r, "未インストール")
                return
            # インストール後の設定（自分の設定）
            notes = []
            done_acts = set()
            for key in self.succeeded:
                act = POST_USER.get(key)
                if act and act not in done_acts:
                    done_acts.add(act)
                    ok, m = USER_ACTIONS[act]()
                    line = f"{POST_USER_LABEL[act]}：{m}"
                    notes.append(("✓ " if ok else "✗ ") + line)
                    self.log.appendPlainText("▶ " + line)
            msg = f"終わりました。成功 {self.ok_count} 個 / 失敗 {self.ng_count} 個"
            if notes:
                msg += "\n" + "\n".join(notes)
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
            if self.dirty and not self.confirm_discard():
                ev.ignore()
                return
            ev.accept()

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName("yomigaeri-mado")
    w = MainWindow()
    w.show()
    if open_path:
        w.open_list(open_path)
    return app.exec()


def main():
    ap = argparse.ArgumentParser(prog="yomigaeri-mado", description=APP_NAME)
    ap.add_argument("--export", metavar="フォルダ",
                    help="画面を出さずにリストと復元スクリプトを書き出す")
    ap.add_argument("--open", metavar="リスト.json",
                    help="リストを開いた状態で起動する")
    ap.add_argument("--version", action="version",
                    version=f"{APP_NAME} {APP_VERSION}")
    args = ap.parse_args()
    if args.export:
        sys.exit(cli_export(os.path.expanduser(args.export)))
    sys.exit(run_gui(args.open))


if __name__ == "__main__":
    main()
