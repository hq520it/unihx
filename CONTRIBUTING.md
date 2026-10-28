# Contributing

欢迎提交 Issue / PR。

## 开发

```bash
git clone <repo>
cd unihx
./install.sh --gui        # 本地安装
unihx doctor              # 环境自检
```

## 目录

- `bin/unihx` — CLI 主程序（构建 / 调试 / 编译器管理）
- `gui/app.py` — PySide6 图形控制台
- `docs/` — 编译器说明、FAQ、逆向笔记
- `extras/hx-rpc` — 可选：直接驱动 HBuilderX 主进程

## 提交前

```bash
python -m py_compile bin/unihx gui/app.py
python bin/unihx --help
```

## 原则

- 不修改用户项目源码：编译只读源码，产物写 `unpackage/`
- 不依赖 HBuilderX GUI；老项目用自包含编译器插件包
- 跨平台：路径处理使用 `os.path`，Windows 上注意 `node.exe` / `npm.cmd`
