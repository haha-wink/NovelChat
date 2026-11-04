<#
.SYNOPSIS
    Wrapper around this project's venv pip. Use it instead of calling pip directly.

.DESCRIPTION
    Why this exists (the actual pip problem on this machine):

    When the project is driven from a sandboxed / low-integrity session, python.exe is
    started at LOW mandatory integrity level while %TEMP% is a normal MEDIUM object.
    Windows Mandatory Integrity Control forbids writing "up", so pip cannot create its
    temp files or unpack wheels. The visible symptoms are:

      * PermissionError: [Errno 13] Permission denied: '...\pip-unpack-xxxx\foo.whl'
      * pip appearing to hang at 100% CPU forever (tempfile keeps retrying creation)

    Fix: point TMP/TEMP at a project-local folder that carries a LOW integrity label and
    lets that label be inherited (O I)(C I). A LOW process can then create its working
    directories there and pip installs normally with no extra flags.

    On a normal (MEDIUM integrity) terminal this is still harmless: pip simply uses a
    project-local temp dir instead of %TEMP%.

.EXAMPLE
    .\scripts\pip.ps1 install -r requirements.txt

.EXAMPLE
    .\scripts\pip.ps1 install --upgrade pip
#>
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$PipArgs
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $root "ai-agent-cyber-town.venv\Scripts\python.exe"
$tempDir = Join-Path $root ".pytemp"

if (-not (Test-Path $venvPython)) {
    Write-Error "venv python not found: $venvPython (run .\scripts\setup.ps1 first)"
    exit 1
}

if (-not (Test-Path $tempDir)) {
    New-Item -ItemType Directory -Force -Path $tempDir | Out-Null
    # LOW integrity label, inherited by every file/dir pip creates inside it
    & icacls $tempDir /setintegritylevel "(OI)(CI)L" | Out-Null
}

$env:TMP = $tempDir
$env:TEMP = $tempDir

& $venvPython -m pip @PipArgs
exit $LASTEXITCODE