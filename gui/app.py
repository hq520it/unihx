#!/usr/bin/env python3
"""开发入口：从源码树直接运行 GUI（安装后请用 unihx-gui 命令）。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from unihx.gui import main

if __name__ == "__main__":
    main()
