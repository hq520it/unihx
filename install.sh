#!/usr/bin/env bash
# unihx 安装脚本
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${PREFIX:-$HOME/.local}"
BINDIR="$PREFIX/bin"
SHAREDIR="$PREFIX/share/unihx"

WITH_GUI=0
for arg in "$@"; do
  case "$arg" in
    --gui) WITH_GUI=1 ;;
    -h|--help)
      echo "用法: ./install.sh [--gui]"
      echo "  --gui   同时安装 PySide6 图形控制台"
      exit 0 ;;
    *) echo "未知参数: $arg"; exit 2 ;;
  esac
done

echo "==> 安装到 $PREFIX"
install -d "$BINDIR" "$SHAREDIR/gui"
install -m 755 "$ROOT/bin/unihx" "$BINDIR/unihx"
install -m 644 "$ROOT/gui/app.py" "$SHAREDIR/gui/app.py"

cat > "$BINDIR/unihx-gui" <<EOF
#!/bin/sh
exec python3 "$SHAREDIR/gui/app.py" "\$@"
EOF
chmod 755 "$BINDIR/unihx-gui"

install -Dm644 "$ROOT/assets/unihx.svg" "$PREFIX/share/icons/unihx.svg"
install -Dm644 "$ROOT/assets/unihx.desktop" "$PREFIX/share/applications/unihx.desktop"
if command -v kbuildsycoca6 >/dev/null 2>&1; then
  kbuildsycoca6 --noincremental >/dev/null 2>&1 || true
fi

echo "==> 检查 Python 依赖"
python3 - <<'PY' || true
import importlib, sys
for mod, hint in [("json5", "pip install json5（推荐，用于解析 JSON5 格式 manifest）"),
                  ("PySide6", "GUI 需要：sudo apt install python3-pyside6.qtwidgets")]:
    try:
        importlib.import_module(mod)
        print(f"  [ok] {mod}")
    except Exception:
        print(f"  [missing] {mod}  -> {hint}")
PY

if [ "$WITH_GUI" -eq 1 ]; then
  python3 -c "import PySide6" 2>/dev/null || {
    echo "!! 未检测到 PySide6，请先安装：sudo apt install python3-pyside6.qtwidgets"
  }
fi

echo
echo "==> 完成"
echo "  unihx doctor        # 环境自检"
echo "  unihx update --check # 检查编译器更新"
if [ "$WITH_GUI" -eq 1 ]; then
  echo "  unihx-gui           # 图形控制台"
fi
echo
echo "如果 $BINDIR 不在 PATH 中，请加到 shell 配置里："
echo "  export PATH=\"$BINDIR:\$PATH\""
