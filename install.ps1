# unihx 安装脚本（Windows PowerShell）
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Prefix = if ($env:UNIHX_PREFIX) { $env:UNIHX_PREFIX } else { Join-Path $env:LOCALAPPDATA "unihx" }
$Bin = Join-Path $Prefix "bin"
$Share = Join-Path $Prefix "share"

New-Item -ItemType Directory -Force -Path $Bin, $Share | Out-Null
Copy-Item (Join-Path $Root "bin\unihx") (Join-Path $Share "unihx.py") -Force
Copy-Item (Join-Path $Root "gui\app.py") (Join-Path $Share "app.py") -Force

$wrapper = @"
@echo off
python "%~dp0..\share\unihx.py" %*
"@
Set-Content -Path (Join-Path $Bin "unihx.cmd") -Value $wrapper -Encoding ASCII

$guiWrapper = @"
@echo off
python "%~dp0..\share\app.py" %*
"@
Set-Content -Path (Join-Path $Bin "unihx-gui.cmd") -Value $guiWrapper -Encoding ASCII

Write-Host "已安装到 $Bin"
Write-Host "请把以下目录加入 PATH："
Write-Host "  $Bin"

# 依赖检查
python -c "import json5" 2>$null
if ($LASTEXITCODE -ne 0) { Write-Host "提示: pip install json5（推荐）" }
python -c "import PySide6" 2>$null
if ($LASTEXITCODE -ne 0) { Write-Host "提示: pip install PySide6（GUI 需要）" }

Write-Host ""
Write-Host "下一步："
Write-Host "  unihx doctor"
Write-Host "  unihx update --check"
