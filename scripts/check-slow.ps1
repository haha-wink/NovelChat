#requires -Version 5.1
<#
.SYNOPSIS
    DSH 卡顿 / 变慢 一键体检（只读，不修改任何文件）

.DESCRIPTION
    检查六件事：
      1. 会话体积与上下文压力（哪个会话在变重）
      2. 机器争用（headless chrome / python / node / DSH 进程）
      3. 端口 8000 是否被占用、有几个监听者
      4. 动画产物目录的依赖树膨胀（node_modules / 文件数）
      5. 工作区垃圾与体积
      6. 插件版本与已知最新版、潜在双开冲突

.EXAMPLE
    pwsh -File .\check-slow.ps1
.EXAMPLE
    pwsh -File .\check-slow.ps1 -NoDeepScan      # 跳过 2.5 万文件的深扫，1 秒出结果
#>
[CmdletBinding()]
param(
    [string]$SessionRoot = "$env:USERPROFILE\.dsh\sessions",
    [string]$ProjCache   = "$env:USERPROFILE\.dsh\storages\session_projcache\sessions",
    [string]$Workspace   = 'E:\NovelChat\backend\data',   # _scratch 已于 2026-11-21 删除，改扫运行时数据目录
    [string]$Project     = 'E:\NovelChat',
    [string]$AnimDir     = 'E:\NovelChat\backend\data\animation',
    [switch]$NoDeepScan
)

$ErrorActionPreference = 'SilentlyContinue'
$script:warn = 0; $script:bad = 0

function Line([string]$tag, [string]$text, [string]$color = 'Gray') {
    if ($tag -eq '!!') { $script:bad++;  Write-Host ('  [!!] ' + $text) -ForegroundColor Red }
    elseif ($tag -eq '!') { $script:warn++; Write-Host ('  [ !] ' + $text) -ForegroundColor Yellow }
    elseif ($tag -eq 'ok') { Write-Host ('  [ ok] ' + $text) -ForegroundColor DarkGreen }
    else { Write-Host ('       ' + $text) -ForegroundColor $color }
}
function Head([string]$t) { Write-Host ''; Write-Host $t -ForegroundColor Cyan; Write-Host ('-' * 64) -ForegroundColor DarkGray }
function Round1($v) { if ($null -eq $v) { 'n/a' } else { [math]::Round([double]$v, 1) } }

function Get-DirStat([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    if ($NoDeepScan) { return [pscustomobject]@{ Files = -1; MB = -1 } }
    $f = Get-ChildItem -LiteralPath $path -Recurse -Force -File -ErrorAction SilentlyContinue
    [pscustomobject]@{
        Files = ($f | Measure-Object).Count
        MB    = [math]::Round((($f | Measure-Object -Property Length -Sum).Sum) / 1MB, 1)
    }
}

Write-Host ''
Write-Host '  DSH 变慢体检  ' -NoNewline -ForegroundColor White
Write-Host ((Get-Date).ToString('yyyy-MM-dd HH:mm:ss')) -ForegroundColor DarkGray

# ---------------------------------------------------------------- 1. 会话体积
Head '1. 会话体积 / 上下文压力（“越聊越慢”的主因）'
$sessions = Get-ChildItem -LiteralPath $SessionRoot -Recurse -Force -File -Filter 'session*.jsonl*' -ErrorAction SilentlyContinue |
            Group-Object { $_.Directory.Name } |
            ForEach-Object { $_.Group | Sort-Object Length -Descending | Select-Object -First 1 } |
            Sort-Object Length -Descending | Select-Object -First 5
$heavy = 0
if (-not $sessions) { Line '--' "找不到会话文件：$SessionRoot" }
else {
    foreach ($s in $sessions) {
        $id = $s.Directory.Name
        $cache = Join-Path $ProjCache ($id + '.json')
        $title = '?'; $surface = 0; $pressure = 0; $window = 0; $turns = 0; $steps = 0
        $llmH = 0; $toolH = 0; $pending = 0
        if (Test-Path -LiteralPath $cache) {
            try {
                $r = (Get-Content -LiteralPath $cache -Raw -Encoding UTF8 | ConvertFrom-Json).record.rows
                if ($r.title -and $r.title.val) { $title = [string]$r.title.val }
                if ($r.contextPressure -and $r.contextPressure.val) {
                    $surface  = $r.contextPressure.val.surfaceTokens
                    $pressure = $r.contextPressure.val.pressureTokens
                    $window   = $r.contextPressure.val.contextWindow
                }
                if ($r.sessionStats -and $r.sessionStats.val) {
                    $st = $r.sessionStats.val
                    $turns = $st.turns; $steps = $st.steps
                    $llmH  = [math]::Round($st.llmMs  / 3600000, 1)
                    $toolH = [math]::Round($st.toolMs / 3600000, 1)
                    if ($st.pendingCalls) { $pending = ($st.pendingCalls.PSObject.Properties | Measure-Object).Count }
                }
            } catch { }
        }
        $sizeMB = [math]::Round($s.Length / 1MB, 1)
        $pct = if ($window -gt 0) { '{0:P0}' -f ($surface / $window) } else { 'n/a' }
        $tag = if ($sizeMB -gt 5 -or $surface -gt 150000) { '!!' } elseif ($sizeMB -gt 2 -or $surface -gt 80000) { '!' } else { 'ok' }
        Line $tag ("{0,-28} {1,6:N1} MB  上下文 {2,8:N0} tok / 压力 {3,8:N0} ({4})" -f `
            $title.Substring(0, [Math]::Min(28, $title.Length)), $sizeMB, $surface, $pressure, $pct)
        Line '--' ("      {0} 轮 / {1} 步 · LLM 累计 {2} h · 工具等待 {3} h" -f $turns, $steps, $llmH, $toolH)
        if ($pending -gt 0) { Line '!' ("      此刻有 {0} 个工具调用在等返回（正卡着的就是它）" -f $pending) }
        if ($surface -gt 150000) { $heavy++ }
        Line '--' ("      " + $s.FullName)
    }
    if ($heavy -gt 0) {
        Line '!!' ("{0} 个会话上下文已超 15 万 token，却都在继续用 —— 这就是「越聊越慢」的直接原因" -f $heavy)
    }
    Line '--' '判读：单会话 >5MB 或上下文 >15 万 token 就该新开会话；>25 万 token 每轮光 TTFT 就几十秒。'

    # 1b. 压缩历史：读会话日志（多 zstd 帧追加格式），统计 ACP 到底压了几次
    $node = (Get-Command node -ErrorAction SilentlyContinue).Source
    if (-not $node) {
        foreach ($cand in @('D:\edge 下载\node\node.exe', "$env:ProgramFiles\nodejs\node.exe")) {
            if (Test-Path -LiteralPath $cand) { $node = $cand; break }
        }
    }
    $scanner = Join-Path $PSScriptRoot 'check-compaction.mjs'
    if ($node -and (Test-Path -LiteralPath $scanner)) {
        Head '1b. 压缩历史（ACP 到底压没压）'
        $files = @($sessions | ForEach-Object { $_.FullName })
        try {
            & $node $scanner @files 2>&1 | ForEach-Object { Line '--' ("    " + $_) }
        } catch { Line '--' ('    扫描失败：' + $_.Exception.Message) }
        Line '--' '判读：0 次压缩 = 从没压过；压了但上下文仍 >15 万 = 压得没涨得快（工具输出太重）。'
    } else {
        Line '--' '（跳过压缩历史：未找到 node 或 scripts/check-compaction.mjs）'
    }
}

# ---------------------------------------------------------------- 2. 机器争用
Head '2. 机器争用（谁在抢 CPU / 内存）'
$procs = Get-Process -ErrorAction SilentlyContinue
function ProcRow([string]$name, [string]$label) {
    $ps = $procs | Where-Object { $_.ProcessName -eq $name }
    if (-not $ps) { Line 'ok' ("{0,-22} 0 个" -f $label); return }
    $n    = ($ps | Measure-Object).Count
    $mem  = [math]::Round((($ps | Measure-Object -Property WorkingSet64 -Sum).Sum) / 1MB, 0)
    $cpu  = [math]::Round((($ps | Measure-Object -Property CPU -Sum).Sum), 0)
    $tag  = if ($n -ge 4 -and $name -eq 'chrome-headless-shell') { '!!' } elseif ($cpu -ge 300 -and $name -notlike '*dsh*') { '!' } else { 'ok' }
    Line $tag ("{0,-22} {1,3} 个   内存 {2,6} MB   累计 CPU {3,7} s" -f $label, $n, $mem, $cpu)
}
ProcRow 'chrome-headless-shell' 'headless Chrome'
ProcRow 'chrome'                'Chrome 本体'
ProcRow 'python'                'python'
ProcRow 'node'                  'node'
ProcRow 'msedge'                'Edge'
$dsh = $procs | Where-Object { $_.ProcessName -match 'DSH|dsh' }
if ($dsh) {
    $n = ($dsh | Measure-Object).Count
    $mem = [math]::Round((($dsh | Measure-Object -Property WorkingSet64 -Sum).Sum) / 1MB, 0)
    $hot = $dsh | Sort-Object CPU -Descending | Select-Object -First 2
    $hotStr = ($hot | ForEach-Object { '{0}s' -f [math]::Round($_.CPU, 0) }) -join ' / '
    $tag = if (($hot | Select-Object -First 1).CPU -gt 900) { '!!' } elseif (($hot | Select-Object -First 1).CPU -gt 300) { '!' } else { 'ok' }
    Line $tag ("{0,-22} {1,3} 个   内存 {2,6} MB   最热两个累计 CPU {3}" -f 'DSH 桌面端', $n, $mem, $hotStr)
}
$os = Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue
if ($os) {
    $freeMB = [math]::Round($os.FreePhysicalMemory / 1KB, 0)
    $totMB  = [math]::Round($os.TotalVisibleMemorySize / 1KB, 0)
    $tag = if ($freeMB -lt 1500) { '!!' } elseif ($freeMB -lt 3000) { '!' } else { 'ok' }
    Line $tag ("{0,-22} 空闲 {1:N0} MB / 共 {2:N0} MB（{3:P0} 已用）" -f '物理内存', $freeMB, $totMB, (1 - $freeMB / $totMB))
}
foreach ($d in [System.IO.DriveInfo]::GetDrives()) {
    if (-not $d.IsReady) { continue }
    $freeGB = [math]::Round($d.AvailableFreeSpace / 1GB, 1)
    $tag = if ($freeGB -lt 10) { '!!' } elseif ($freeGB -lt 30) { '!' } else { 'ok' }
    Line $tag ("{0,-22} 空闲 {1,7:N1} GB / {2,7:N1} GB" -f ("磁盘 " + $d.Name), $freeGB, ($d.TotalSize / 1GB))
}

# ---------------------------------------------------------------- 3. 端口
Head '3. 端口 8000（有多个监听者 = 重启没生效）'
$conn = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if (-not $conn) { Line 'ok' '8000 端口空闲 —— 后端没起' }
else {
    $n = ($conn | Measure-Object).Count
    if ($n -gt 1) { Line '!!' ("8000 有 {0} 个 LISTEN 进程（旧的没杀干净）" -f $n) } else { Line 'ok' ("8000 有 {0} 个 LISTEN 进程" -f $n) }
    foreach ($c in $conn) {
        $p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
        Line '--' ("    PID {0,-7} {1,-22} 启动 {2:HH:mm:ss}  内存 {3} MB" -f $c.OwningProcess, $p.ProcessName, $p.StartTime, [math]::Round($p.WorkingSet64 / 1MB, 0))
    }
}

# ---------------------------------------------------------------- 4. 动画依赖树
Head '4. 动画产物依赖树（“安装依赖”越来越慢的元凶）'
if (-not (Test-Path -LiteralPath $AnimDir)) { Line '--' "目录不存在：$AnimDir" }
else {
    $jobs = Get-ChildItem -LiteralPath $AnimDir -Directory -Force
    $nmCount = (Get-ChildItem -LiteralPath $AnimDir -Directory -Force -Recurse -Filter 'node_modules' -ErrorAction SilentlyContinue | Measure-Object).Count
    $st = Get-DirStat $AnimDir
    $tag = if ($nmCount -gt 3 -or $st.MB -gt 500) { '!!' } elseif ($nmCount -gt 1 -or $st.MB -gt 200) { '!' } else { 'ok' }
    Line $tag ("job 目录 {0} 个，但 node_modules {1} 套" -f $jobs.Count, $nmCount)
    if ($st.MB -ge 0) { Line '--' ("    合计 {0:N0} 文件 / {1:N1} MB" -f $st.Files, $st.MB) }
    $newest = $jobs | Sort-Object LastWriteTime -Descending | Select-Object -First 3
    foreach ($j in $newest) { Line '--' ("    最近：{0,-22} {1:MM-dd HH:mm}" -f $j.Name, $j.LastWriteTime) }
    if ($nmCount -gt $jobs.Count) {
        Line '!' '每个 job 各装一套 puppeteer：一次 MV 要下载/解包几百 MB，再把 2 万+ 文件丢给 Defender 扫。'
        Line '--' '   建议：统一用项目根的 node_modules，并设 PUPPETEER_SKIP_DOWNLOAD=true + PUPPETEER_EXECUTABLE_PATH。'
    }
}

# ---------------------------------------------------------------- 5. 工作区垃圾
Head '5. 工作区垃圾'
$db = Get-ChildItem -LiteralPath $Workspace -File -Filter 'tmp*.db' -ErrorAction SilentlyContinue
$wsStat = Get-DirStat $Workspace
if ($db) {
    $tag = if (($db | Measure-Object).Count -gt 20) { '!' } else { 'ok' }
    Line $tag ("{0} 个残留 tmp*.db，共 {1:N1} MB" -f ($db | Measure-Object).Count, ((($db | Measure-Object -Property Length -Sum).Sum) / 1MB))
} else { Line 'ok' '没有残留 tmp*.db' }
if ($wsStat -and $wsStat.MB -ge 0) {
    $tag = if ($wsStat.MB -gt 300) { '!' } else { 'ok' }
    Line $tag ("工作区合计 {0:N0} 文件 / {1:N1} MB" -f $wsStat.Files, $wsStat.MB)
    Get-ChildItem -LiteralPath $Workspace -Directory -Force | ForEach-Object {
        $s = Get-DirStat $_.FullName
        if ($s -and $s.MB -ge 20) { Line '--' ("    {0,-24} {1:N0} 文件 / {2:N1} MB" -f $_.Name, $s.Files, $s.MB) }
    }
}

# ---------------------------------------------------------------- 6. 插件
Head '6. 插件版本 / 冲突'
$known = @{
    'dsh-context'          = '0.87.0'
    'billion-context-dsh'  = '0.2.27'
    'dsh-ops'              = '0.5.5(需 dsh>=0.2.2-rc.1)'
    '@liustack/modlens'    = '3.27.5'
    'dsh-whale-widget'     = '0.4.2'
    'dsh-boot-animation'   = '0.4.2'
}
$profileNm = Join-Path $env:USERPROFILE '.dsh\profiles\desktop\node_modules'
$installed = @{}
foreach ($p in $known.Keys) {
    $pkg = Join-Path $profileNm ($p + '\package.json')
    if (Test-Path -LiteralPath $pkg) {
        try { $installed[$p] = (Get-Content -LiteralPath $pkg -Raw -Encoding UTF8 | ConvertFrom-Json).version } catch { $installed[$p] = '?' }
    }
}
foreach ($p in ($known.Keys | Sort-Object)) {
    if ($installed.ContainsKey($p)) {
        $cur = $installed[$p]; $tgt = $known[$p]
        $tag = if ($tgt.StartsWith($cur)) { 'ok' } else { '!' }
        Line $tag ("{0,-22} 已装 {1,-9} 最新 {2}" -f $p, $cur, $tgt)
    } else {
        Line '--' ("{0,-22} 未安装" -f $p)
    }
}
$corePath = 'E:\deepseek蓝色大肥鱼\DSH Desktop\resources\app\node_modules\@deepseek-ai\dsh\package.json'
if (Test-Path -LiteralPath $corePath) {
    try { Line '--' ("{0,-22} {1}" -f 'DSH 内核', (Get-Content -LiteralPath $corePath -Raw -Encoding UTF8 | ConvertFrom-Json).version) } catch { }
}
if ($installed.ContainsKey('dsh-context') -and $installed.ContainsKey('billion-context-dsh')) {
    Line '!' 'dsh-context 与 billion-context-dsh 同时在装：两者都挂上下文层，压缩经常“谁都压不下去”。'
    Line '--' '   建议只留一个（推荐留 billion-context-dsh 负责压缩，或留 dsh-context 只看板）。'
}

# ---------------------------------------------------------------- 结论
Head '结论'
if ($script:bad -eq 0 -and $script:warn -le 1) {
    Write-Host '  没发现明显瓶颈，卡顿多半来自单次超长工具调用（渲染/安装）本身。' -ForegroundColor Green
} else {
    Write-Host ("  {0} 个红灯、{1} 个黄灯。优先处理顺序：" -f $script:bad, $script:warn) -ForegroundColor Yellow
    Write-Host '   1) 会话 >5MB 或上下文 >15 万：新开会话，老会话只读存档' -ForegroundColor Gray
    Write-Host '   2) headless Chrome 一次只跑一个 job，跑完就杀' -ForegroundColor Gray
    Write-Host '   3) 动画依赖复用 + 关掉重复的 context 插件' -ForegroundColor Gray
}
Write-Host ''