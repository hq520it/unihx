# android-push（实验）

不用 HBuilderX，把 app-plus 资源推到 DCloud 标准基座运行。

## 原理（逆向所得）

1. PC 端起 HTTP + WebSocket 服务
2. `adb reverse tcp:<port> tcp:<port>` 把手机端口映射到 PC
3. `am start -n <基座包>/io.dcloud.debug.PullDebugActivity --es ip 127.0.0.1 --es port <ws> --es appid <id>`
4. 基座连上 WS 后，服务端推送：

```json
{"mobile": {...}, "contents": {
  "project": {"type": "UniApp", "appID": "HBuilder"},
  "app": {"appID": "HBuilder", "customBase": false},
  "fileInfo": [{"action":"writeFile","sourcePath":"http://127.0.0.1:<http>/static/?path=x","name":"x","path":"","fullPackage":true,"firstInstall":true}],
  "refreashType": "reload",
  "associatePaths": ""
}}
```

- `fullPackage=true` 时，服务端必须返回**裸 wgt/zip 字节**（不能再压一层）
- 基座解压到 `apps/<appid>/www`（标准基座固定 `HBuilder`）
- 之后可发 `refreashType: "restart"` 触发重启

## 用法

```bash
# 1. 构建 app-plus
unihx build --project /path/to/app --target app --out /tmp/app/www

# 2. 打 wgt（可选，push.py 会自动处理目录）
(cd /tmp/app/www && zip -qr app.wgt .)

# 3. 推送
python3 push.py /tmp/app/www HBuilder
```

## 验证结果

- ✅ Android 11 模拟器（WebView 83）+ 基座 15.26：`HELLO FROM UNIHX ANDROID` 正常渲染
- ❌ AUTOID Q9 真机（WebView **74**）：白屏（HBuilderX 官方运行同样白屏）
- ✅ 热更新：`unihx android --watch`，改源码后自动推送并 reload（截图 `docs/assets/android-hotreload.png`）

## 自定义基座验证

`uni-app-xmes` 自带调试基座 `unpackage/debug/android_debug.apk`（包名 `com.oppein.xmestest`，arm-only）。

```bash
unihx android --www /tmp/xmes-app --appid __UNI__BCAAF06   --base-package com.oppein.xmestest --custom-base
```

真机日志确认基座加载了我们推送的资源：

```
createWindow before _url=file:///storage/emulated/0/Android/data/com.oppein.xmestest/apps/__UNI__BCAAF06/www/__uniappview.html
```

启动页正常显示（`docs/assets/android-custom-base.png`），后续页面仍受 WebView 74 限制。

## 白屏根因与修复

新编译的 app-plus 产物默认目标较新，会保留 optional chaining（`?.`）等语法；
Chrome 74 解析失败 → 白屏。

修复：构建时设置

```bash
BROWSERSLIST="Android >= 4.4, ios >= 9, chrome >= 49"
```

unihx 的 `build --target app` / `android` 已默认注入该变量，产物变为 ES5。
实测真机（WebView 74）可正常显示（`docs/assets/android-pda-es5.png`）。

## 已知问题

- 标准基座加载后 view 层可能报 `Failed to receiveTasks, instance (N) is not available`，页面白屏
- 自定义基座资源内置在 APK 中，外部替换会被版本校验覆盖，需要重新云打包
