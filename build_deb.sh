#!/bin/bash
# よみがえり窓 の DEB パッケージを作る
#   使い方:  bash build_deb.sh
#   できたら: sudo apt install ./yomigaeri-mado_0.2.1_all.deb
set -e
cd "$(dirname "$0")"

VER=0.2.1
PKG=yomigaeri-mado
ROOT=build/${PKG}_${VER}_all

rm -rf build
mkdir -p "$ROOT/DEBIAN" "$ROOT/usr/lib/$PKG" "$ROOT/usr/bin" \
         "$ROOT/usr/share/applications" "$ROOT/usr/share/doc/$PKG"

install -m 644 yomigaeri_mado.py "$ROOT/usr/lib/$PKG/"
install -m 644 LICENSE "$ROOT/usr/share/doc/$PKG/copyright"

cat > "$ROOT/usr/bin/$PKG" <<'EOF'
#!/bin/sh
exec /usr/bin/python3 /usr/lib/yomigaeri-mado/yomigaeri_mado.py "$@"
EOF
chmod 755 "$ROOT/usr/bin/$PKG"

cat > "$ROOT/usr/share/applications/$PKG.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=よみがえり窓
Comment=再インストール後にアプリをまとめて入れ直す
Exec=yomigaeri-mado
Icon=system-software-install
Terminal=false
Categories=System;Settings;PackageManager;
Keywords=reinstall;backup;snap;apt;flatpak;再インストール;一括;
EOF

cat > "$ROOT/DEBIAN/control" <<EOF
Package: $PKG
Version: $VER
Section: admin
Priority: optional
Architecture: all
Depends: python3, python3-pyside6.qtwidgets, python3-pyside6.qtgui, python3-pyside6.qtcore, pkexec | policykit-1
Maintainer: Kenji Iida <kenjiiida-commits@users.noreply.github.com>
Homepage: https://github.com/kenjiiida-commits/YomigaeriMado
Description: Reinstall your apps after a fresh Ubuntu install
 よみがえり窓: Ubuntu の再インストール後に、前のアプリ
 (Snap / apt / Flatpak) をまとめて入れ直すソフト。
EOF

dpkg-deb --build --root-owner-group "$ROOT" "${PKG}_${VER}_all.deb"
echo "できました: ${PKG}_${VER}_all.deb"
