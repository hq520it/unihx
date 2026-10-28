# FAQ

## 需要安装 HBuilderX 吗？

不是必须的。

- 已安装 HBuilderX：`unihx` 会自动探测 `~/.local/opt/HBuilderX`、`/opt/HBuilderX`，直接复用其编译器
- 没有安装：运行 `unihx update`，从 DCloud 官方渠道下载编译器

## 为什么更新要下载 1.3GB？

DCloud 官方 Linux 渠道只提供 `linux_full_x64` 全量包（`release.json` 中唯一 Linux 包）。
`unihx update` 只解压其中的 `plugins/uniapp-cli`、`plugins/node`（可选 vite），但下载仍是全量。
已安装 HBuilderX 的用户可以跳过下载，直接复用。

## 「体验版」能自动设置吗？

- **普通开发者账号**：不能。微信没有公开 API；上传只产生「开发版」，
  「选为体验版」必须在 mp.weixin.qq.com 后台点一下。
- **第三方平台 / 服务商代管**：有官方接口「上传代码并生成体验版」（`commit`）
  和「获取体验版二维码」（`gettrialqrcode`），可以全自动。

## 本地调试用哪个微信开发者工具？

Linux 原生版（提供 `wechat-devtools-cli`）。`unihx run` 在编译成功后自动调用：

```bash
wechat-devtools-cli cli open --project <dist>
```

## 为什么 hx-rpc 的 `source` 必须是 HBuilderX 目录？

服务端用它做鉴权。不一致时 `auth:false`，后续所有命令都返回空 `break`。
详见 [reverse-engineering.md](reverse-engineering.md)。

## 产物 appid 变成了 touristappid？

检查 `manifest.json` 的 `mp-weixin.appid` 是否为 `null` 或缺失。
合并逻辑见 [compiler.md](compiler.md) 第 5 节。

## 支持 vue2 老项目吗？

支持。`uniapp-cli`（webpack）就是给这类项目用的；
`components/xxx.vue` 这类裸导入也只有 webpack 能解析。

## `unihx build` 报「找不到编译器」

```bash
unihx doctor
```

确认 `compiler` 一行有值；没有就 `unihx update` 或设置 `HX_HOME` / `UNI_COMPILER_DIR`。

## 会修改项目文件吗？

不会。构建只写 `unpackage/dist/`（或 `--out` 指定目录）。

## 报错 `Cannot find module 'webpack'`

项目的 `vue.config.js` 里 `require('webpack')`，但项目没装依赖。
unihx 会把编译器自带的 `node_modules` 加进 `NODE_PATH`，通常即可解决；
如果仍报错，检查 `--vite` 是否用错（vue2 项目不要加）。

## 报错 `文件查找失败：'weixin-js-sdk'`

项目 `package.json` 声明了 npm 依赖但目录里没有 `node_modules`。
HBuilderX 托管项目常见这种情况，执行：

```bash
unihx build --project <proj> --target mp-weixin --install
# 或手动：cd <proj> && npm install
```

## 自定义发行模式是给 H5 用的，选不了？

可以选。发行模式与平台的关系由脚本里的 `env.UNI_PLATFORM` 决定：

```json
"custom-platform": {
  "env": { "UNI_PLATFORM": "h5", "UAT": true },
  "define": { "OPPEIN-H5-UAT": true }
}
```

GUI 选中该模式后会自动把发行目标切到 H5 并锁定；CLI 直接：

```bash
unihx build --project <proj> --target h5 --script custom-platform
unihx serve --project <proj> --script custom-platform   # H5 调试
```

## 支持 Windows / macOS 吗？

CLI 已支持：

- **编译器探测**：Windows 查 `Program Files\HBuilderX` 等；macOS 查 `/Applications/HBuilderX.app/Contents/HBuilderX`
- **在线更新**：按平台选 `win_simple` / `mac_simple(_arm64)` / `linux_full_x64`，自动处理 zip/dmg/tar
- **微信开发者工具**：Windows 找 `cli.bat`，macOS 找 `.app/Contents/MacOS/cli`
- **安装**：Windows 用 `install.ps1`，Linux/macOS 用 `install.sh`

注意：Windows/macOS 官方包是「标准版」，可能不含 `uniapp-cli` 插件；
建议直接复用已安装的 HBuilderX。GUI 终端面板在 Windows 上需要 ConPTY（后续支持）。

## unihx 怎么判断项目类型？

看 `package.json` 是否声明了官方 CLI 编译器依赖：

- 含 `@dcloudio/vue-cli-plugin-uni`（vue2）或 `@dcloudio/vite-plugin-uni`（vue3）→ **CLI 项目**，走 `npm run build/dev`
- 否则 → **HBuilderX 老项目**，用内置编译器直接编译

## 老项目会改我的 package.json 吗？

不会。

- 不合并、不写回、不装 `node_modules`
- 编译器只读源码和 `manifest.json`，产物写 `unpackage/`
- 自定义发行模式（`uni-app.scripts`）照常生效

## 老项目里 import 了 npm 包（如 weixin-js-sdk）怎么办？

这类项目需要在项目里有一次 `node_modules`（webpack 从项目目录解析依赖）：

```bash
unihx build --project <proj> --target mp-weixin --install
```

`--install` 只新增 `node_modules`，不改源码和 `package.json`。
（`--isolate` 是实验特性：复制源码到缓存工作区再编译，用于彻底隔离，但依赖安装较慢。）
