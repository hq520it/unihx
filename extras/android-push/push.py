#!/usr/bin/env python3
"""PoC: 不用 HBuilderX，直接把 app-plus 资源推到 DCloud 基座运行。"""
import asyncio
import http.server
import json
import mimetypes
import os
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse

ROOT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/android-run/app-plus"
APPID = sys.argv[2] if len(sys.argv) > 2 else None

if not APPID:
    import re
    mf = "/mnt/225GB/uni-app/uni-app-mom/manifest.json"
    txt = open(mf, encoding="utf-8").read()
    m = re.search(r'"appid"\s*:\s*"([^"]+)"', txt)
    APPID = m.group(1) if m else "__UNI__TEST"

print("ROOT =", ROOT)
print("APPID =", APPID)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


HTTP_PORT = free_port()
WS_PORT = free_port()
print(f"HTTP={HTTP_PORT} WS={WS_PORT}")


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[http]", self.address_string(), fmt % args, flush=True)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.rstrip("/") != "/static":
            self.send_error(404)
            return
        qs = urllib.parse.parse_qs(parsed.query)
        rel = (qs.get("path") or [""])[0]
        full = os.path.normpath(os.path.join(ROOT, rel))
        if not full.startswith(os.path.abspath(ROOT)) or not os.path.isfile(full):
            print("[http] 404", rel, flush=True)
            self.send_error(404)
            return
        data = open(full, "rb").read()
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)



httpd = socketserver.ThreadingTCPServer(("127.0.0.1", HTTP_PORT), Handler)
httpd.daemon_threads = True
threading.Thread(target=httpd.serve_forever, daemon=True).start()


def file_entries():
    if os.environ.get("WGT"):
        url = f"http://127.0.0.1:{HTTP_PORT}/static/?path=app.wgt"
        return [{"action": "writeFile", "sourcePath": url, "name": "app.wgt", "path": "",
                 "fullPackage": True, "firstInstall": True}]
    out = []
    for base, _dirs, names in os.walk(ROOT):
        for n in names:
            full = os.path.join(base, n)
            rel = os.path.relpath(full, ROOT).replace(os.sep, "/")
            url = f"http://127.0.0.1:{HTTP_PORT}/static/?path=" + urllib.parse.quote(rel)
            dest = os.path.dirname(rel).replace("\\", "/").strip("/")
            out.append({"action": "writeFile", "sourcePath": url, "name": n, "path": dest,
                        "fullPackage": False, "firstInstall": False})
    out.sort(key=lambda x: x["name"])
    return out


MOBILE = {"clientID": "AUTOID Q9", "name": "AUTOID Q9", "platform": "Android",
          "udid": "", "version": "10"}

MESSAGE = {
    "mobile": MOBILE,
    "contents": {
        "project": {"type": "UniApp", "appID": APPID},
        "fileInfo": file_entries(),
        "app": {"appID": APPID, "customBase": False},
        "refreashType": "reload",
        "associatePaths": "",
    },
}
print("files =", len(MESSAGE["contents"]["fileInfo"]))


PUSHED = {"done": False}

async def handler(ws):
    print("[ws] connected", ws.remote_address, flush=True)
    if PUSHED["done"]:
        print("[ws] 已推送过，保持连接不重推", flush=True)
        try:
            async for _ in ws:
                pass
        except Exception:
            pass
        return
    PUSHED["done"] = True
    try:
        first = await asyncio.wait_for(ws.recv(), timeout=3)
        print("[ws] recv:", repr(first)[:400], flush=True)
        try:
            MOBILE.update(json.loads(first).get("mobile") or {})
        except Exception:
            pass
    except asyncio.TimeoutError:
        pass
    await asyncio.sleep(8)   # 等基座初始化完成再推送
    MESSAGE["mobile"] = MOBILE
    MESSAGE["mobile"] = MOBILE
    warm = json.loads(json.dumps(MESSAGE))
    warm["contents"]["fileInfo"] = warm["contents"]["fileInfo"][:1]
    await ws.send(json.dumps(warm, ensure_ascii=False))
    print("[ws] 预热消息已发，等基座启动", flush=True)
    for _ in range(60):
        try:
            m = await asyncio.wait_for(ws.recv(), timeout=1)
        except asyncio.TimeoutError:
            continue
        print("[ws] recv:", repr(m)[:200], flush=True)
        if "application_Started" in str(m):
            print("[ws] 基座已启动，推全量", flush=True)
            break
    payload = json.dumps(MESSAGE, ensure_ascii=False)
    print("[ws] send full sync", len(payload), "bytes", flush=True)
    await ws.send(payload)
    await asyncio.sleep(12)
    reload_msg = json.loads(json.dumps(MESSAGE))
    reload_msg["contents"]["fileInfo"] = []
    reload_msg["contents"]["refreashType"] = "reload"
    print("[ws] 发送 reload 触发重载", flush=True)
    await ws.send(json.dumps(reload_msg, ensure_ascii=False))
    try:
        async for msg in ws:
            print("[ws] recv:", repr(msg)[:400], flush=True)
    except Exception as e:
        print("[ws] closed:", e, flush=True)


async def main():
    async with __import__("websockets").serve(handler, "0.0.0.0", WS_PORT, ping_interval=None):
        print("[ws] listening", WS_PORT, flush=True)
        await asyncio.Future()


threading.Thread(target=lambda: asyncio.run(main()), daemon=True).start()
time.sleep(1)

UDID = subprocess.check_output(["adb", "devices"]).decode().splitlines()[1].split()[0]
print("device:", UDID)
subprocess.call(["adb", "-s", UDID, "reverse", f"tcp:{HTTP_PORT}", f"tcp:{HTTP_PORT}"])
subprocess.call(["adb", "-s", UDID, "reverse", f"tcp:{WS_PORT}", f"tcp:{WS_PORT}"])
print("reverses:\n" + subprocess.check_output(["adb", "reverse", "--list"]).decode())

cmd = ["adb", "-s", UDID, "shell", "am", "start",
       "-n", "io.dcloud.HBuilder/io.dcloud.debug.PullDebugActivity",
       "--es", "ip", "127.0.0.1",
       "--es", "port", str(WS_PORT),
       "--es", "appid", APPID]
print(">", " ".join(cmd), flush=True)
subprocess.call(cmd)
print("started; Ctrl+C 退出", flush=True)
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass
