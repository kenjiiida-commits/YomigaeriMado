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
  8. デスクトップの設定（壁紙・ドック・ショートカットなど）と
     個人データ（Mozc 辞書・フォントなど）のバックアップと復元
  9. PPA・外部の入手先（Chrome など）の登録も書き出して戻す
 10. かんたん復元：アプリ → 設定とデータ → 仕上げ を画面の案内どおりに進める

コマンドで使う場合:
  yomigaeri-mado --export フォルダ   リストと復元スクリプトを書き出す（画面なし）
  yomigaeri-mado --open リスト.json  リストを開いた状態で起動
  yomigaeri-mado --restore リスト.json  かんたん復元を始める

License: MIT  (c) 2026 飯田顕治
"""

import argparse
import ast
import base64
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
import tarfile
import tempfile

APP_NAME = "よみがえり窓"
APP_VERSION = "0.5.0"
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
                   "linux-generic", "linux-firmware", "linux-signed",
                   # 言語の設定で自動的に入るもの
                   "language-pack-", "libreoffice-l10n-", "libreoffice-help-",
                   "hunspell-", "hyphen-", "mythes-", "gnome-user-docs-",
                   "firefox-locale-", "thunderbird-locale-")

# Ubuntu の標準パッケージの元になるメタパッケージ
STANDARD_METAS = ("ubuntu-desktop", "ubuntu-desktop-minimal", "ubuntu-minimal",
                  "ubuntu-standard", "ubuntu-server", "ubuntu-server-minimal")

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


def standard_packages():
    """Ubuntu に最初から入っている（標準の）パッケージ名の集合。
    ubuntu-desktop などのメタパッケージがたどる依存関係と、
    優先度が required / important / standard のものを集める。"""
    names = set()
    out = run(["dpkg-query", "-W",
               "-f=${Package} ${Priority} ${db:Status-Status}\n"])
    installed = set()
    for line in out.splitlines():
        cols = line.split()
        if len(cols) == 3 and cols[2] == "installed":
            installed.add(cols[0])
            if cols[1] in ("required", "important", "standard"):
                names.add(cols[0])
    metas = [m for m in STANDARD_METAS if m in installed]
    if metas:
        out = run(["apt-cache", "depends", "--recurse", "--installed",
                   "--no-suggests", "--no-conflicts", "--no-breaks",
                   "--no-replaces", "--no-enhances"] + metas, timeout=300)
        for line in out.splitlines():
            if line and not line[0].isspace() and not line.startswith("<"):
                names.add(line.strip())
    return names


def list_apt():
    """自分で入れた apt パッケージ: (repo_pkgs, local_pkgs, filtered)"""
    manual = set(run(["apt-mark", "showmanual"]).split())
    initial = initial_packages()
    std = standard_packages()
    mine = {p for p in manual - initial - std
            if not p.startswith(APT_SKIP_PREFIX)}
    local = local_deb_packages()
    # DEB ファイルから入れたものは、標準と重なっても残す
    local_mine = {p for p in manual & local
                  if not p.startswith(APT_SKIP_PREFIX)}
    return (sorted(mine - local), sorted(local_mine),
            bool(initial or std))


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


# ------------------------------------------------- 追加の入手先（PPA など）
SOURCES_DIR = "/etc/apt/sources.list.d"
TRUSTED_DIR = "/etc/apt/trusted.gpg.d"


def collect_repos():
    """Ubuntu 標準以外の入手先の登録ファイルと、その鍵を集める。"""
    repos = []
    try:
        names = sorted(os.listdir(SOURCES_DIR))
    except Exception:
        names = []
    for fn in names:
        if not fn.endswith((".list", ".sources")) or fn.startswith("ubuntu"):
            continue
        path = os.path.join(SOURCES_DIR, fn)
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception:
            continue
        active = [l for l in content.splitlines()
                  if l.strip() and not l.lstrip().startswith("#")]
        if not active:
            continue
        keys = {}
        for m in re.finditer(r"signed-by\s*[=:]\s*(/[^\s\]]+)", content, re.I):
            kp = m.group(1)
            if os.path.isfile(kp) and "ubuntu-archive" not in kp:
                try:
                    with open(kp, "rb") as f:
                        keys[kp] = base64.b64encode(f.read()).decode()
                except Exception:
                    pass
        repos.append({"file": fn, "content": content, "keys": keys})
    # 古い方式の鍵（trusted.gpg.d）
    loose = {}
    try:
        for fn in sorted(os.listdir(TRUSTED_DIR)):
            if fn.startswith("ubuntu-keyring") or \
                    not fn.endswith((".gpg", ".asc")):
                continue
            kp = os.path.join(TRUSTED_DIR, fn)
            with open(kp, "rb") as f:
                loose[kp] = base64.b64encode(f.read()).decode()
    except Exception:
        pass
    if loose and repos:
        repos.append({"file": "", "content": "", "keys": loose})
    return repos


def repo_label(r):
    """登録ファイルから、人が読める名前（URL）を取り出す。"""
    m = re.search(r"(?:^deb\s+(?:\[[^\]]*\]\s+)?|^URIs:\s*)(\S+)",
                  r.get("content", ""), re.M)
    return m.group(1) if m else r.get("file", "")


# ---------------------------------------------- デスクトップの設定（dconf）
# (表示名, [まるごと保存するフォルダ], [1つずつ保存するキー])
DESKTOP_GROUPS = [
    ("壁紙とロック画面", ["/org/gnome/desktop/background/",
                     "/org/gnome/desktop/screensaver/"], []),
    ("ドックに並べたアプリ", [], ["/org/gnome/shell/favorite-apps"]),
    ("ドックの設定", ["/org/gnome/shell/extensions/dash-to-dock/"], []),
    ("見た目（テーマ・ダーク・フォント・文字の大きさ）",
     ["/org/gnome/desktop/interface/"], []),
    ("キーボードショートカット",
     ["/org/gnome/desktop/wm/keybindings/", "/org/gnome/shell/keybindings/",
      "/org/gnome/settings-daemon/plugins/media-keys/"], []),
    ("入力ソース", ["/org/gnome/desktop/input-sources/"], []),
    ("マウス・タッチパッド", ["/org/gnome/desktop/peripherals/"], []),
    ("ウィンドウの動作", ["/org/gnome/desktop/wm/preferences/",
                    "/org/gnome/mutter/"], []),
    ("電源と画面オフ", ["/org/gnome/settings-daemon/plugins/power/",
                  "/org/gnome/desktop/session/"], []),
    ("夜間モード", ["/org/gnome/settings-daemon/plugins/color/"], []),
    ("ファイル（Nautilus）", ["/org/gnome/nautilus/"], []),
    ("端末", ["/org/gnome/terminal/"], []),
    ("GNOME 拡張機能のオン・オフ", [], ["/org/gnome/shell/enabled-extensions",
                                  "/org/gnome/shell/disabled-extensions"]),
]


def collect_desktop():
    """{表示名: {"dirs": {パス: dump}, "keys": {キー: 値}}}"""
    if shutil.which("dconf") is None:
        return {}
    groups = {}
    for label, dirs, keys in DESKTOP_GROUPS:
        g = {"dirs": {}, "keys": {}}
        for d in dirs:
            dump = run(["dconf", "dump", d]).strip()
            if dump:
                g["dirs"][d] = dump
        for k in keys:
            v = run(["dconf", "read", k]).strip()
            if v:
                g["keys"][k] = v
        if g["dirs"] or g["keys"]:
            groups[label] = g
    return groups


def wallpaper_files(groups):
    """壁紙・ロック画面に使っている、ホームフォルダ内の画像（ホームからの相対パス）。"""
    home = os.path.expanduser("~")
    out = []
    g = groups.get("壁紙とロック画面", {})
    for dump in g.get("dirs", {}).values():
        for m in re.finditer(r"'file://([^']+)'", dump):
            p = urllib_unquote(m.group(1))
            if p.startswith(home + "/") and os.path.isfile(p):
                rel = os.path.relpath(p, home)
                if rel not in out:
                    out.append(rel)
    return out


def urllib_unquote(s):
    from urllib.parse import unquote
    return unquote(s)


def restore_desktop(groups, labels, old_home=""):
    """選んだ設定を戻す。[(表示名, 成功, メッセージ)]"""
    home = os.path.expanduser("~")
    results = []
    for label in labels:
        g = groups.get(label)
        if not g:
            continue
        errs = []
        for d, dump in g.get("dirs", {}).items():
            if old_home and old_home != home:
                dump = dump.replace(old_home + "/", home + "/")
            r = subprocess.run(["dconf", "load", d], input=dump + "\n",
                               capture_output=True, text=True)
            if r.returncode:
                errs.append(r.stderr.strip())
        for k, v in g.get("keys", {}).items():
            r = subprocess.run(["dconf", "write", k, v],
                               capture_output=True, text=True)
            if r.returncode:
                errs.append(r.stderr.strip())
        results.append((label, not errs, "; ".join(errs)))
    return results


# ------------------------------------------------------- 個人データ
# (表示名, [ホームからの相対パス], 最初からオンにするか)
DATA_CANDIDATES = [
    ("Mozc の辞書と設定", [".mozc", ".config/mozc"], True),
    ("自分で入れたフォント", [".local/share/fonts", ".fonts"], True),
    ("自作のアプリ起動アイコン", [".local/share/applications"], True),
    ("自動起動の設定", [".config/autostart"], True),
    ("シェルの設定（.bashrc など）",
     [".bashrc", ".profile", ".bash_aliases", ".inputrc"], True),
    ("Git の設定", [".gitconfig"], True),
    ("よみがえり窓の設定", [".config/yomigaeri-mado"], True),
]
WALLPAPER_LABEL = "壁紙の画像"
# 安全のため、バックアップに入れないもの
DATA_FORBIDDEN = (".ssh", ".gnupg", ".local/share/keyrings", ".pki",
                  ".password-store", ".mozilla", ".config/google-chrome",
                  ".config/chromium", ".cache")


def data_forbidden(rel):
    rel = rel.strip("/")
    return any(rel == f or rel.startswith(f + "/") for f in DATA_FORBIDDEN)


def path_size(p):
    if os.path.isfile(p):
        return os.path.getsize(p)
    total = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            fp = os.path.join(root, f)
            if not os.path.islink(fp):
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
    return total


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def data_selection(cfg=None):
    """設定に従って、バックアップする個人データ [(表示名, [相対パス])]。
    実在するものだけ返す。"""
    cfg = cfg if cfg is not None else load_config()
    choice = cfg.get("data_choice", {})
    home = os.path.expanduser("~")
    out = []
    for label, rels, default in DATA_CANDIDATES:
        if not choice.get(label, default):
            continue
        ex = [r for r in rels if os.path.exists(os.path.join(home, r))]
        if ex:
            out.append((label, ex))
    for rel in cfg.get("data_extra", []):
        if not data_forbidden(rel) and os.path.exists(os.path.join(home, rel)):
            out.append((rel, [rel]))
    return out


def companion_paths(list_path):
    """リストに付く「設定」と「データ」のファイル名。"""
    base = list_path[:-5] if list_path.endswith(".json") else list_path
    return base + "_設定.json", base + "_データ.tar.gz"


def export_extras(list_path, with_data=True):
    """デスクトップ設定と個人データを、リストの隣に書き出す。説明文を返す。"""
    settings_path, data_path = companion_paths(list_path)
    home = os.path.expanduser("~")
    groups = collect_desktop()
    data = data_selection() if with_data else []
    walls = wallpaper_files(groups) if with_data else []
    if walls:
        data.append((WALLPAPER_LABEL, walls))
    index = []
    if data:
        tmp = data_path + ".tmp"
        with tarfile.open(tmp, "w:gz") as tar:
            for label, rels in data:
                for rel in rels:
                    tar.add(os.path.join(home, rel), arcname=rel)
                index.append({"label": label, "paths": rels})
        os.replace(tmp, data_path)
    elif os.path.exists(data_path):
        os.remove(data_path)
    info = {"format": 1, "created": datetime.datetime.now()
            .isoformat(timespec="seconds"), "home": home,
            "desktop": groups, "data": index,
            "data_file": os.path.basename(data_path) if index else ""}
    tmp = settings_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    os.replace(tmp, settings_path)
    size = human_size(os.path.getsize(data_path)) if index else "なし"
    return (f"デスクトップ設定 {len(groups)} 種類 / 個人データ {len(index)} 種類"
            f"（{size}）")


def restore_data(data_path, labels, index):
    """選んだ個人データをホームフォルダに戻す（同じ名前は上書き）。"""
    home = os.path.expanduser("~")
    want = [p for e in index if e["label"] in labels for p in e["paths"]]
    if not want:
        return 0
    n = 0
    with tarfile.open(data_path, "r:gz") as tar:
        members = [m for m in tar.getmembers()
                   if any(m.name == p or m.name.startswith(p.rstrip("/") + "/")
                          for p in want)]
        for m in members:
            try:
                tar.extract(m, home, filter="data")
            except TypeError:      # 古い Python
                tar.extract(m, home)
            n += 1
    return n


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

nohup yomigaeri-mado --restore "$LIST" >/dev/null 2>&1 &
echo "よみがえり窓を開きました。画面の案内に沿って進めてください。"
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
        meta = new_meta()
        meta["repos"] = collect_repos()
        write_list_file(path, items, meta)
        cfg = load_config()
        try:
            extra = export_extras(path)
        except Exception as e:
            extra = f"設定・データは失敗: {e}"
        cfg["last_export"] = f"{stamp} 成功（{len(items)} 個 / {extra}）"
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


def build_install_script(items, fetcher_path="", work_dir="", repos=None):
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

    if apts and repos:
        lines.append("say '##INFO 追加の入手先を登録しています…'")
        for r in repos:
            for kp, b64 in r.get("keys", {}).items():
                lines += [f"if [ ! -e {q(kp)} ]; then mkdir -p {q(os.path.dirname(kp))};"
                          f" echo {q(b64)} | base64 -d > {q(kp)};"
                          f" chmod 644 {q(kp)}; fi"]
            if r.get("file"):
                dest = os.path.join(SOURCES_DIR, os.path.basename(r["file"]))
                lines += [f"if [ ! -e {q(dest)} ]; then"
                          f" echo {q(base64.b64encode(r['content'].encode()).decode())}"
                          f" | base64 -d > {q(dest)}; chmod 644 {q(dest)};"
                          f" say '##INFO 入手先を登録しました: ' {q(r['file'])}; fi"]
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
def run_gui(open_path=None, wizard=False):
    from PySide6.QtCore import QProcess, Qt, QTimer
    from PySide6.QtGui import QAction, QColor, QFont
    from PySide6.QtWidgets import (
        QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
        QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QInputDialog,
        QLabel, QLineEdit, QListWidget, QMainWindow, QScrollArea, QStackedWidget, QMessageBox, QPlainTextEdit,
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
                f"「{RESTORE_SCRIPT}」、デスクトップ設定と個人データが上書き保存されます。\n"
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

    class DataChoiceDialog(QDialog):
        """バックアップする個人データを選ぶ。"""
        def __init__(self, parent):
            super().__init__(parent)
            self.setWindowTitle("バックアップする個人データ")
            self.cfg = load_config()
            choice = self.cfg.get("data_choice", {})
            home = os.path.expanduser("~")
            v = QVBoxLayout(self)
            v.addWidget(QLabel("書き出しのとき、リストと一緒に保存するものを選んでください。\n"
                               "（パスワードや鍵などの大事な情報は入れません）"))
            self.boxes = {}
            for label, rels, default in DATA_CANDIDATES:
                ex = [r for r in rels if os.path.exists(os.path.join(home, r))]
                size = sum(path_size(os.path.join(home, r)) for r in ex)
                cb = QCheckBox(f"{label}　（{human_size(size)}）" if ex
                               else f"{label}　（見つかりません）")
                cb.setChecked(bool(ex) and choice.get(label, default))
                cb.setEnabled(bool(ex))
                cb.setToolTip("、".join("~/" + r for r in rels))
                self.boxes[label] = cb
                v.addWidget(cb)
            v.addWidget(QLabel("壁紙の画像は、ホームフォルダ内にあれば自動で保存します。"))
            v.addWidget(QLabel("ほかに保存したいフォルダ・ファイル（ホームフォルダの中）:"))
            self.extra = QListWidget()
            for rel in self.cfg.get("data_extra", []):
                self.extra.addItem(rel)
            v.addWidget(self.extra)
            row = QHBoxLayout()
            b_add = QPushButton("フォルダを追加…")
            b_add.clicked.connect(self.add_folder)
            b_addf = QPushButton("ファイルを追加…")
            b_addf.clicked.connect(self.add_file)
            b_del = QPushButton("選んだものを外す")
            b_del.clicked.connect(
                lambda: [self.extra.takeItem(self.extra.row(i))
                         for i in self.extra.selectedItems()])
            for b in (b_add, b_addf, b_del):
                row.addWidget(b)
            v.addLayout(row)
            btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            btns.accepted.connect(self.accept)
            btns.rejected.connect(self.reject)
            v.addWidget(btns)
            self.resize(560, 520)

        def _add(self, path):
            home = os.path.expanduser("~")
            if not path:
                return
            path = os.path.abspath(path)
            if not path.startswith(home + "/"):
                QMessageBox.warning(self, APP_NAME, "ホームフォルダの中から選んでください。")
                return
            rel = os.path.relpath(path, home)
            if data_forbidden(rel):
                QMessageBox.warning(self, APP_NAME,
                                    "パスワードや鍵が入っている場所なので、保存できません。")
                return
            size = path_size(path)
            if size > 500 * 1024 * 1024:
                ans = QMessageBox.question(
                    self, APP_NAME, f"{human_size(size)} あります。大きいですが追加しますか？")
                if ans != QMessageBox.Yes:
                    return
            if not self.extra.findItems(rel, Qt.MatchExactly):
                self.extra.addItem(rel)

        def add_folder(self):
            self._add(QFileDialog.getExistingDirectory(
                self, "保存するフォルダ", os.path.expanduser("~")))

        def add_file(self):
            self._add(QFileDialog.getOpenFileName(
                self, "保存するファイル", os.path.expanduser("~"))[0])

        def accept(self):
            cfg = load_config()
            cfg["data_choice"] = {l: cb.isChecked() for l, cb in self.boxes.items()
                                  if cb.isEnabled()}
            cfg["data_extra"] = [self.extra.item(i).text()
                                 for i in range(self.extra.count())]
            save_config(cfg)
            super().accept()

    class RestorePicker(QWidget):
        """デスクトップ設定と個人データのチェック一覧（ダイアログとかんたん復元で共用）。"""
        def __init__(self, settings_path, data_path):
            super().__init__()
            with open(settings_path, encoding="utf-8") as f:
                self.info = json.load(f)
            self.data_path = data_path
            outer = QVBoxLayout(self)
            outer.setContentsMargins(0, 0, 0, 0)
            sc = QScrollArea()
            sc.setWidgetResizable(True)
            inner = QWidget()
            v = QVBoxLayout(inner)
            v.addWidget(QLabel(f"{self.info.get('created', '?')} に保存したものです。"
                               "戻したいものにチェックを入れてください。"))
            self.desk = {}
            if self.info.get("desktop"):
                v.addWidget(QLabel("<b>デスクトップの設定</b>"))
                for label in self.info["desktop"]:
                    cb = QCheckBox(label)
                    cb.setChecked(True)
                    self.desk[label] = cb
                    v.addWidget(cb)
            self.data = {}
            if self.info.get("data") and os.path.exists(data_path):
                v.addWidget(QLabel("<b>個人データ</b>（同じ名前のファイルは上書きします）"))
                for e in self.info["data"]:
                    cb = QCheckBox(e["label"])
                    cb.setToolTip("、".join("~/" + p for p in e["paths"]))
                    cb.setChecked(True)
                    self.data[e["label"]] = cb
                    v.addWidget(cb)
            elif self.info.get("data"):
                v.addWidget(QLabel("個人データのファイルが見つかりません: " +
                                   os.path.basename(data_path)))
            v.addStretch(1)
            sc.setWidget(inner)
            outer.addWidget(sc)

        def anything(self):
            return any(cb.isChecked() for cb in
                       list(self.desk.values()) + list(self.data.values()))

        def apply(self):
            """選んだものを戻して、結果の行を返す。"""
            labels_d = [l for l, cb in self.data.items() if cb.isChecked()]
            labels_s = [l for l, cb in self.desk.items() if cb.isChecked()]
            lines = []
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                # 画像などのデータを先に戻す（壁紙の設定が画像を指すため）
                if labels_d:
                    try:
                        n = restore_data(self.data_path, labels_d,
                                         self.info["data"])
                        lines.append(f"✓ 個人データ {len(labels_d)} 種類（{n} 個）")
                    except Exception as e:
                        lines.append(f"✗ 個人データ: {e}")
                for label, ok, msg in restore_desktop(
                        self.info.get("desktop", {}), labels_s,
                        self.info.get("home", "")):
                    lines.append(("✓ " if ok else "✗ ") + label +
                                 ("" if ok else f": {msg}"))
            finally:
                QApplication.restoreOverrideCursor()
            return lines

    class RestoreDialog(QDialog):
        """デスクトップ設定と個人データを戻す。"""
        def __init__(self, parent, settings_path, data_path):
            super().__init__(parent)
            self.setWindowTitle("設定とデータを戻す")
            v = QVBoxLayout(self)
            v.addWidget(QLabel("アプリをインストールした後に戻すのがおすすめです。"))
            self.picker = RestorePicker(settings_path, data_path)
            v.addWidget(self.picker, 1)
            btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            btns.button(QDialogButtonBox.Ok).setText("戻す")
            btns.accepted.connect(self.accept)
            btns.rejected.connect(self.reject)
            v.addWidget(btns)
            self.resize(480, 520)

        def accept(self):
            if not self.picker.anything():
                super().reject()
                return
            lines = self.picker.apply()
            QMessageBox.information(
                self, APP_NAME, "戻しました。\n" + "\n".join(lines) +
                "\n\n全部を反映させるには、一度ログアウトしてログインし直してください。")
            super().accept()

    class RestoreWizard(QDialog):
        """かんたん復元：アプリ → 設定とデータ → 仕上げ を順に進める。"""
        def __init__(self, win):
            super().__init__(win)
            self.win = win
            self.setWindowTitle("かんたん復元")
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            self.resize(560, 560)
            meta = win.list_meta
            self.settings_path, self.data_path = companion_paths(win.list_path)
            self.has_settings = os.path.exists(self.settings_path)
            self.install_result = None   # (ok, ng) / "skip"
            self.restore_lines = None    # [...] / "skip"
            self.running = False

            v = QVBoxLayout(self)
            self.steps = QLabel()
            self.steps.setWordWrap(True)
            v.addWidget(self.steps)
            self.stack = QStackedWidget()
            v.addWidget(self.stack, 1)

            # --- 0 はじめに
            p0 = QWidget()
            l0 = QVBoxLayout(p0)
            todo = len(win.checked_items())
            manual = [i["name"] for i in win.items if i["kind"] == "deb-local"
                      and win.state_of(win.row_of[(i["kind"], i["name"])])
                      != "インストール済み"]
            t = (f"<h3>前のパソコンの環境を、3つの手順で元に戻します</h3>"
                 f"リスト：{os.path.basename(win.list_path)}<br>"
                 f"（{meta.get('hostname', '?')} / {meta.get('os', '?')} / "
                 f"{meta.get('created', '?')} 作成）<br><br>"
                 f"<b>① アプリを入れる</b>　まだ入っていない {todo} 個<br>"
                 f"<b>② 設定とデータを戻す</b>　"
                 f"{'壁紙・ドック・辞書など' if self.has_settings else '保存されていません'}<br>"
                 f"<b>③ 仕上げ</b>　ログインし直して完了<br><br>"
                 "インターネットにつないだまま、パソコンを切らずに進めてください。")
            if manual:
                t += ("<br><br>※次のアプリは自動で入れられないので、後で手動で入れてください："
                      "<br>・" + "<br>・".join(manual))
            lab = QLabel(t)
            lab.setWordWrap(True)
            l0.addWidget(lab)
            l0.addStretch(1)
            self.stack.addWidget(p0)

            # --- 1 アプリ
            p1 = QWidget()
            l1 = QVBoxLayout(p1)
            self.app_label = QLabel()
            self.app_label.setWordWrap(True)
            l1.addWidget(self.app_label)
            self.app_progress = QProgressBar()
            self.app_progress.setFormat("%v / %m")
            self.app_progress.hide()
            l1.addWidget(self.app_progress)
            self.app_now = QLabel()
            self.app_now.setWordWrap(True)
            l1.addWidget(self.app_now)
            row = QHBoxLayout()
            self.b_install = QPushButton("インストールを始める")
            self.b_install.setStyleSheet("font-weight: bold; padding: 6px 14px;")
            self.b_install.clicked.connect(self.do_install)
            self.b_retry = QPushButton("失敗したものをやり直す")
            self.b_retry.clicked.connect(self.do_retry)
            self.b_retry.hide()
            row.addWidget(self.b_install)
            row.addWidget(self.b_retry)
            row.addStretch(1)
            l1.addLayout(row)
            l1.addStretch(1)
            self.stack.addWidget(p1)

            # --- 2 設定とデータ
            p2 = QWidget()
            l2 = QVBoxLayout(p2)
            if self.has_settings:
                l2.addWidget(QLabel("<h3>② 設定とデータを戻す</h3>"))
                self.picker = RestorePicker(self.settings_path, self.data_path)
                l2.addWidget(self.picker, 1)
                self.b_apply = QPushButton("チェックしたものを戻す")
                self.b_apply.setStyleSheet("font-weight: bold; padding: 6px 14px;")
                self.b_apply.clicked.connect(self.do_restore)
                l2.addWidget(self.b_apply, 0, Qt.AlignLeft)
            else:
                self.picker = None
                lab = QLabel("<h3>② 設定とデータを戻す</h3>"
                             "このリストには、デスクトップの設定と個人データが"
                             "保存されていません（v0.4.0 より前に書き出したリストです）。"
                             "「次へ」で進んでください。")
                lab.setWordWrap(True)
                l2.addWidget(lab)
                l2.addStretch(1)
            self.restore_label = QLabel()
            self.restore_label.setWordWrap(True)
            l2.addWidget(self.restore_label)
            self.stack.addWidget(p2)

            # --- 3 仕上げ
            p3 = QWidget()
            l3 = QVBoxLayout(p3)
            self.final = QLabel()
            self.final.setWordWrap(True)
            self.final.setTextInteractionFlags(Qt.TextSelectableByMouse)
            l3.addWidget(self.final)
            l3.addStretch(1)
            self.b_logout = QPushButton("今すぐログアウトする")
            self.b_logout.clicked.connect(self.do_logout)
            l3.addWidget(self.b_logout, 0, Qt.AlignLeft)
            self.stack.addWidget(p3)

            # --- 下のボタン
            nav = QHBoxLayout()
            self.b_skip = QPushButton("この手順をとばす")
            self.b_skip.clicked.connect(self.skip)
            self.b_back = QPushButton("戻る")
            self.b_back.clicked.connect(lambda: self.go(self.stack.currentIndex() - 1))
            self.b_next = QPushButton("次へ")
            self.b_next.setDefault(True)
            self.b_next.clicked.connect(self.next)
            nav.addWidget(self.b_skip)
            nav.addStretch(1)
            nav.addWidget(self.b_back)
            nav.addWidget(self.b_next)
            v.addLayout(nav)

            self.timer = QTimer(self)
            self.timer.timeout.connect(self.mirror)
            win.install_listeners.append(self.on_install_done)
            self.refresh_app_page()
            self.go(0)

        # ---- 画面の切り替え
        def go(self, i):
            i = max(0, min(3, i))
            self.stack.setCurrentIndex(i)
            names = ["はじめに", "① アプリを入れる", "② 設定とデータを戻す", "③ 仕上げ"]
            self.steps.setText("　→　".join(
                f"<b><u>{n}</u></b>" if k == i else
                (f"<span style='color:#2e7d32'>{n} ✓</span>" if k < i else
                 f"<span style='color:gray'>{n}</span>")
                for k, n in enumerate(names)))
            if i == 3:
                self.build_final()
            self.update_buttons()

        def update_buttons(self):
            i = self.stack.currentIndex()
            self.b_back.setEnabled(0 < i and not self.running)
            self.b_skip.setVisible(i in (1, 2))
            self.b_skip.setEnabled(not self.running)
            if i == 0:
                self.b_next.setText("はじめる")
            elif i == 3:
                self.b_next.setText("閉じる")
            else:
                self.b_next.setText("次へ")
            done1 = self.install_result is not None
            done2 = self.restore_lines is not None or not self.has_settings
            self.b_next.setEnabled(not self.running and (
                i == 0 or i == 3 or (i == 1 and done1) or (i == 2 and done2)))

        def next(self):
            i = self.stack.currentIndex()
            if i == 3:
                self.close()
                return
            self.go(i + 1)

        def skip(self):
            i = self.stack.currentIndex()
            if i == 1 and self.install_result is None:
                self.install_result = "skip"
            if i == 2 and self.restore_lines is None:
                self.restore_lines = "skip"
            self.go(i + 1)

        # ---- ① アプリ
        def refresh_app_page(self):
            todo = self.win.checked_items()
            repos = self.win.current_repos(todo)
            t = "<h3>① アプリを入れる</h3>"
            if self.install_result is None:
                if todo:
                    t += (f"まだ入っていないアプリ <b>{len(todo)} 個</b> を入れます。<br>"
                          "いらないものは、後ろの一覧でチェックをはずしてから始めてください。<br>"
                          "パスワードを1回だけ聞かれます。数が多いと時間がかかります。")
                    if repos:
                        t += ("<br><br>追加の入手先も登録します：<br>・" +
                              "<br>・".join(repo_label(r) for r in repos if r["file"]))
                    if any((i["kind"], i["name"]) in PRE_ROOT for i in todo):
                        t += ("<br><br>※動画・音楽コーデックは、Microsoft フォントの"
                              "利用規約に同意したものとして入れます。")
                else:
                    t += "入れるアプリはありません（全部入っています）。「次へ」で進んでください。"
                    self.install_result = (0, 0)
            self.app_label.setText(t)
            self.b_install.setVisible(self.install_result is None and bool(todo))

        def do_install(self):
            todo = self.win.checked_items()
            if not todo:
                self.refresh_app_page()
                self.update_buttons()
                return
            self.start(todo)

        def do_retry(self):
            todo = [it for r, it in enumerate(self.win.items)
                    if self.win.state_of(r) == "失敗"]
            if todo:
                self.start(todo)

        def start(self, todo):
            self.running = True
            self.b_install.hide()
            self.b_retry.hide()
            self.app_progress.show()
            self.app_progress.setMaximum(len(todo))
            self.app_progress.setValue(0)
            self.app_now.setText("パスワードの画面が出たら、入力してください。")
            self.update_buttons()
            self.win.start_install(todo)
            self.timer.start(400)

        def mirror(self):
            self.app_progress.setMaximum(max(1, self.win.total_count))
            self.app_progress.setValue(self.win.done_count)
            self.app_now.setText(self.win.info.text())

        def on_install_done(self, cancelled, ok, ng):
            self.timer.stop()
            self.running = False
            self.app_progress.setValue(self.win.done_count)
            if cancelled:
                self.app_now.setText("パスワード入力がキャンセルされました。"
                                     "もう一度「インストールを始める」を押してください。")
                self.app_progress.hide()
                self.b_install.show()
            else:
                prev = self.install_result if isinstance(self.install_result, tuple) else (0, 0)
                failed = sum(1 for r in range(len(self.win.items))
                             if self.win.state_of(r) == "失敗")
                self.install_result = (prev[0] + ok, failed)
                self.app_label.setText("<h3>① アプリを入れる</h3>")
                msg = f"終わりました。成功 {ok} 個 / 失敗 {failed} 個。"
                if failed:
                    msg += ("<br>失敗したものは「失敗したものをやり直す」で再挑戦できます。"
                            "うまくいかなければ、とばして先に進んでも大丈夫です。")
                    self.b_retry.show()
                else:
                    msg += "<br>「次へ」で設定とデータを戻します。"
                self.app_now.setText(msg)
            self.update_buttons()

        # ---- ② 設定とデータ
        def do_restore(self):
            if not self.picker.anything():
                QMessageBox.information(self, APP_NAME, "チェックが入っていません。")
                return
            self.restore_lines = self.picker.apply()
            self.b_apply.setEnabled(False)
            self.restore_label.setText("<b>戻しました。</b><br>" +
                                       "<br>".join(self.restore_lines))
            self.update_buttons()

        # ---- ③ 仕上げ
        def build_final(self):
            t = "<h3>③ 仕上げ</h3>"
            r = self.install_result
            if isinstance(r, tuple):
                t += f"アプリ：成功 {r[0]} 個"
                if r[1]:
                    fails = [it["name"] for k, it in enumerate(self.win.items)
                             if self.win.state_of(k) == "失敗"]
                    t += (f" / 失敗 {r[1]} 個<br>　入らなかったもの：" + "、".join(fails) +
                          "<br>　（後で「失敗したものをやり直す」で再挑戦できます）")
            else:
                t += "アプリ：とばしました"
            t += "<br>"
            if isinstance(self.restore_lines, list):
                bad = [l for l in self.restore_lines if l.startswith("✗")]
                t += (f"設定とデータ：{len(self.restore_lines) - len(bad)} 件を戻しました" +
                      ("<br>　" + "<br>　".join(bad) if bad else ""))
            elif self.has_settings:
                t += "設定とデータ：とばしました"
            manual = [i["name"] for i in self.win.items if i["kind"] == "deb-local"
                      and self.win.state_of(self.win.row_of[(i["kind"], i["name"])])
                      != "インストール済み"]
            if manual:
                t += "<br>手動で入れるもの：" + "、".join(manual)
            t += ("<br><br><b>最後に、一度ログアウトしてログインし直してください。</b><br>"
                  "壁紙やドック、日本語入力などの設定が、すべて反映されます。")
            self.final.setText(t)

        def do_logout(self):
            if shutil.which("gnome-session-quit"):
                QProcess.startDetached("gnome-session-quit", ["--logout"])
            else:
                QMessageBox.information(self, APP_NAME,
                                        "右上のメニューからログアウトしてください。")

        def closeEvent(self, ev):
            if self.running:
                QMessageBox.information(self, APP_NAME,
                                        "インストール中です。終わるまで待ってください。")
                ev.ignore()
                return
            try:
                self.win.install_listeners.remove(self.on_install_done)
            except ValueError:
                pass
            ev.accept()

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
            self.install_listeners = []
            self.wizard = None
            self._build_ui()
            self._set_info("「① 今のアプリを書き出す」「② リストを開く」"
                           "「定番カタログ」のどれかから始めてください。")

        # ---- 画面づくり
        def _build_ui(self):
            tb1 = QToolBar("基本")
            tb1.setMovable(False)
            self.addToolBar(tb1)
            self.addToolBarBreak()
            tb2 = QToolBar("そのほか")
            tb2.setMovable(False)
            self.addToolBar(tb2)
            self.acts = []
            tb = tb1

            def act(text, slot):
                a = QAction(text, self)
                a.triggered.connect(slot)
                tb.addAction(a)
                tb.addSeparator()
                self.acts.append(a)
                return a

            act("かんたん復元（おすすめ）", self.wizard_action)
            act("① 今のアプリを書き出す", self.export_list)
            act("② リストを開く", self.open_list_dialog)
            act("③ 選んだものをまとめてインストール", self.install_selected)
            act("失敗したものをやり直す", self.retry_failed)
            tb = tb2
            act("定番カタログ", self.open_catalog)
            act("設定とデータを戻す…", self.restore_dialog)
            act("バックアップする個人データ…", self.data_choice_dialog)
            act("自動書き出し…", self.auto_export_dialog)
            act("リストを保存", self.save_list)

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
            self.list_meta["repos"] = collect_repos()
            self.fill_table(items, "list")
            if not self.write_list(path):
                return
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                extra = export_extras(path)
            except Exception as e:
                extra = f"設定・データの書き出しに失敗しました: {e}"
            finally:
                QApplication.restoreOverrideCursor()
            counts = {k: sum(1 for i in items if i["kind"] == k)
                      for k in KIND_LABEL}
            msg = (f"書き出しました：{path}\n"
                   f"Snap {counts['snap']}個 / apt {counts['apt']}個 / "
                   f"Flatpak {counts['flatpak']}個 / GitHub {counts['github']}個 / "
                   f"DEBファイル {counts['deb-local']}個 / "
                   f"追加の入手先 {len([r for r in self.list_meta['repos'] if r['file']])}個\n"
                   f"{extra}\n"
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
            self.list_meta["repos"] = data.get("repos", [])
            n = len(self.checked_items())
            self._set_info(
                f"{os.path.basename(path)}（{data.get('hostname', '?')} / "
                f"{data.get('os', '?')} / {data.get('created', '?')} 作成）\n"
                f"まだ入っていない {n} 個にチェックを入れました。"
                "いらないものはチェックをはずして「③」を押してください。"
                + ("\nインストールが終わったら「設定とデータを戻す…」で、"
                   "デスクトップの設定と個人データも戻せます。"
                   if os.path.exists(companion_paths(path)[0]) else ""))

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
            repos = self.current_repos(targets)
            if repos:
                extra += ("\n\n追加の入手先も登録します：\n・" +
                          "\n・".join(repo_label(r) for r in repos if r["file"]))
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

        def current_repos(self, targets):
            if self.mode != "list" or not any(i["kind"] == "apt" for i in targets):
                return []
            return [r for r in self.list_meta.get("repos", [])
                    if r.get("file") or r.get("keys")]

        def wizard_action(self):
            if self.mode != "list" or not self.list_path:
                path, _ = QFileDialog.getOpenFileName(
                    self, "元に戻したいリストを選んでください", os.path.expanduser("~"),
                    "よみがえりリスト (*.json)")
                if not path:
                    return
                self.open_list(path)
                if not self.list_path:
                    return
            self.start_wizard()

        def start_wizard(self):
            if self.wizard and self.wizard.isVisible():
                self.wizard.raise_()
                return
            self.wizard = RestoreWizard(self)
            self.wizard.show()

        def data_choice_dialog(self):
            DataChoiceDialog(self).exec()

        def restore_dialog(self):
            if not self.list_path or self.mode != "list":
                path, _ = QFileDialog.getOpenFileName(
                    self, "戻したいリストを選んでください", os.path.expanduser("~"),
                    "よみがえりリスト (*.json)")
                if not path:
                    return
            else:
                path = self.list_path
            settings_path, data_path = companion_paths(path)
            if not os.path.exists(settings_path):
                QMessageBox.information(
                    self, APP_NAME, "このリストには設定とデータが保存されていません。\n"
                    "（v0.4.0 より前に書き出したリストです）")
                return
            RestoreDialog(self, settings_path, data_path).exec()

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
                f.write(build_install_script(targets, fetcher, self.work_dir,
                                             self.current_repos(targets)))

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
                for cb in list(self.install_listeners):
                    cb(True, 0, 0)
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
            if self.install_listeners:
                for cb in list(self.install_listeners):
                    cb(False, self.ok_count, self.ng_count)
            else:
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
        if wizard and w.list_path:
            w.start_wizard()
    return app.exec()


def main():
    ap = argparse.ArgumentParser(prog="yomigaeri-mado", description=APP_NAME)
    ap.add_argument("--export", metavar="フォルダ",
                    help="画面を出さずにリストと復元スクリプトを書き出す")
    ap.add_argument("--open", metavar="リスト.json",
                    help="リストを開いた状態で起動する")
    ap.add_argument("--restore", metavar="リスト.json",
                    help="リストを開いて、かんたん復元を始める")
    ap.add_argument("--version", action="version",
                    version=f"{APP_NAME} {APP_VERSION}")
    args = ap.parse_args()
    if args.export:
        sys.exit(cli_export(os.path.expanduser(args.export)))
    sys.exit(run_gui(args.restore or args.open, wizard=bool(args.restore)))


if __name__ == "__main__":
    main()
