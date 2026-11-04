# 下载 RAG 记忆库用的嵌入模型（约 92 MB）。
#
# 模型文件不进 git —— 新机器上跑一次这个脚本即可：
#     pwsh scripts/fetch_model.ps1
#
# 下载源是 hf-mirror.com（huggingface.co 在部分网络下连不通）。
# 具体环境变量与踩过的坑见 scripts/fetch_model.py 的文件头注释。

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root "ai-agent-cyber-town.venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "找不到虚拟环境里的 python：$python" -ForegroundColor Red
    Write-Host "请先运行 scripts/setup.ps1 创建虚拟环境。" -ForegroundColor Yellow
    exit 1
}

& $python (Join-Path $PSScriptRoot "fetch_model.py")
exit $LASTEXITCODE