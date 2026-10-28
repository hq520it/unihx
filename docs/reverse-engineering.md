# HBuilderX CLI 本地 IPC 协议（逆向笔记）

HBuilderX 的 `hbuilderx-cli` 只是一个薄客户端：它通过 Unix socket 与 HBuilderX 主进程通信。
本文记录逆向所得，供 `hx-rpc` 实现参考。

## 1. 连接

- Socket：`/tmp/hxf<32位十六进制>`（主进程启动时创建）
- 传输：`SOCK_STREAM`，自定义分帧：

```
Content-Length: <N>\r\n\r\n<JSON>
```

## 2. 握手

客户端先发送：

```json
{"method":"client verify","params":{
  "clientid": <客户端 pid>,
  "env": { ... 环境变量 ... },
  "source": "/path/to/HBuilderX",
  "workdir": "/path/to/HBuilderX"
}}
```

服务端返回：

```json
{"method":"auth return","params":{"auth":true,"clientid":...,"serverdir":"...","workdir":"..."}}
```

> **关键**：`source` 和 `workdir` 必须等于 HBuilderX 安装目录，否则 `auth:false`，
> 之后所有命令都返回空的 `break` 帧——这就是官方 CLI 里写死的行为。

## 3. 命令

```json
{"method":"<命令>","params":{...}}
```

命令名就是 CLI 的子命令字符串，例如：

- `ver`
- `project list`
- `publish mp-weixin`
- `publish app`
- `pack`

参数直接来自 CLI 选项，常见字段：

| 参数 | 说明 |
|---|---|
| `clientid` | 客户端 pid |
| `env` | 环境变量快照 |
| `workdir` | 工作目录 |
| `project` | 项目绝对路径 |
| `appid` / `upload` / `version` / `privatekey` / `description` / `robot` | 小程序发布参数（全部为字符串） |

## 4. 输出

服务端**主动推送**日志帧，客户端必须逐条 ack：

```json
{"id":"100050","method":"log","params":{"message":"...","source":<pid>}}
→ 客户端回 {"id":"100050","result":null}
```

结束标记：

```json
{"id":"100051","method":"break","params":{"code":0,"result":"OK","type":"project list"}}
→ 客户端回 {"id":"100051","result":null}
```

## 5. 实现要点（hx-rpc）

1. 自动发现最新 socket
2. 握手时 `source`/`workdir` 指向 HBuilderX 目录
3. 命令帧**不要**带 `source`，否则服务端会返回帮助文本
4. 逐条 ack `log` 帧，否则服务端不再继续推送
5. 成功判定看日志中的「成功」字样，而不是 `break.result`（发布时可能为空）

## 6. 二进制侧的发现

在 HBuilderX 的 Linux 二进制（未剥离符号）中可以看到：

- `CliPublish`：CLI 发布入口，Qt 元方法签名形如 `[CliPublish/publish${}.../params]`
- `MainHostPublish`：GUI/RPC 桥
  - `initialize(RPC::IMsgConnection*)`
  - `compileUniappx(QJsonValue const&)`
  - `uploadWeapp(QJsonValue const&)` —— 内部调用 `HB::Util::syncExec`，即同步执行外部进程
- Windows 版 `HBuilderX.dll` 为 PE32 i386 + Qt5，无 WebEngine/CEF，逻辑集中在 DLL
- 上传逻辑不在 JS 层：`@dcloudio` 包里搜不到 `miniprogram-ci` 或微信 API 域名

## 7. 结论

- 编译（mp-weixin/wgt/appResource）都可以完全脱离 HBuilderX 主进程，直接调用官方编译器
- HBuilderX 主进程只在云打包、登录态、上传等场景需要
- 因此本项目同时提供两条路径：`unihx build`（直编）与 `hx-rpc`（驱动 HBuilderX）
