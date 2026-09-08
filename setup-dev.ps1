# ============================================================
#  TickFlow Stock Panel - one-shot dev env bootstrap (Windows)
#  Run from project root in your desktop PowerShell session:
#     powershell -ExecutionPolicy Bypass -File .\setup-dev.ps1
#  Flags: -SkipInstall  -NoStart  -BackendPort N  -FrontendPort N
# ============================================================

[CmdletBinding()]
param(
    [switch]$SkipInstall,
    [switch]$NoStart,
    [int]$BackendPort  = 3018,
    [int]$FrontendPort = 3011
)

$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch {}

function Log($m)  { Write-Host ("[setup] {0}" -f $m) -ForegroundColor Cyan }
function Ok($m)   { Write-Host ("[ok]    {0}" -f $m) -ForegroundColor Green }
function Warn($m) { Write-Host ("[warn]  {0}" -f $m) -ForegroundColor Yellow }
function Err($m)  { Write-Host ("[err]   {0}" -f $m) -ForegroundColor Red }

$Root = (Get-Location).Path
Log ("Working dir: {0}" -f $Root)
if (-not (Test-Path (Join-Path $Root "docker-compose.yml"))) {
    throw "Run this from project root (where docker-compose.yml lives)"
}

# ---- 1. base tools (git, node) ----
Log "Checking git / node"
$missing = @()
foreach ($t in @(
    @{n="git";  id="Git.Git"},
    @{n="node"; id="OpenJS.NodeJS.LTS"}
)) {
    if (-not (Get-Command $t.n -ErrorAction SilentlyContinue)) {
        $missing += $t
        Warn ("Missing: {0}" -f $t.n)
    } else { Ok ("Have {0} -> {1}" -f $t.n, (Get-Command $t.n).Source) }
}
if ($missing.Count -gt 0 -and -not $SkipInstall) {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        foreach ($t in $missing) {
            & winget install --id=$t.id --accept-package-agreements --accept-source-agreements | Out-Null
        }
    } else {
        Warn "winget not found. Install missing tools manually then re-run (or pass -SkipInstall)."
    }
}

# ---- 2. uv (user-level, no admin) ----
Log "Ensuring uv"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    if (-not $SkipInstall) {
        Log "Installing uv via official script"
        powershell -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
        $uvBin = Join-Path $env:USERPROFILE ".local\bin"
        $env:Path = $uvBin + ";" + $env:Path
    } else { throw "uv not installed. Re-run without -SkipInstall." }
}
Ok ("uv: {0}" -f (& uv --version))

# ---- 3. pnpm (user prefix, avoid corepack/global perms) ----
Log "Ensuring pnpm"
if (-not (Get-Command pnpm -ErrorAction SilentlyContinue)) {
    if (-not $SkipInstall) {
        $prefix = Join-Path $env:USERPROFILE ".local\pnpm"
        $cache  = Join-Path $env:USERPROFILE ".local\npm-cache"
        New-Item -ItemType Directory -Path $prefix -Force | Out-Null
        New-Item -ItemType Directory -Path $cache  -Force | Out-Null
        $env:NPM_CONFIG_PREFIX = $prefix
        $env:NPM_CONFIG_CACHE  = $cache
        $env:Path = $prefix + ";" + $env:Path
        npm install -g pnpm@9 --no-fund --no-audit
    } else { throw "pnpm not installed. Re-run without -SkipInstall." }
}
Ok ("pnpm: {0}" -f (& pnpm --version))

# ---- 4. free port 3018 if old tsp container is running ----
Log "Probing docker daemon"
$dockerReady = $false
try {
    $null = & docker info 2>&1
    if ($LASTEXITCODE -eq 0) { $dockerReady = $true }
} catch {}
if (-not $dockerReady) {
    Warn "Docker daemon not reachable. Start Docker Desktop, or ignore (dev mode does not need it)."
} else {
    try {
        $running = & docker ps --filter "name=tsp" --format "{{.Names}}" 2>$null
        if ($running) {
            Log "Stopping existing tsp container to free port"
            & docker stop tsp  | Out-Null
            & docker rm -f tsp | Out-Null
        } else {
            Ok "No stale tsp container"
        }
    } catch {
        Warn "docker ps failed; skipping tsp cleanup"
    }
}

# ---- 5. backend deps ----
Log "uv sync (backend)"
Push-Location (Join-Path $Root "backend")
try {
    uv sync
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }
    Ok "backend deps installed"
} finally { Pop-Location }

# ---- 6. frontend deps ----
Log "pnpm install (frontend)"
Push-Location (Join-Path $Root "frontend")
try {
    pnpm install
    if ($LASTEXITCODE -ne 0) { throw "pnpm install failed" }
    Ok "frontend deps installed"
} finally { Pop-Location }

# ---- 7. start dev (optional) ----
if ($NoStart) {
    Log "All set. Start dev later with: .\\dev.ps1"
    return
}
Log ("Starting dev (backend :{0}, frontend :{1})" -f $BackendPort, $FrontendPort)
$env:BACKEND_PORT  = [string]$BackendPort
$env:FRONTEND_PORT = [string]$FrontendPort
& (Join-Path $Root "dev.ps1") -BackendPort $BackendPort -FrontendPort $FrontendPort