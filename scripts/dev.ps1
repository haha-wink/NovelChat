# 注意：本文件必须保存为 UTF-8 with BOM。
# Windows PowerShell 5.1 对无 BOM 的 .ps1 会按 ANSI(GBK) 解析，中文会乱码并触发解析错误。
<#
.SYNOPSIS
  一键启动开发环境（后端 FastAPI + 前端 Vite）。

.DESCRIPTION
  后端路径已绝对化，从任何目录启动都能跑，不再要求先 cd backend。
  前端 dev server 通过 vite 代理把 /api 转发到 127.0.0.1:8000。

  自动重载只监听 backend\api、backend\core、backend\services 三个代码目录，
  刻意不监听 backend\data —— 否则聊天时写入记忆库/向量库会让服务反复重启。

.EXAMPLE
  .\scripts\dev.ps1
  .\scripts\dev.ps1 -BackendOnly
  .\scripts\dev.ps1 -NoReload        # 关掉自动重载（改后端代码需手动重启）
  .\scripts\dev.ps1 -FrontendOnly
#>
param(
  [switch]$BackendOnly,
  [switch]$FrontendOnly,
  [switch]$NoReload
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$venvPython = Join-Path $root 'ai-agent-cyber-town.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
  Write-Warning "未找到虚拟环境 $venvPython，回退到系统 python"
  $venvPython = 'python'
}

$envFile = Join-Path $root 'backend\.env'
if (-not (Test-Path $envFile)) {
  Write-Warning '未找到 backend\.env —— 复制 backend\.env.example 并填写 DEEPSEEK_API_KEY，否则 /api/chat 会返回 503'
}

if (-not $FrontendOnly) {
  $reloadArgs = ''
  if (-not $NoReload) {
    $reloadArgs = ' --reload --reload-dir backend\api --reload-dir backend\services --reload-dir backend\core'
  }
  Write-Host '==> 启动后端：http://127.0.0.1:8000  (接口文档 http://127.0.0.1:8000/docs)'
  # 用 cmd.exe /k 开窗口：窗口不会一闪就关，出错也看得到。
  # 整条命令再包一层双引号，是 cmd 对「首个参数被引号包裹」的官方写法。
  $backendArgs = '/k ""{0}" -m uvicorn main:app --app-dir backend --host 127.0.0.1 --port 8000{1}"' -f $venvPython, $reloadArgs
  Start-Process -FilePath 'cmd.exe' -ArgumentList $backendArgs -WorkingDirectory $root
}

if (-not $BackendOnly) {
  Write-Host '==> 启动前端：http://127.0.0.1:5173'
  Start-Process -FilePath 'cmd.exe' -ArgumentList '/k', 'npm run dev' -WorkingDirectory (Join-Path $root 'frontend')
}

Write-Host ''
Write-Host '已启动。关闭对应的命令行窗口即可停止服务。'
Write-Host '后端启动只要 1~2 秒（不再加载向量模型）；上传小说后会调用 DeepSeek 抽取角色。'
