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

## 已知问题

- 标准基座加载后 view 层可能报 `Failed to receiveTasks, instance (N) is not available`，页面白屏
- 自定义基座资源内置在 APK 中，外部替换会被版本校验覆盖，需要重新云打包
