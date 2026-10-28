# 编译器：环境变量、自定义发行模式与直编原理

## 1. 两套编译器

HBuilderX 内置两套 uni-app 编译器：

| 目录 | 技术栈 | 入口 |
|---|---|---|
| `plugins/uniapp-cli` | webpack + `@vue/cli-service` | `bin/uniapp-cli.js` |
| `plugins/uniapp-cli-vite` | Vite | `node_modules/@dcloudio/vite-plugin-uni/bin/uni.js` |

判断用哪套：

- 项目里存在 `components/xxx.vue` 这类依赖 `baseUrl` 的裸导入 → **webpack**（vite 会解析失败）
- 标准 uni-app CLI 模板（`@dcloudio/*` 依赖 + vite）→ **vite**

## 2. 环境变量契约

```
UNI_PLATFORM      mp-weixin / h5 / app ...
UNI_INPUT_DIR     项目根目录
UNI_OUTPUT_DIR    产物目录
NODE_ENV          production / development（development 时 watch）
UNI_MINIMIZE      true 时压缩
UNI_SCRIPT        uni-app.scripts 中的自定义模式名
```

webpack 入口 (`bin/uniapp-cli.js`) 逻辑：

```js
process.env.UNI_INPUT_DIR  = path.resolve(process.env.UNI_INPUT_DIR)
process.env.UNI_OUTPUT_DIR = path.resolve(process.env.UNI_OUTPUT_DIR)
if (process.env.UNI_SCRIPT) initCustomScript(process.env.UNI_SCRIPT, ...)
const service = new Service(process.env.VUE_CLI_CONTEXT || process.cwd())
service.run(NODE_ENV === 'development' && platform === 'h5' ? 'uni-serve' : 'uni-build', {...})
```

## 3. 自定义发行模式（uni-app.scripts）

`package.json`：

```json
"uni-app": {
  "scripts": {
    "custom-platform": {
      "title": "微信小程序体验版",
      "env": { "UNI_PLATFORM": "mp-weixin", "UAT": true },
      "define": { "OPPEIN-MP-UAT": true }
    }
  }
}
```

`@dcloudio/uni-cli-shared/lib/package.js`：

```js
initCustomScript(name, pkgPath) {
  const scriptOptions = pkg['uni-app'].scripts[name]
  // 其它脚本的 define 置 false，保证互斥
  process.env.UNI_PLATFORM = scriptOptions.env.UNI_PLATFORM
  process.env.UNI_SCRIPT = name
  process.UNI_SCRIPT_ENV = scriptOptions.env
  process.UNI_SCRIPT_DEFINE = scriptOptions.define
}
```

`define` 用于条件编译：

```js
// config/publish.js
if (process.env.NODE_ENV === 'production') {
  // #ifdef OPPEIN-MP-UAT
  publishEnv = 'test';
  // #endif
}
```

所以「体验版」构建 = `UNI_SCRIPT=custom-platform` + `NODE_ENV=production`。

> **平台由脚本决定**：`env.UNI_PLATFORM` 可以是 `mp-weixin`、`h5`、`app-plus` 等。
> unihx 会以脚本平台为准（覆盖 `--target`），因此同一个项目可以有小程序体验版，
> 也可以有 H5 体验版（例如 `UNI_PLATFORM=h5` + `OPPEIN-H5-UAT`）。

## 4. 直编命令

```bash
cd <compiler>/plugins/uniapp-cli
NODE_ENV=production \
UNI_PLATFORM=mp-weixin \
UNI_SCRIPT=custom-platform \
UNI_INPUT_DIR=/path/to/project \
UNI_OUTPUT_DIR=/path/to/project/unpackage/dist/build/mp-weixin \
  <compiler>/plugins/node/node bin/uniapp-cli.js
```

产物与 HBuilderX 一致（文件数可对比验证）。

## 5. 离线打包目标（unihx build --target）

| target | UNI_PLATFORM | 产物目录 |
|---|---|---|
| `mp-weixin` | `mp-weixin` | `unpackage/dist/build/mp-weixin` |
| `app` | `app-plus` | `unpackage/dist/build/app-plus` |
| `wgt` | `app-plus` | `unpackage/release/<name>.wgt` |
| `h5` | `h5` | `unpackage/dist/build/h5` |

`wgt` 的实现：先按 `app-plus` 编译，再把产物目录打成 zip（条目在根目录）。
实测与 HBuilderX 生成的 wgt 文件列表完全一致（56 个文件、2.7M）。

## 6. mp-weixin 产物细节

- `project.config.json` 由 `uni-mp-weixin/dist/uni.compiler.js` 生成
- appid 合并规则在 `@dcloudio/uni-cli-shared/dist/json/mp/project.js`：
  - 项目根有 `project.wx.json` / `project.config.json` → 直接拷贝
  - 否则读取 `manifest.json` 的 `mp-weixin` 段中 `appid/setting/...`
  - 都没有 → `touristappid`
- 自动拷贝 `sitemap.json`、`ext.json`、`custom-tab-bar`、`functional-pages`、`project.private.config.json`

## 7. 编译器在线更新

官方渠道：

- 版本清单：`https://download1.dcloud.net.cn/hbuilderx/release.json`
- Linux 全量包：`linux_full_x64`（约 1.3GB，DCloud 维护）

`unihx update` 会：

1. 拉取 `release.json`，与本地版本比较
2. 断点续传下载到 `~/.local/share/unihx/downloads/`
3. 用 `tar --wildcards --strip-components=1` 只解压 `plugins/uniapp-cli`、`plugins/node`（可选 `uniapp-cli-vite`）
4. 安装到 `~/.local/share/unihx/compilers/<version>` 并切换 `compiler` 软链

## 8. 模块解析（NODE_PATH）

编译器从 HBuilderX 插件目录运行，项目里的 `vue.config.js` 等文件如果
`require('webpack')`，Node 的默认解析路径里没有它。

unihx 会设置：

```
NODE_PATH=<compiler>/plugins/uniapp-cli/node_modules:<project>/node_modules
```

另外项目若声明了 npm 依赖但没有 `node_modules`，用 `--install` 自动安装。

## 9. 已知坑

- `manifest.json` 里 appid 为 `null` 时，产物 appid 会回落到 `touristappid`
- Linux 版 HBuilderX 的插件仓库不可用；`compile-less` 等需手动补
- 编译期间产物目录会被清空，开发者工具可能误报缺文件（重新编译即可）
- `UNI_OUTPUT_DIR` 不存在时，vite 会用默认 `dist/build/<platform>`

## 9. 自包含编译器包（不安装 HBuilderX）

老项目只需要以下目录即可编译，无需 HBuilderX 主程序：

```
<compiler>/plugins/
├── uniapp-cli            uni-app 编译器（webpack）
├── node                  Node 运行时
├── compile-less          less 编译插件
├── compile-node-sass     node-sass（libsass）编译插件
└── compile-dart-sass     dart-sass 编译插件
```

运行时 unihx 会设置：

```
UNI_HBUILDERX_PLUGINS=<compiler>/plugins
UNI_INPUT_DIR=<项目>        # 只读
UNI_OUTPUT_DIR=<项目>/unpackage/...
```

`unihx update` 从官方 HBuilderX 包中只提取这几个目录（Linux 包大，Windows/macOS 包约 200MB），
之后 `unihx build` 不再需要 HBuilderX 安装，也不需要 `hbuilderx-cli` 主进程。

也可以直接指定已有插件目录：

```bash
unihx build --project <proj> --target mp-weixin --compiler /path/to/compiler
```
