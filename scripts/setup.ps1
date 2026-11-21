# Rebuild the Python environment for this project.
#
# Usage (from anywhere):
#   .\scripts\setup.ps1                # create venv if missing, install deps, run self-check
#   .\scripts\setup.ps1 -Check         # only verify files/venv, print the plan, touch nothing
#   .\scripts\setup.ps1 -Force         # delete the venv first, then reinstall
#   .\scripts\setup.ps1 -Mirror https://pypi.tuna.tsinghua.edu.cn/simple
#   .\scripts\setup.ps1 -VenvName my.venv
#
# Notes:
#   * Only the venv folder is deleted by -Force. backend\.env and backend\data\ are never touched.
#   * This script is intentionally ASCII-only so Windows PowerShell 5.1 can parse it.
#   * If PowerShell blocks it ("not digitally signed"), run:
#       powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1
#     or permanently allow local scripts:
#       Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

param(
    [switch]$Check,
    [switch]$Force,
    [switch]$SkipTests,
    [string]$Mirror = "",
    [string]$VenvName = ".venv"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$venvPath = Join-Path $root $VenvName
$venvPython = Join-Path $venvPath "Scripts\python.exe"
$requirements = Join-Path $root "backend\requirements.txt"
$envFile = Join-Path $root "backend\.env"
$envExample = Join-Path $root "backend\.env.example"
$smokeTest = Join-Path $root "backend\tests\smoke_test.py"

function Step($msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }
function Ok($msg) { Write-Host "    OK  $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "    !!  $msg" -ForegroundColor Yellow }
function Die($msg) { Write-Host "    XX  $msg" -ForegroundColor Red; exit 1 }

Step "Project layout"
Write-Host "    root : $root"
Write-Host "    venv : $venvPath"
if (-not (Test-Path $requirements)) { Die "backend\requirements.txt not found" }
Ok "backend/requirements.txt found"
if (-not (Test-Path (Join-Path $root "backend\main.py"))) { Die "backend\main.py not found (wrong folder?)" }
Ok "backend\main.py found"

Step "Existing environment"
$venvExists = Test-Path $venvPython
if ($venvExists) {
    $version = & $venvPython -c "import sys; print(sys.version.split()[0])" 2>$null
    Ok "venv already present (python $version)"
} else {
    Warn "no venv at $venvPath"
}

if ($Check) {
    Step "Plan (check mode, nothing changed)"
    $plan = @()
    if ($Force) { $plan += "delete $venvPath" }
    if (-not $venvExists -or $Force) { $plan += "python -m venv $venvPath" }
    $plan += "write pip config into the venv (timeout / retries / no-cache-dir)"
    $plan += "pip install --upgrade pip setuptools wheel (--no-cache-dir)"
    if ($Mirror) { $plan += "pip install -r backend\requirements.txt -i $Mirror --no-cache-dir --no-build-isolation" }
    else { $plan += "pip install -r backend\requirements.txt --no-cache-dir --no-build-isolation" }
    if (-not (Test-Path $envFile)) { $plan += "copy backend\.env.example to backend\.env" }
    if (-not $SkipTests) { $plan += "python backend\tests\smoke_test.py" }
    $i = 1
    foreach ($item in $plan) { Write-Host ("    {0}. {1}" -f $i, $item); $i++ }
    Write-Host ""
    Ok "check mode finished (re-run without -Check to apply)"
    exit 0
}

if ($Force -and $venvExists) {
    Step "Removing old venv"
    Remove-Item -Recurse -Force $venvPath
    Ok "deleted $venvPath"
    $venvExists = $false
}

if (-not (Test-Path $venvPython)) {
    Step "Creating venv"
    $py = Get-Command python -ErrorAction SilentlyContinue
    if ($py) {
        $sysPython = $py.Source
    } else {
        $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
        if (-not $pyLauncher) { Die "no python found on PATH; install Python 3.11+ first" }
        $sysPython = "py"
    }
    Write-Host "    using: $sysPython"
    if ($sysPython -eq "py") { & py -3 -m venv $venvPath } else { & $sysPython -m venv $venvPath }
    if (-not (Test-Path $venvPython)) { Die "venv creation failed" }
    Ok "venv created"
}

Step "Installing dependencies"

# Per-venv pip config: keeps mirror / timeouts / --no-cache-dir in one place so a plain
# `pip install` in this venv behaves the same without a long flag list. The file lives
# inside the venv (gitignored), so a rebuilt venv gets a fresh copy from this script.
$pipIni = Join-Path $venvPath "pip.ini"
$iniLines = @(
    "[global]"
    "timeout = 15"
    "retries = 2"
    # keep pip out of %LOCALAPPDATA%\pip\Cache, which a LOW-integrity python cannot write
    "no-cache-dir = true"
    "disable-pip-version-check = true"
)
if ($Mirror) { $iniLines += "index-url = $Mirror" }
$iniLines | Set-Content -Path $pipIni -Encoding ascii
Ok "pip config written: $pipIni"

# Interpreter-startup fallback: if %TEMP% is not writable (LOW-integrity sandboxed
# session) point tempfile at .pytemp. Without it pip cannot unpack wheels and jieba
# cannot cache, so the app silently degrades to the builtin name engine.
$sitePackages = Join-Path $venvPath "Lib\site-packages"
$siteCustomizeSrc = Join-Path $PSScriptRoot "sitecustomize.py"
if (Test-Path $siteCustomizeSrc) {
    Copy-Item $siteCustomizeSrc (Join-Path $sitePackages "sitecustomize.py") -Force
    Ok "temp fallback installed into venv"
} else {
    Warn "scripts\sitecustomize.py missing; LOW-integrity sessions may fail on temp files"
}

$pipFlags = @("--disable-pip-version-check", "--no-cache-dir")

# Temp dir for pip. Python may run at LOW mandatory integrity while %TEMP% is MEDIUM, and
# Windows forbids writing up: pip then either dies with PermissionError on pip-unpack-*.whl
# or spins at 100% CPU retrying temp creation. A project-local LOW-labelled dir fixes both.
$pipTemp = Join-Path $root ".pytemp"
if (-not (Test-Path $pipTemp)) { New-Item -ItemType Directory -Force -Path $pipTemp | Out-Null }
try { & icacls $pipTemp /setintegritylevel "(OI)(CI)L" | Out-Null } catch { }
$env:TMP = $pipTemp
$env:TEMP = $pipTemp
Ok "pip temp dir: $pipTemp"

& $venvPython -m pip install --upgrade pip @pipFlags
if ($LASTEXITCODE -ne 0) { Warn "pip upgrade failed, continuing" }

# jieba ships as an sdist: pip's isolated build env has to bootstrap setuptools first and
# can stall there, so pre-install setuptools/wheel and build without isolation.
& $venvPython -m pip install setuptools wheel @pipFlags
if ($LASTEXITCODE -ne 0) { Warn "could not pre-install setuptools/wheel (jieba may fail to build)" }

if ($Mirror) {
    & $venvPython -m pip install -r $requirements -i $Mirror @pipFlags --no-build-isolation
} else {
    & $venvPython -m pip install -r $requirements @pipFlags --no-build-isolation
}
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Warn "pip install failed. Common fixes:"
    Write-Host "    - behind a firewall/proxy: add -Mirror https://pypi.tuna.tsinghua.edu.cn/simple"
    Write-Host "    - pip hangs at 100% CPU: its cache/temp dir is not writable; --no-cache-dir is already set above"
    Write-Host "    - pip dies with PermissionError on pip-unpack-*.whl: run outside the restricted sandbox / with full access"
    Write-Host "    - offline machine: pip download on another PC, then pip install the .whl files"
    Write-Host "    - if only jieba fails, the app still works (falls back to builtin rules)"
    exit 1
}
Ok "dependencies installed"

Step "Config file"
if (Test-Path $envFile) {
    Ok "backend\.env exists (kept as is)"
} elseif (Test-Path $envExample) {
    Copy-Item $envExample $envFile
    Warn "backend\.env created from .env.example -- fill in DEEPSEEK_API_KEY and SESSION_SECRET"
} else {
    Warn "no .env / .env.example found"
}

if (-not $SkipTests) {
    Step "Self check"
    & $venvPython $smokeTest
    if ($LASTEXITCODE -ne 0) { Warn "self check reported failures (see output above)" }
    else { Ok "self check passed" }
}

Step "Next"
Write-Host "    start dev server : .\scripts\dev.ps1"
Write-Host "    check name engine: .\$VenvName\Scripts\python.exe scripts\check_nlp.py"
Write-Host "    health endpoint  : http://127.0.0.1:8000/api/health"
Write-Host ""
Ok "done"