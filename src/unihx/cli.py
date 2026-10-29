#!/usr/bin/env python3
"""unihx — 只用 uni-app 官方编译器（uni-cli）的构建/调试工具

跨平台：Linux / macOS / Windows
  * 离线打包：mp-weixin / app 资源 / wgt / h5
  * 本地调试：watch 编译 + 自动打开微信开发者工具
  * 自定义发行模式（package.json 的 uni-app.scripts）
  * 编译器在线更新（DCloud 官方渠道）

用法：
  unihx doctor
  unihx list-scripts --project <proj>
  unihx build --project <proj> --target mp-weixin|app|wgt|h5 [--script custom-platform]
  unihx run   --project <proj> [--script custom-platform]      # 仅 mp-weixin
  unihx serve --project <proj>                                 # 仅 h5
  unihx update [--check] [--force] [--with-vite]
  unihx versions
"""
import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

HOME = os.path.expanduser("~")
IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
PLATFORM_NAME = "windows" if IS_WIN else ("macos" if IS_MAC else "linux")

CACHE = os.path.join(HOME, ".local/share/unihx")
COMPILERS = os.path.join(CACHE, "compilers")
CURRENT = os.path.join(CACHE, "compiler")
DOWNLOADS = os.path.join(CACHE, "downloads")
WORKSPACES = os.path.join(CACHE, "workspaces")
RELEASE_JSON = "https://download1.dcloud.net.cn/hbuilderx/release.json"
ALPHA_JSON = "https://download1.dcloud.net.cn/hbuilderx/alpha.json"

TARGETS = {
    "mp-weixin": {"platform": "mp-weixin", "dir": "mp-weixin"},
    "app": {"platform": "app-plus", "dir": "app-plus"},
    "wgt": {"platform": "app-plus", "dir": "app-plus"},
    "h5": {"platform": "h5", "dir": "h5"},
}


def log(msg):
    print(f"[unihx] {msg}", file=sys.stderr)


def fail(msg, code=1):
    log(msg)
    sys.exit(code)


def fetch_json(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


# ---------------- 平台相关 ----------------

def hbuilderx_candidates():
    cands = [os.environ.get("HX_HOME", "")]
    if IS_WIN:
        for env in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(env)
            if base:
                cands.append(os.path.join(base, "HBuilderX"))
        local = os.environ.get("LOCALAPPDATA")
        if local:
            cands.append(os.path.join(local, "Programs", "HBuilderX"))
        cands += [r"C:\HBuilderX", r"D:\HBuilderX"]
    elif IS_MAC:
        cands += ["/Applications/HBuilderX.app/Contents/HBuilderX",
                  os.path.expanduser("~/Applications/HBuilderX.app/Contents/HBuilderX")]
    else:
        cands += [os.path.join(HOME, ".local/opt/HBuilderX"),
                  "/opt/HBuilderX", "/usr/local/HBuilderX"]
    return cands


def node_bin(root):
    name = "node.exe" if IS_WIN else "node"
    p = os.path.join(root, "plugins", "node", name)
    if os.path.isfile(p):
        return p
    return shutil.which("node")


def npm_cmd():
    return shutil.which("npm") or ("npm.cmd" if IS_WIN else "npm")


def devtools_cli():
    if IS_WIN:
        cands = [
            os.path.expandvars(r"%ProgramFiles(x86)%\Tencent\微信web开发者工具\cli.bat"),
            os.path.expandvars(r"%ProgramFiles%\Tencent\微信web开发者工具\cli.bat"),
            os.path.expandvars(r"%LOCALAPPDATA%\微信开发者工具\cli.bat"),
        ]
        for c in cands:
            if os.path.isfile(c):
                return c
        return None
    if IS_MAC:
        p = "/Applications/wechatwebdevtools.app/Contents/MacOS/cli"
        return p if os.path.isfile(p) else None
    return shutil.which("wechat-devtools-cli")


def compiler_entry(root):
    return os.path.join(root, "plugins/uniapp-cli/bin/uniapp-cli.js")


def vite_entry(root):
    return os.path.join(root, "plugins/uniapp-cli-vite/node_modules/@dcloudio/vite-plugin-uni/bin/uni.js")


def is_compiler_root(d):
    return bool(d) and os.path.isfile(compiler_entry(d))


def is_vite_root(d):
    return bool(d) and os.path.isfile(vite_entry(d))


def current_compiler():
    """返回当前生效的编译器根目录（软链或指针文件）"""
    if os.path.islink(CURRENT):
        return os.path.realpath(CURRENT)
    if os.path.isfile(CURRENT):
        try:
            return open(CURRENT, encoding="utf-8").read().strip()
        except Exception:
            return None
    return None


def current_cache_version():
    root = current_compiler()
    if root:
        vf = os.path.join(root, "VERSION")
        if os.path.isfile(vf):
            return open(vf).read().strip()
    return None


def set_current(target):
    """优先软链，Windows 无权限时退回指针文件"""
    try:
        if os.path.lexists(CURRENT):
            os.remove(CURRENT)
        os.symlink(target, CURRENT)
        return
    except OSError:
        pass
    if os.path.isdir(CURRENT):
        shutil.rmtree(CURRENT, ignore_errors=True)
    with open(CURRENT, "w", encoding="utf-8") as f:
        f.write(target)


def resolve_compiler(explicit=None):
    for d in (explicit, os.environ.get("UNI_COMPILER_DIR"), current_compiler(),
              *hbuilderx_candidates()):
        if is_compiler_root(d):
            return d
    return None


# ---------------- 隔离工作区（HBuilderX 老项目） ----------------

MIGRATE_SCRIPTS = {
    "build:mp-weixin": "cross-env NODE_ENV=production UNI_PLATFORM=mp-weixin vue-cli-service uni-build",
    "dev:mp-weixin": "cross-env NODE_ENV=development UNI_PLATFORM=mp-weixin vue-cli-service uni-build --watch",
    "build:h5": "cross-env NODE_ENV=production UNI_PLATFORM=h5 vue-cli-service uni-build",
    "dev:h5": "cross-env NODE_ENV=development UNI_PLATFORM=h5 vue-cli-service uni-serve",
    "build:app-plus": "cross-env NODE_ENV=production UNI_PLATFORM=app-plus vue-cli-service uni-build",
    "dev:app-plus": "cross-env NODE_ENV=development UNI_PLATFORM=app-plus vue-cli-service uni-build --watch",
}

MIGRATE_DEV_DEPS = {
    "@dcloudio/types": "^3.3.3",
    "@dcloudio/uni-app": "^2.0.2-alpha-5020520260821001",
    "@dcloudio/uni-app-plus": "^2.0.2-alpha-5020520260821001",
    "@dcloudio/uni-automator": "^2.0.2-alpha-5020520260821001",
    "@dcloudio/uni-cli-i18n": "^2.0.2-alpha-5020520260821001",
    "@dcloudio/uni-cli-shared": "^2.0.2-alpha-5020520260821001",
    "@dcloudio/uni-h5": "^2.0.2-alpha-5020520260821001",
    "@dcloudio/uni-i18n": "^2.0.2-alpha-5020520260821001",
    "@dcloudio/uni-migration": "^2.0.2-alpha-5020520260821001",
    "@dcloudio/uni-mp-360": "^2.0.2-alpha-5020520260821001",
    "@dcloudio/uni-mp-alipay": "^2.0.2-alpha-5020520260821001",
    "@dcloudio/uni-mp-baidu": "^2.0.2-alpha-5020520260821001",
    "@dcloudio/uni-mp-harmony": "^2.0.2-alpha-5020520260821001",
    "@dcloudio/uni-mp-jd": "^2.0.2-alpha-5020520260821001",
    "@dcloudio/uni-mp-kuaishou": "^2.0.2-alpha-5020520260821001",
    "@dcloudio/uni-mp-lark": "^2.0.2-alpha-5020520260821001",
    "@dcloudio/uni-mp-qq": "^2.0.2-alpha-5020520260821001",
    "@dcloudio/uni-mp-toutiao": "^2.0.2-alpha-5020520260821001",
    "@dcloudio/uni-mp-vue": "^2.0.2-alpha-5020520260821001",
    "@dcloudio/uni-mp-weixin": "^2.0.2-alpha-5020520260821001",
    "@dcloudio/uni-mp-xhs": "^2.0.2-alpha-5020520260821001",
    "@dcloudio/uni-quickapp-webview": "^2.0.2-alpha-5020520260821001",
    "@dcloudio/uni-stacktracey": "^2.0.2-alpha-5020520260821001",
    "@dcloudio/uni-stat": "^2.0.2-alpha-5020520260821001",
    "@dcloudio/uni-template-compiler": "^2.0.2-alpha-5020520260821001",
    "@dcloudio/vue-cli-plugin-uni": "^2.0.2-alpha-5020520260821001",
    "@dcloudio/vue-cli-plugin-uni-optimize": "^2.0.2-alpha-5020520260821001",
    "@dcloudio/webpack-uni-mp-loader": "^2.0.2-alpha-5020520260821001",
    "@dcloudio/webpack-uni-pages-loader": "^2.0.2-alpha-5020520260821001",
    "@vue/cli-plugin-babel": "^4.5.19",
    "@vue/cli-service": "4.2.3",
    "@vue/composition-api": "^1.7.0",
    "@vue/shared": "^3.2.20",
    "babel-plugin-import": "^1.13.8",
    "caniuse-lite": "^1.0.30001488",
    "flyio": "^0.6.2",
    "less-loader": "^4.1.0",
    "module-alias": "^2.1.0",
    "patch-package": "^6.4.7",
    "postcss-comment": "^2.0.0",
    "postcss-px-to-viewport": "^1.1.1",
    "postinstall-postinstall": "^2.1.0",
    "pug-plain-loader": "^1.0.0",
    "regenerator-runtime": "^0.12.1",
    "sass-loader": "^8.0.2",
    "stylus-loader": "^3.0.2",
    "vue": ">= 2.6.11 < 2.7",
    "vue-class-component": "^6.3.2",
    "vue-i18n": "^8.24.1",
    "vue-property-decorator": "^7.2.0",
    "vue-template-compiler": "2.6.14",
    "vuex": "^3.0.1",
    "cross-env": "^7.0.3",
    "postcss": "^8.4.38",
    "postcss-import": "^15.1.0",
    "autoprefixer": "^10.4.19",
    "@babel/plugin-transform-runtime": "^7.24.3",
    "webpack": "4.46.0",
    "less": "^4.2.0",
    "sass": "^1.77.0"
}

POSTCSS_CONFIG = """const path = require('path')
module.exports = {
  parser: require('postcss-comment'),
  plugins: [
    require('postcss-import')({
      resolve(id) {
        if (id.startsWith('~@/')) return path.resolve(process.env.UNI_INPUT_DIR, id.substr(3))
        if (id.startsWith('@/')) return path.resolve(process.env.UNI_INPUT_DIR, id.substr(2))
        if (id.startsWith('/') && !id.startsWith('//')) return path.resolve(process.env.UNI_INPUT_DIR, id.substr(1))
        return id
      }
    }),
    require('autoprefixer')({ remove: process.env.UNI_PLATFORM !== 'h5' }),
    require('@dcloudio/vue-cli-plugin-uni/packages/postcss')
  ]
}
"""

BABEL_CONFIG = """module.exports = {
  presets: [['@vue/app', { useBuiltIns: process.env.UNI_PLATFORM === 'h5' ? 'usage' : 'entry' }]],
  plugins: [
    ['import', { libraryName: '@dcloudio/uni-ui', customName: (name) => `@dcloudio/uni-ui/lib/${name}/${name}` }],
    ...(process.env.UNI_PLATFORM !== 'h5' ? ['@babel/plugin-transform-runtime'] : [])
  ]
}
"""


def workspace_path(project):
    import hashlib
    h = hashlib.sha1(os.path.abspath(project).encode()).hexdigest()[:10]
    return os.path.join(WORKSPACES, f"{os.path.basename(project)}-{h}")


def detect_css(project):
    less = sass = False
    for base, dirs, names in os.walk(project):
        dirs[:] = [d for d in dirs if d not in ("node_modules", "unpackage", ".git")]
        for n in names:
            if n.endswith(".less"):
                less = True
            elif n.endswith((".scss", ".sass")):
                sass = True
        if less and sass:
            break
    return less, sass


SYNC_EXCLUDE_DIRS = {"node_modules", "unpackage", ".git"}
SYNC_KEEP = {"package.json", "postcss.config.js", "babel.config.js"}


def patch_manifest_sass(ws):
    mf = os.path.join(ws, "manifest.json")
    if os.path.isfile(mf):
        try:
            text = open(mf, encoding="utf-8").read()
            if '"node-sass"' in text:
                open(mf, "w", encoding="utf-8").write(text.replace('"node-sass"', '"sass"'))
        except Exception:
            pass


def sync_source(project, ws, delete=True):
    os.makedirs(ws, exist_ok=True)
    if shutil.which("rsync"):
        cmd = ["rsync", "-a"]
        if delete:
            cmd.append("--delete")
        for d in SYNC_EXCLUDE_DIRS:
            cmd += ["--exclude", d]
        for f in SYNC_KEEP:
            cmd += ["--exclude", f]
        cmd += [project.rstrip("/") + "/", ws.rstrip("/") + "/"]
        rc = subprocess.call(cmd)
        patch_manifest_sass(ws)
        return rc
    # 纯 Python 兜底
    for base, dirs, names in os.walk(project):
        dirs[:] = [d for d in dirs if d not in SYNC_EXCLUDE_DIRS]
        rel = os.path.relpath(base, project)
        target_dir = os.path.join(ws, rel) if rel != "." else ws
        os.makedirs(target_dir, exist_ok=True)
        for n in names:
            if n in SYNC_KEEP:
                continue
            src = os.path.join(base, n)
            dst = os.path.join(target_dir, n)
            try:
                if not os.path.exists(dst) or os.path.getmtime(src) > os.path.getmtime(dst):
                    shutil.copy2(src, dst)
            except OSError:
                pass
    patch_manifest_sass(ws)
    return 0


CLI_DEP_KEYS = ("@dcloudio/vue-cli-plugin-uni", "@dcloudio/vite-plugin-uni")


def project_kind(project):
    """cli = package.json 声明了官方 CLI 编译器依赖；legacy = HBuilderX 托管项目"""
    pkg = read_package(project) or {}
    deps = {}
    deps.update(pkg.get("dependencies") or {})
    deps.update(pkg.get("devDependencies") or {})
    if any(k in deps for k in CLI_DEP_KEYS):
        return "cli"
    return "legacy"


def needs_isolate(project):
    return project_kind(project) == "legacy"


def has_ts_sources(project):
    for base, dirs, names in os.walk(project):
        dirs[:] = [d for d in dirs if d not in SYNC_EXCLUDE_DIRS]
        for n in names:
            if n.endswith((".ts", ".tsx")) and not n.endswith(".d.ts"):
                return True
    return False


def prepare_workspace(project, registry=None, force=False):
    ws = workspace_path(project)
    if force and os.path.isdir(ws):
        shutil.rmtree(ws, ignore_errors=True)
    log(f"隔离工作区: {ws}")
    sync_source(project, ws, delete=not force)

    # 合并规则：原项目 package.json 原样保留（dependencies / uni-app.scripts / 自定义脚本），
    # 只补缺失的 CLI scripts 和编译器 devDependencies；原项目已有同名项优先，不覆盖。
    origin = read_package(project) or {}
    pkg = dict(origin)
    scripts = dict(origin.get("scripts") or {})
    for k, v in MIGRATE_SCRIPTS.items():
        scripts.setdefault(k, v)
    pkg["scripts"] = scripts
    dev = dict(origin.get("devDependencies") or {})
    for k, v in MIGRATE_DEV_DEPS.items():
        dev.setdefault(k, v)
    less, sass = detect_css(ws)
    if less:
        dev.setdefault("less", "^4.2.0")
    if sass:
        dev.setdefault("sass", "^1.77.0")
    if has_ts_sources(ws):
        fail("项目包含 TypeScript 源码，当前隔离模板暂未适配 TS，请先转 JS 或使用 HBuilderX 编译")
    pkg["devDependencies"] = dev
    with open(os.path.join(ws, "package.json"), "w", encoding="utf-8") as f:
        json.dump(pkg, f, ensure_ascii=False, indent=2)

    with open(os.path.join(ws, "postcss.config.js"), "w", encoding="utf-8") as f:
        f.write(POSTCSS_CONFIG)
    with open(os.path.join(ws, "babel.config.js"), "w", encoding="utf-8") as f:
        f.write(BABEL_CONFIG)
    tsconfig = os.path.join(ws, "tsconfig.json")
    if not os.path.isfile(tsconfig):
        with open(tsconfig, "w", encoding="utf-8") as f:
            json.dump({
                "compilerOptions": {
                    "target": "esnext", "module": "esnext", "moduleResolution": "node",
                    "allowJs": True, "noEmit": True, "skipLibCheck": True,
                    "esModuleInterop": True, "resolveJsonModule": True,
                    "jsx": "preserve", "baseUrl": ".",
                },
                "include": ["**/*.ts", "**/*.tsx", "**/*.vue", "**/*.js", "**/*.jsx"],
                "exclude": ["node_modules", "unpackage"],
            }, f, ensure_ascii=False, indent=2)
    patch_manifest_sass(ws)

    stamp = os.path.join(ws, ".unihx-deps")
    sig = json.dumps(dev, sort_keys=True)
    nm = os.path.join(ws, "node_modules")
    need_install = (not os.path.isdir(nm)) or (not os.path.isfile(stamp))
    if not need_install:
        need_install = open(stamp).read().strip() != sig
    if need_install:
        cmd = [npm_cmd(), "install", "--legacy-peer-deps", "--no-audit", "--no-fund"]
        if registry:
            cmd += ["--registry", registry]
        log(f"安装依赖: {' '.join(cmd)}")
        if subprocess.call(cmd, cwd=ws) != 0:
            fail("工作区依赖安装失败")
        open(stamp, "w").write(sig)
    return ws


def start_sync(project, ws, stop_event):
    import threading
    import time

    def loop():
        while not stop_event.is_set():
            sync_source(project, ws, delete=False)
            time.sleep(1.0)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


# ---------------- 项目信息 ----------------

def read_package(project):
    path = os.path.join(project, "package.json")
    if not os.path.isfile(path):
        return {}
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
        try:
            import json5
            return json5.loads(text)
        except Exception:
            return json.loads(text)
    except Exception:
        return {}


def npm_script(project, kind, target):
    """kind: build/dev; target: mp-weixin/h5/app-plus"""
    scripts = (read_package(project) or {}).get("scripts") or {}
    names = []
    if kind == "build":
        names = [f"build:{target}", "build"]
    else:
        names = [f"dev:{target}", f"serve:{target}", "dev"]
    for n in names:
        if n in scripts:
            return n
    return None


def run_npm(project, script):
    log(f"npm run {script}")
    env = os.environ.copy()
    env["UNI_CLI_CONTEXT"] = project
    return subprocess.call([npm_cmd(), "run", script], cwd=project, env=env)


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
        return [(k, v.get("title") or k, v) for k, v in scripts.items()]
    except Exception:
        return []


def script_platform(project, name):
    if not name:
        return None
    for n, _title, cfg in read_scripts(project):
        if n == name:
            return ((cfg.get("env") or {}).get("UNI_PLATFORM")) or None
    return None


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
        return {"name": d.get("name"), "version": d.get("versionName"),
                "appid": mp.get("appid")}
    except Exception:
        return {}


# ---------------- 构建 ----------------

def make_env(project, out, platform_name, script, mode, compiler_root=None):
    env = {
        "NODE_ENV": "development" if mode == "dev" else "production",
        "UNI_PLATFORM": platform_name,
        "UNI_INPUT_DIR": os.path.abspath(project),
        "UNI_OUTPUT_DIR": os.path.abspath(out),
    }
    if script:
        env["UNI_SCRIPT"] = script
    paths = []
    if compiler_root:
        paths.append(os.path.join(compiler_root, "plugins/uniapp-cli/node_modules"))
    proj_nm = os.path.join(os.path.abspath(project), "node_modules")
    if os.path.isdir(proj_nm):
        paths.append(proj_nm)
    if os.environ.get("NODE_PATH"):
        paths.append(os.environ["NODE_PATH"])
    if paths:
        env["NODE_PATH"] = os.pathsep.join(paths)
    return env


def has_project_deps(project):
    pkg = os.path.join(project, "package.json")
    if not os.path.isfile(pkg):
        return False
    try:
        d = json.load(open(pkg, encoding="utf-8"))
    except Exception:
        return False
    deps = {}
    deps.update(d.get("dependencies") or {})
    deps.update(d.get("devDependencies") or {})
    return bool(deps)


def ensure_deps(project, install, registry=None):
    if not has_project_deps(project):
        return True
    if os.path.isdir(os.path.join(project, "node_modules")):
        return True
    if not install:
        log("项目缺少 node_modules。加 --install 可自动 npm install，或手动在项目里执行 npm install。")
        return True
    cmd = [npm_cmd(), "install", "--no-audit", "--no-fund"]
    if registry:
        cmd += ["--registry", registry]
    log(f"安装项目依赖: {' '.join(cmd)}")
    return subprocess.call(cmd, cwd=project) == 0


def compiler_cmd(root, platform_name, vite):
    if vite:
        if not is_vite_root(root):
            fail("该编译器没有 vite 插件，去掉 --vite 或运行 unihx update --with-vite")
        return (os.path.join(root, "plugins/uniapp-cli-vite"),
                [node_bin(root),
                 os.path.join("node_modules/@dcloudio/vite-plugin-uni/bin/uni.js"),
                 "build", "-p", platform_name])
    return (os.path.join(root, "plugins/uniapp-cli"), [node_bin(root), "bin/uniapp-cli.js"])


def zip_dir(src, dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    files = []
    for base, _dirs, names in os.walk(src):
        for n in names:
            full = os.path.join(base, n)
            files.append((full, os.path.relpath(full, src)))
    files.sort(key=lambda x: x[1])
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for full, arc in files:
            z.write(full, arc)
    return dest, len(files)


def ws_out(project, args, out_dir):
    return args.out or os.path.join(project, "unpackage/dist",
                                    "dev" if args.mode == "dev" else "build", out_dir)


def build_isolated(project, out, platform_name, args, out_dir):
    ws = prepare_workspace(project, registry=getattr(args, "registry", None),
                           force=getattr(args, "recreate_workspace", False))
    os.makedirs(out, exist_ok=True)
    env = os.environ.copy()
    env["UNI_CLI_CONTEXT"] = ws
    env["UNI_INPUT_DIR"] = ws
    env["UNI_OUTPUT_DIR"] = os.path.abspath(out)
    env["UNI_PLATFORM"] = platform_name
    if args.script:
        env["UNI_SCRIPT"] = args.script
    script = "build:%s" % platform_name
    log(f"隔离构建: {script}（源码只读，产物写入 {out}）")
    rc = subprocess.call([npm_cmd(), "run", script], cwd=ws, env=env)
    if rc == 0 and args.target == "wgt":
        name = args.wgt_name or (os.path.basename(project) + ".wgt")
        if not name.endswith(".wgt"):
            name += ".wgt"
        dest = os.path.join(project, "unpackage/release", name)
        path, count = zip_dir(out, dest)
        log(f"wgt 已生成: {path}  ({count} 个文件)")
    return rc


def cmd_build(args):
    project = os.path.abspath(args.project)
    if args.target not in TARGETS:
        fail(f"未知目标 {args.target}，可选：{', '.join(TARGETS)}")
    t = TARGETS[args.target]
    platform_name = t["platform"]
    sp = script_platform(project, args.script)
    if sp:
        if sp != platform_name:
            log(f"注意：自定义模式平台是 {sp}，覆盖目标平台 {platform_name}")
        platform_name = sp
    if args.target == "wgt" and platform_name != "app-plus":
        fail("wgt 只能打包 app-plus 产物；该自定义模式平台是 " + platform_name)
    dir_map = {"mp-weixin": "mp-weixin", "h5": "h5", "app-plus": "app-plus"}
    out_dir = dir_map.get(platform_name, platform_name)

    kind = project_kind(project)
    if getattr(args, "isolate", False):  # 实验特性，仅显式启用
        return build_isolated(project, ws_out(project, args, out_dir), platform_name,
                              args, out_dir)

    if args.npm or kind == "cli":
        script = npm_script(project, "build", platform_name)
        if not script:
            fail("项目里没有 build:%s 脚本，先运行 unihx init-cli --project %s --write"
                 % (platform_name, project))
        if not ensure_deps(project, True, args.registry):
            fail("项目依赖安装失败")
        return run_npm(project, script)

    root = ensure_compiler(args)
    if not ensure_deps(project, args.install, args.registry):
        fail("项目依赖安装失败")

    out = args.out or os.path.join(project, "unpackage/dist",
                                   "dev" if args.mode == "dev" else "build", out_dir)
    cwd, cmd = compiler_cmd(root, platform_name, args.vite)
    env = os.environ.copy()
    env.update(make_env(project, out, platform_name, args.script, args.mode, root))
    env["UNI_HBUILDERX_PLUGINS"] = os.path.join(root, "plugins")
    # app-plus 产物要兼容旧 Android WebView（如 Chrome 74），
    # 否则 babel 会保留 ?. / ?? 等语法导致白屏
    if platform_name == "app-plus" and "BROWSERSLIST" not in env:
        env["BROWSERSLIST"] = "Android >= 4.4, ios >= 9, chrome >= 49"

    log(f"platform={PLATFORM_NAME} compiler={root}")
    log(f"uni-platform={platform_name} target={args.target}")
    if args.script:
        log(f"script={args.script}")
    log(f"output={out}")
    rc = subprocess.call(cmd, cwd=cwd, env=env)
    if rc != 0:
        return rc

    if args.target == "wgt":
        name = args.wgt_name or (os.path.basename(project) + ".wgt")
        if not name.endswith(".wgt"):
            name += ".wgt"
        dest = os.path.join(project, "unpackage/release", name)
        path, count = zip_dir(out, dest)
        log(f"wgt 已生成: {path}  ({count} 个文件)")
    return 0


def run_isolated(project, args, platform_name):
    import threading
    ws = prepare_workspace(project, registry=getattr(args, "registry", None),
                           force=getattr(args, "recreate_workspace", False))
    out = os.path.join(project, "unpackage/dist/dev", platform_name)
    os.makedirs(out, exist_ok=True)
    env = os.environ.copy()
    env["UNI_CLI_CONTEXT"] = ws
    env["UNI_INPUT_DIR"] = ws
    env["UNI_OUTPUT_DIR"] = os.path.abspath(out)
    env["UNI_PLATFORM"] = platform_name
    if args.script:
        env["UNI_SCRIPT"] = args.script
    stop = threading.Event()
    start_sync(project, ws, stop)
    script = "dev:%s" % platform_name
    log(f"隔离运行: {script}（源码同步 → 工作区）")
    proc = subprocess.Popen([npm_cmd(), "run", script], cwd=ws, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, encoding="utf-8", errors="replace")
    opened = False
    try:
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            if not opened and platform_name == "mp-weixin" and \
                    any(k in line for k in ("编译成功", "Build complete", "DONE")):
                opened = True
                if not getattr(args, "no_devtools", False):
                    open_devtools(out)
    except KeyboardInterrupt:
        proc.terminate()
    finally:
        stop.set()
    return proc.wait()


def devtools_alive():
    return subprocess.run(["pgrep", "-f", "wechat-devtools/electron/electron"],
                          stdout=subprocess.DEVNULL).returncode == 0


def devtools_alive():
    return subprocess.run(["pgrep", "-f", "wechat-devtools/electron/electron"],
                          stdout=subprocess.DEVNULL).returncode == 0


def _try_devtools_open(cli, project_out, timeout=15):
    try:
        r = subprocess.run([cli, "cli", "open", "--project", project_out],
                           capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        return False, str(e)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    bad = ("timeout" in out.lower() or "fail" in out.lower() or "error" in out.lower())
    return (r.returncode == 0 and not bad), out


def open_devtools(project_out):
    cli = devtools_cli()
    if not cli:
        log("未找到微信开发者工具 CLI，跳过自动打开")
        return
    log(f"打开微信开发者工具: {project_out}")
    gui = shutil.which("wechat-devtools")
    if gui and not devtools_alive():
        log("开发者工具未运行，启动 GUI…")
        try:
            subprocess.Popen([gui], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except Exception as e:
            log(f"启动开发者工具失败: {e}")
    import time
    last = ""
    for _ in range(8):
        ok, msg = _try_devtools_open(cli, project_out)
        if ok:
            log("开发者工具已打开")
            return
        last = msg
        time.sleep(5)
    log(f"开发者工具打开失败: {last[-160:]}")
    log("提示：请在 微信开发者工具 → 设置 → 安全设置 中开启「服务端口」后重试")


def cmd_run(args):
    project = os.path.abspath(args.project)
    root = ensure_compiler(args)
    if not ensure_deps(project, args.install, args.registry):
        fail("项目依赖安装失败")
    sp = script_platform(project, args.script)
    if sp and sp != "mp-weixin":
        fail(f"该自定义模式平台是 {sp}，调试请用 `unihx serve`")
    kind = project_kind(project)
    if getattr(args, "isolate", False):
        return run_isolated(project, args, "mp-weixin")
    if args.npm or kind == "cli":
        script = npm_script(project, "dev", "mp-weixin")
        if not script:
            fail("项目里没有 dev:mp-weixin 脚本，先运行 unihx init-cli --project %s --write" % project)
        if not ensure_deps(project, True, args.registry):
            fail("项目依赖安装失败")
        return run_npm(project, script)
    out = os.path.join(project, "unpackage/dist/dev/mp-weixin")
    cwd, _ = compiler_cmd(root, "mp-weixin", args.vite)
    env = os.environ.copy()
    env.update(make_env(project, out, "mp-weixin", args.script, "dev", root))
    env["UNI_HBUILDERX_PLUGINS"] = os.path.join(root, "plugins")
    cmd = ([node_bin(root), "node_modules/@dcloudio/vite-plugin-uni/bin/uni.js", "dev", "-p", "mp-weixin"]
           if args.vite else [node_bin(root), "bin/uniapp-cli.js"])
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1,
                            encoding="utf-8", errors="replace")
    opened = False
    try:
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            if not opened and any(k in line for k in ("编译成功", "Build complete", "DONE")):
                opened = True
                if not args.no_devtools:
                    open_devtools(out)
    except KeyboardInterrupt:
        proc.terminate()
    return proc.wait()


def cmd_serve(args):
    project = os.path.abspath(args.project)
    root = ensure_compiler(args)
    if not ensure_deps(project, args.install, args.registry):
        fail("项目依赖安装失败")
    sp = script_platform(project, args.script)
    if sp and sp != "h5":
        fail(f"该自定义模式平台是 {sp}，调试请用 `unihx run`")
    kind = project_kind(project)
    if getattr(args, "isolate", False):
        return run_isolated(project, args, "h5")
    if args.npm or kind == "cli":
        script = npm_script(project, "dev", "h5")
        if not script:
            fail("项目里没有 dev:h5 脚本，先运行 unihx init-cli --project %s --write" % project)
        if not ensure_deps(project, True, args.registry):
            fail("项目依赖安装失败")
        return run_npm(project, script)
    out = os.path.join(project, "unpackage/dist/dev/h5")
    cwd, _ = compiler_cmd(root, "h5", args.vite)
    env = os.environ.copy()
    env.update(make_env(project, out, "h5", args.script, "dev", root))
    env["UNI_HBUILDERX_PLUGINS"] = os.path.join(root, "plugins")
    cmd = ([node_bin(root), "node_modules/@dcloudio/vite-plugin-uni/bin/uni.js", "dev", "-p", "h5"]
           if args.vite else [node_bin(root), "bin/uniapp-cli.js"])
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1,
                            encoding="utf-8", errors="replace")
    try:
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
    except KeyboardInterrupt:
        proc.terminate()
    return proc.wait()


# ---------------- 自检 / 更新 ----------------

def cmd_doctor(args):
    root = resolve_compiler(args.compiler)
    print("unihx doctor")
    print(f"  platform  : {PLATFORM_NAME} ({platform.machine()})")
    print(f"  python    : {sys.version.split()[0]}")
    print(f"  node      : {node_bin(root) if root else (shutil.which('node') or '未找到')}")
    print(f"  compiler  : {root or '未找到（unihx update 或安装 HBuilderX）'}")
    if root:
        print(f"  webpack   : {'是' if is_compiler_root(root) else '否'}")
        print(f"  vite      : {'是' if is_vite_root(root) else '否'}")
    print(f"  缓存版本  : {current_cache_version() or '无'}")
    print(f"  开发者工具: {devtools_cli() or '未找到（可选）'}")
    print(f"  上传工具  : {shutil.which('miniprogram-ci') or '未找到（可选）'}")
    print(f"  支持目标  : {', '.join(TARGETS)}")
    return 0


def cmd_list_scripts(args):
    scripts = read_scripts(os.path.abspath(args.project))
    if not scripts:
        print("（无自定义发行模式）")
        return 0
    for name, title, cfg in scripts:
        env = cfg.get("env") or {}
        defines = list((cfg.get("define") or {}).keys())
        print(f"  {title}\n    name={name}  platform={env.get('UNI_PLATFORM')}  define={','.join(defines) or '-'}")
    return 0


def package_for_platform(rel):
    code = None
    if IS_WIN:
        code = "win_simple"
    elif IS_MAC:
        code = "mac_simple_arm64" if platform.machine() == "arm64" else "mac_simple"
    else:
        code = "linux_full_x64"
    for f in rel.get("files", []):
        if f.get("code") == code:
            return f
    return None


def download(url, dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    log(f"下载 {url}")
    rc = subprocess.call(["curl", "-L", "-C", "-", "--retry", "5", "--retry-delay", "3",
                          "--retry-all-errors", "-o", dest, url])
    if rc != 0:
        fail("下载失败")


def install_from_dir(src_root, dest, with_vite=False):
    names = ["uniapp-cli", "node", "compile-less", "compile-node-sass", "compile-dart-sass"]
    if with_vite:
        names.append("uniapp-cli-vite")
    found = False
    for name in names:
        s = os.path.join(src_root, "plugins", name)
        if not os.path.isdir(s):
            continue
        found = True
        d = os.path.join(dest, "plugins", name)
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.copytree(s, d, dirs_exist_ok=True)
    if not found:
        fail("解压后找不到编译器插件，包结构可能变化")


def extract_archive(archive, dest, with_vite=False):
    """支持 zip / tar.gz / dmg，只安装需要的插件"""
    lower = archive.lower()
    with tempfile.TemporaryDirectory() as tmp:
        if lower.endswith(".zip"):
            with zipfile.ZipFile(archive) as z:
                z.extractall(tmp)
        elif lower.endswith((".tar.gz", ".tgz")):
            rc = subprocess.call(["tar", "-xzf", archive, "-C", tmp])
            if rc != 0:
                fail("解压失败")
        elif lower.endswith(".dmg") and IS_MAC:
            mnt = os.path.join(tmp, "mnt")
            os.makedirs(mnt)
            subprocess.call(["hdiutil", "attach", "-nobrowse", "-quiet",
                             "-mountpoint", mnt, archive])
            try:
                app = os.path.join(mnt, "HBuilderX.app", "Contents", "HBuilderX")
                if not os.path.isdir(os.path.join(app, "plugins")):
                    cands = [os.path.join(mnt, n) for n in os.listdir(mnt)]
                    app = next((c for c in cands if os.path.isdir(os.path.join(c, "plugins"))), app)
                install_from_dir(app, dest, with_vite)
                return
            finally:
                subprocess.call(["hdiutil", "detach", mnt, "-quiet"])
        else:
            fail(f"不支持的安装包格式: {archive}")
        # zip/tar：找到包含 plugins 的根目录
        root = tmp
        if not os.path.isdir(os.path.join(root, "plugins")):
            for name in os.listdir(tmp):
                cand = os.path.join(tmp, name)
                if os.path.isdir(os.path.join(cand, "plugins")):
                    root = cand
                    break
        install_from_dir(root, dest, with_vite)


COMPILER_PLUGIN_DIRS = ["uniapp-cli", "node", "compile-less", "compile-node-sass", "compile-dart-sass"]


def install_from_hbuilderx_dir(src_root, with_vite=False):
    """从已有 HBuilderX 安装目录复制编译器插件"""
    version = "local"
    try:
        d = json.load(open(os.path.join(src_root, "plugins/uniapp-cli/package.json"), encoding="utf-8"))
        version = (d.get("version") or "local").replace("/", "-")
    except Exception:
        pass
    if not version.startswith("local"):
        version = "local-" + version
    target = os.path.join(COMPILERS, version)
    if not is_compiler_root(target):
        install_from_dir(src_root, target, with_vite=with_vite)
    open(os.path.join(target, "VERSION"), "w").write(version + "\n")
    set_current(target)
    return version, target


def install_compiler_package(channel="release", force=False, with_vite=False, quiet=False):
    """下载官方包并只安装编译器插件；返回 (version, target)"""
    url = ALPHA_JSON if channel == "alpha" else RELEASE_JSON
    try:
        rel = fetch_json(url)
    except Exception as e:
        fail(f"获取版本信息失败: {e}")
    version = rel.get("version")
    pkg = package_for_platform(rel)
    if not pkg:
        fail(f"官方清单里没有 {PLATFORM_NAME} 对应的包")
    cur = current_cache_version()
    if not quiet:
        print(f"平台: {PLATFORM_NAME}   最新: {version}   本地: {cur or '无'}")
        print(f"要安装的插件: {', '.join(COMPILER_PLUGIN_DIRS)}" + (" + uniapp-cli-vite" if with_vite else ""))
    if not force and cur == version and is_compiler_root(current_compiler() or ""):
        if not quiet:
            print("已是最新，无需安装。")
        return version, current_compiler()
    ext = ".zip" if pkg["path"].endswith(".zip") else (".dmg" if pkg["path"].endswith(".dmg") else ".tar.gz")
    archive = os.path.join(DOWNLOADS, f"HBuilderX.{version}{ext}")
    if not os.path.isfile(archive) or os.path.getsize(archive) == 0:
        download(pkg["path"], archive)
    target = os.path.join(COMPILERS, version)
    if not is_compiler_root(target):
        extract_archive(archive, target, with_vite=with_vite)
    if not is_compiler_root(target):
        fail("安装后仍找不到编译器，请检查包结构")
    open(os.path.join(target, "VERSION"), "w").write(version + "\n")
    set_current(target)
    if not quiet:
        print(f"已安装到: {target}")
    return version, target


def ensure_compiler(args):
    root = resolve_compiler(getattr(args, "compiler", None))
    if root:
        return root
    if getattr(args, "no_auto_install", False):
        fail("找不到编译器，且已禁用自动安装。运行 unihx compiler install 或加 --compiler")
    log("未找到编译器，自动安装（首次需下载官方包，请稍候）…")
    install_compiler_package(with_vite=getattr(args, "vite", False))
    root = resolve_compiler(None)
    if not root:
        fail("自动安装后仍未找到编译器")
    log(f"编译器就绪: {root}")
    return root


def cmd_compiler(args):
    action = args.action or "install"
    if action == "list":
        print(f"缓存目录: {COMPILERS}")
        cur = current_compiler()
        if os.path.isdir(COMPILERS):
            for v in sorted(os.listdir(COMPILERS)):
                mark = "  <- 当前" if cur == os.path.join(COMPILERS, v) else ""
                print(f"  {v}{mark}")
        else:
            print("  （空）")
        return 0
    if action == "path":
        root = current_compiler()
        print(root or "（未安装）")
        return 0
    if action == "remove":
        version = args.version or current_cache_version()
        if not version:
            fail("没有可删除的版本")
        target = os.path.join(COMPILERS, version)
        if not os.path.isdir(target):
            fail(f"版本不存在: {version}")
        shutil.rmtree(target, ignore_errors=True)
        rest = sorted(os.listdir(COMPILERS)) if os.path.isdir(COMPILERS) else []
        if rest:
            set_current(os.path.join(COMPILERS, rest[-1]))
        elif os.path.lexists(CURRENT):
            os.remove(CURRENT)
        print(f"已删除 {version}")
        return 0

    # install
    if args.from_dir:
        src = os.path.abspath(args.from_dir)
        if not os.path.isdir(os.path.join(src, "plugins")):
            fail(f"{src} 下没有 plugins 目录")
        version, target = install_from_hbuilderx_dir(src, with_vite=args.with_vite)
        print(f"已从本地复制: {version}")
        print(f"  -> {target}")
        return 0

    version, target = install_compiler_package(channel=args.channel, force=args.force,
                                               with_vite=args.with_vite)
    print(f"当前编译器: {current_compiler()}")
    return 0


def cmd_update(args):
    url = ALPHA_JSON if args.channel == "alpha" else RELEASE_JSON
    try:
        rel = fetch_json(url)
    except Exception as e:
        fail(f"获取版本信息失败: {e}")
    version = rel.get("version")
    pkg = package_for_platform(rel)
    if not pkg:
        fail(f"官方清单里没有 {PLATFORM_NAME} 对应的包")
    cur = current_cache_version()
    print(f"平台: {PLATFORM_NAME}   最新版本: {version}   本地: {cur or '无'}   包: {pkg.get('code')} {pkg.get('size')}")
    if not args.force and cur == version:
        print("已是最新，无需更新。")
        return 0
    if args.check:
        return 0
    ext = ".zip" if pkg["path"].endswith(".zip") else (".dmg" if pkg["path"].endswith(".dmg") else ".tar.gz")
    archive = os.path.join(DOWNLOADS, f"HBuilderX.{version}{ext}")
    if not os.path.isfile(archive) or os.path.getsize(archive) == 0:
        download(pkg["path"], archive)
    target = os.path.join(COMPILERS, version)
    if not is_compiler_root(target):
        extract_archive(archive, target, with_vite=args.with_vite)
    if not is_compiler_root(target):
        fail("安装后仍找不到编译器，请检查包结构")
    open(os.path.join(target, "VERSION"), "w").write(version + "\n")
    set_current(target)
    print(f"已更新到 {version}: {target}")
    return 0


INIT_SCRIPTS_VUE2 = {
    "dev:mp-weixin": "cross-env NODE_ENV=development UNI_PLATFORM=mp-weixin vue-cli-service uni-build --watch",
    "build:mp-weixin": "cross-env NODE_ENV=production UNI_PLATFORM=mp-weixin vue-cli-service uni-build",
    "dev:h5": "cross-env NODE_ENV=development UNI_PLATFORM=h5 vue-cli-service uni-serve",
    "build:h5": "cross-env NODE_ENV=production UNI_PLATFORM=h5 vue-cli-service uni-build",
}
INIT_DEPS_VUE2 = {
    "@dcloudio/uni-cli-shared": "^2.0.2-5020620260917001",
    "@dcloudio/vue-cli-plugin-uni": "^2.0.2-5020620260917001",
    "@vue/cli-service": "4.2.3",
    "cross-env": "^7.0.3",
    "vue-template-compiler": "^2.6.14",
}
INIT_SCRIPTS_VUE3 = {
    "dev:mp-weixin": "uni -p mp-weixin",
    "build:mp-weixin": "uni build -p mp-weixin",
    "dev:h5": "uni",
    "build:h5": "uni build",
}
INIT_DEPS_VUE3 = {
    "@dcloudio/uni-cli-shared": "^3.0.0-alpha-5020520260821001",
    "@dcloudio/vite-plugin-uni": "^3.0.0-alpha-5020520260821001",
    "vite": "^5.2.8",
}


def cmd_init_cli(args):
    project = os.path.abspath(args.project)
    pkg_path = os.path.join(project, "package.json")
    pkg = read_package(project)
    if not pkg:
        fail(f"找不到 {pkg_path}")
    scripts = pkg.setdefault("scripts", {})
    deps = pkg.setdefault("devDependencies", {})
    if args.vue3:
        new_scripts, new_deps = INIT_SCRIPTS_VUE3, INIT_DEPS_VUE3
    else:
        new_scripts, new_deps = INIT_SCRIPTS_VUE2, INIT_DEPS_VUE2
    changes = []
    for k, v in new_scripts.items():
        if k not in scripts:
            scripts[k] = v
            changes.append(f"+ scripts.{k}")
    for k, v in new_deps.items():
        if k not in deps:
            deps[k] = v
            changes.append(f"+ devDependencies.{k}")
    print(f"项目: {project}")
    if not changes:
        print("无需修改，已包含 CLI 脚本和依赖。")
        return 0
    print("将添加：")
    for c in changes:
        print("  " + c)
    if not args.write:
        print("\n这是预览。加 --write 写入 package.json，然后执行 npm install。")
        return 0
    with open(pkg_path, "w", encoding="utf-8") as f:
        json.dump(pkg, f, ensure_ascii=False, indent=2)
    print("\n已写入。下一步：")
    print(f"  cd {project} && npm install --registry=https://registry.npmmirror.com")
    print(f"  unihx build --project {project} --target mp-weixin --npm")
    return 0


def cmd_versions(args):
    print(f"平台: {PLATFORM_NAME}")
    print(f"缓存目录: {COMPILERS}")
    if os.path.isdir(COMPILERS):
        cur = current_compiler()
        for v in sorted(os.listdir(COMPILERS)):
            mark = "  <- 当前" if cur == os.path.join(COMPILERS, v) else ""
            print(f"  {v}{mark}")
    for d in hbuilderx_candidates():
        if is_compiler_root(d):
            print(f"  HBuilderX: {d}")
    return 0


# ---------------- Android 基座运行（实验） ----------------

def _adb(*args, capture=True):
    return subprocess.run(["adb", *args], capture_output=capture, text=True)


def _pick_device(preferred=None):
    out = _adb("devices").stdout or ""
    devs = [ln.split()[0] for ln in out.splitlines()[1:] if ln.strip().endswith("device")]
    if preferred:
        return preferred if preferred in devs else None
    return devs[0] if devs else None


def _make_wgt(www, dest):
    import zipfile
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for base, _dirs, names in os.walk(www):
            for n in names:
                if n.endswith(".wgt"):
                    continue
                full = os.path.join(base, n)
                z.write(full, os.path.relpath(full, www).replace(os.sep, "/"))
    return dest


def _read_appid(project):
    try:
        import json5
        d = json5.load(open(os.path.join(project, "manifest.json"), encoding="utf-8"))
    except Exception:
        try:
            d = json.load(open(os.path.join(project, "manifest.json"), encoding="utf-8"))
        except Exception:
            d = {}
    return d.get("id") or d.get("appid") or ""


def cmd_android(args):
    """把 app-plus 资源推到 DCloud 基座运行（不依赖 HBuilderX）。"""
    import time
    udid = _pick_device(args.device)
    if not udid:
        log("未找到已连接的 Android 设备（adb devices）")
        return 1

    if args.www:
        www = args.www
    else:
        if not args.project:
            log("需要 --project 或 --www")
            return 1
        www = tempfile.mkdtemp(prefix="unihx-android-")
        log(f"编译 app-plus → {www}")
        cmd = [sys.executable, "-m", "unihx.cli", "build",
               "--project", args.project, "--target", "app", "--out", www]
        child_env = os.environ.copy()
        child_env.setdefault("BROWSERSLIST", "Android >= 4.4, ios >= 9, chrome >= 49")
        if args.watch:
            cmd += ["--mode", "dev"]
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             env=child_env)
            for _ in range(120):
                if os.path.isfile(os.path.join(www, "app-service.js")):
                    break
                time.sleep(1)
            log("dev watch 构建已启动")
        else:
            r = subprocess.run(cmd, text=True, env=child_env)
            if r.returncode != 0:
                log("编译失败")
                return r.returncode

    appid = args.appid or (_read_appid(args.project) if args.project else "") or "HBuilder"

    if args.custom_base:
        # 自定义基座：直接把资源推到该 app 的 www 再启动（无需调试服务）
        dest = f"/sdcard/Android/data/{args.base_package}/apps/{appid}/www"
        log(f"自定义基座模式：{args.base_package} / {appid}")
        def _launch():
            res = _adb("-s", udid, "shell", "cmd", "package", "resolve-activity",
                       "--brief", args.base_package).stdout or ""
            comps = [ln.strip() for ln in res.splitlines() if "/" in ln]
            comp = comps[-1] if comps else f"{args.base_package}/io.dcloud.PandoraEntryActivity"
            _adb("-s", udid, "shell", "am", "force-stop", args.base_package)
            _adb("-s", udid, "shell", "am", "start", "-n", comp)

        _adb("-s", udid, "shell", "force-stop", args.base_package)
        _adb("-s", udid, "shell", "rm", "-rf", dest)
        _adb("-s", udid, "shell", "mkdir", "-p", dest)
        r = _adb("-s", udid, "push", www + "/.", dest + "/")
        log((r.stdout or r.stderr or "").strip().splitlines()[-1] if (r.stdout or r.stderr) else "push done")
        _launch()
        log("已启动自定义基座")

        if args.watch:
            def _snap():
                out = {}
                for base, _d, names in os.walk(www):
                    for n in names:
                        f = os.path.join(base, n)
                        try:
                            out[f] = os.path.getmtime(f), os.path.getsize(f)
                        except OSError:
                            pass
                return out

            last = _snap()
            log("已开启热更新监听（修改源码后自动推送并重启基座）")
            try:
                while True:
                    time.sleep(1.5)
                    cur = _snap()
                    changed = [f for f in cur if last.get(f) != cur[f]]
                    last = cur
                    if not changed:
                        continue
                    for f in changed:
                        rel = os.path.relpath(f, www).replace(os.sep, "/")
                        sub = os.path.dirname(rel)
                        if sub:
                            _adb("-s", udid, "shell", "mkdir", "-p", f"{dest}/{sub}")
                        _adb("-s", udid, "push", f, f"{dest}/{rel}")
                    _launch()
                    log(f"[热更新] 推送 {len(changed)} 个文件并重启基座")
            except KeyboardInterrupt:
                pass
            return 0
        return 0
    if appid.startswith("__UNI__") is False and args.appid is None and args.project:
        pass

    try:
        from websockets.sync.server import serve as ws_serve
    except Exception:
        log("需要 python websockets：sudo apt install python3-websockets")
        return 1

    import functools
    import http.server
    import mimetypes
    import urllib.parse
    import socket
    import socketserver
    import threading
    import time
    import urllib.parse
    import zipfile

    wgt = _make_wgt(www, os.path.join(www, "app.wgt"))

    def free_port():
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        return p

    http_port = free_port()
    ws_port = args.port or free_port()

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            rel = (q.get("path") or [""])[0]
            full = wgt if rel == "app.wgt" else os.path.join(www, rel)
            if not os.path.isfile(full):
                self.send_error(404)
                return
            data = open(full, "rb").read()
            self.send_response(200)
            ctype = "application/zip" if rel == "app.wgt" else (mimetypes.guess_type(full)[0] or "application/octet-stream")
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", http_port), H)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    files = [{"action": "writeFile",
              "sourcePath": f"http://127.0.0.1:{http_port}/static/?path=app.wgt",
              "name": "app.wgt", "path": "", "fullPackage": True, "firstInstall": True}]
    msg = {"mobile": {}, "contents": {"project": {"type": "UniApp", "appID": appid},
                                      "fileInfo": files,
                                      "app": {"appID": appid, "customBase": False},
                                      "refreashType": "reload", "associatePaths": ""}}

    state = {"ws": None}

    def handler(ws):
        state["ws"] = ws
        log("[基座] 已连接")
        try:
            first = ws.recv()
            log(f"[基座] {str(first)[:160]}")
            try:
                msg["mobile"] = json.loads(first).get("mobile") or {}
            except Exception:
                pass
        except Exception:
            pass
        ws.send(json.dumps(msg, ensure_ascii=False))
        log(f"[推送] {len(msg['contents']['fileInfo'])} 个文件（{appid}）")
        try:
            for m in ws:
                log(f"[基座] {str(m)[:200]}")
        except Exception:
            pass

    server = ws_serve(handler, "127.0.0.1", ws_port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"HTTP={http_port} WS={ws_port} appid={appid}")

    _adb("-s", udid, "reverse", f"tcp:{http_port}", f"tcp:{http_port}")
    _adb("-s", udid, "reverse", f"tcp:{ws_port}", f"tcp:{ws_port}")
    _adb("-s", udid, "shell", "am", "force-stop", args.base_package)
    r = _adb("-s", udid, "shell", "am", "start", "-n",
             f"{args.base_package}/io.dcloud.debug.PullDebugActivity",
             "--es", "ip", "127.0.0.1", "--es", "port", str(ws_port),
             "--es", "appid", appid)
    log((r.stdout or r.stderr or "").strip())
    if args.watch:
        def _snap():
            out = {}
            for base, _d, names in os.walk(www):
                for n in names:
                    f = os.path.join(base, n)
                    try:
                        out[f] = os.path.getmtime(f), os.path.getsize(f)
                    except OSError:
                        pass
            return out

        def _watch():
            last = _snap()
            while True:
                time.sleep(1.5)
                cur = _snap()
                changed = [f for f in cur if last.get(f) != cur[f]]
                last = cur
                if not changed or not state["ws"]:
                    continue
                entries = []
                for f in changed:
                    rel = os.path.relpath(f, www).replace(os.sep, "/")
                    url = f"http://127.0.0.1:{http_port}/static/?path=" + urllib.parse.quote(rel)
                    entries.append({"action": "writeFile", "sourcePath": url,
                                    "name": os.path.basename(f),
                                    "path": os.path.dirname(rel).strip("/"),
                                    "fullPackage": False, "firstInstall": False})
                upd = {"mobile": msg["mobile"], "contents": {
                    "project": {"type": "UniApp", "appID": appid},
                    "fileInfo": entries,
                    "app": {"appID": appid, "customBase": False},
                    "refreashType": "reload", "associatePaths": ""}}
                try:
                    state["ws"].send(json.dumps(upd, ensure_ascii=False))
                    log(f"[热更新] 推送 {len(entries)} 个文件")
                except Exception as e:
                    log(f"[热更新] 发送失败: {e}")

        threading.Thread(target=_watch, daemon=True).start()
        log("已开启热更新监听（修改源码后自动推送）")

    log("运行中，Ctrl+C 结束")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for port in (http_port, ws_port):
            _adb("-s", udid, "reverse", "--remove", f"tcp:{port}")
        server.shutdown()
        httpd.shutdown()
    return 0



def main():
    p = argparse.ArgumentParser(prog="unihx", description="uni-app 离线打包与本地调试工具")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="环境自检")
    d.add_argument("--compiler")
    d.set_defaults(func=cmd_doctor)

    s = sub.add_parser("list-scripts", help="列出项目的自定义发行模式")
    s.add_argument("--project", required=True)
    s.set_defaults(func=cmd_list_scripts)

    b = sub.add_parser("build", help="离线打包")
    b.add_argument("--project", required=True)
    b.add_argument("--target", default="mp-weixin", choices=list(TARGETS))
    b.add_argument("--script", help="uni-app.scripts 里的名字")
    b.add_argument("--mode", choices=["prod", "dev"], default="prod")
    b.add_argument("--out")
    b.add_argument("--wgt-name", help="wgt 文件名（默认 <项目名>.wgt）")
    b.add_argument("--compiler")
    b.add_argument("--vite", action="store_true", help="使用 vite 编译器（vue3 项目）")
    b.add_argument("--install", action="store_true", help="缺少 node_modules 时自动 npm install")
    b.add_argument("--no-auto-install", action="store_true", help="找不到编译器时不自动下载")
    b.add_argument("--registry", help="npm 源（配合 --install）")
    b.add_argument("--npm", action="store_true", help="用项目自己的 npm scripts 构建（无需 HBuilderX 编译器）")
    b.add_argument("--isolate", action="store_true", help="强制隔离工作区构建")
    b.add_argument("--no-isolate", action="store_true", help="禁用隔离工作区")
    b.add_argument("--recreate-workspace", action="store_true", help="重建隔离工作区")
    b.set_defaults(func=cmd_build)

    r = sub.add_parser("run", help="watch 编译并打开微信开发者工具")
    r.add_argument("--project", required=True)
    r.add_argument("--script")
    r.add_argument("--compiler")
    r.add_argument("--vite", action="store_true")
    r.add_argument("--no-devtools", action="store_true")
    r.add_argument("--install", action="store_true", help="缺少 node_modules 时自动 npm install")
    r.add_argument("--no-auto-install", action="store_true", help="找不到编译器时不自动下载")
    r.add_argument("--registry", help="npm 源（配合 --install）")
    r.add_argument("--npm", action="store_true", help="用项目自己的 npm scripts 运行")
    r.add_argument("--isolate", action="store_true", help="强制隔离工作区运行")
    r.add_argument("--no-isolate", action="store_true", help="禁用隔离工作区")
    r.add_argument("--recreate-workspace", action="store_true", help="重建隔离工作区")
    r.set_defaults(func=cmd_run)

    sv = sub.add_parser("serve", help="H5 开发服务器（热更新）")
    sv.add_argument("--project", required=True)
    sv.add_argument("--script")
    sv.add_argument("--compiler")
    sv.add_argument("--vite", action="store_true")
    sv.add_argument("--install", action="store_true")
    sv.add_argument("--no-auto-install", action="store_true", help="找不到编译器时不自动下载")
    sv.add_argument("--registry")
    sv.add_argument("--npm", action="store_true", help="用项目自己的 npm scripts 运行")
    sv.add_argument("--isolate", action="store_true", help="强制隔离工作区运行")
    sv.add_argument("--no-isolate", action="store_true", help="禁用隔离工作区")
    sv.add_argument("--recreate-workspace", action="store_true", help="重建隔离工作区")
    sv.set_defaults(func=cmd_serve)

    u = sub.add_parser("update", help="在线更新编译器（官方渠道）")
    u.add_argument("--check", action="store_true")
    u.add_argument("--force", action="store_true")
    u.add_argument("--channel", choices=["release", "alpha"], default="release")
    u.add_argument("--with-vite", action="store_true")
    u.set_defaults(func=cmd_update)

    ic = sub.add_parser("init-cli", help="把项目改造成标准 uni-app CLI 项目（摆脱 HBuilderX）")
    ic.add_argument("--project", required=True)
    ic.add_argument("--vue3", action="store_true", help="使用 vite（vue3）模板")
    ic.add_argument("--write", action="store_true", help="写入 package.json")
    ic.set_defaults(func=cmd_init_cli)

    c = sub.add_parser("compiler", help="编译器插件包管理（install/list/path/remove）")
    c.add_argument("action", nargs="?", default="install",
                   choices=["install", "list", "path", "remove"])
    c.add_argument("--from-dir", help="从已有 HBuilderX 目录复制插件（不下载）")
    c.add_argument("--channel", choices=["release", "alpha"], default="release")
    c.add_argument("--with-vite", action="store_true")
    c.add_argument("--force", action="store_true")
    c.add_argument("--version", help="remove 时指定版本")
    c.set_defaults(func=cmd_compiler)

    a = sub.add_parser("android", help="运行到 Android 基座（实验，不依赖 HBuilderX）")
    a.add_argument("--project")
    a.add_argument("--www", help="直接使用已编译的 app-plus 目录")
    a.add_argument("--device", help="adb 设备序列号")
    a.add_argument("--appid")
    a.add_argument("--base-package", default="io.dcloud.HBuilder")
    a.add_argument("--port", type=int, help="固定 WebSocket 端口")
    a.add_argument("--watch", action="store_true", help="监听输出目录变化并热更新")
    a.add_argument("--custom-base", action="store_true", help="自定义基座模式：直接推资源到该 app 的 www")
    a.set_defaults(func=cmd_android)

    v = sub.add_parser("versions", help="查看已安装编译器")
    v.set_defaults(func=cmd_versions)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
