<#
    Launch LogSeeker for colleagues to use.

        .\run.ps1              # reachable from the network
        .\run.ps1 -LocalOnly   # only from this machine, for testing

    Settings come from .env (see .env.example). Anything already set as a real
    environment variable wins over .env.
#>

param(
    [int]$Port = 8501,
    [switch]$LocalOnly
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".env")) {
    Write-Warning "No .env file found. Copy .env.example to .env and fill in your log-host login."
    Write-Host   "    Copy-Item .env.example .env"
    exit 1
}

# The operator's log-host account. Shown so nobody is surprised about whose
# credentials every visitor's search is running under.
$envUser = (Get-Content .env | Where-Object { $_ -match '^\s*LOGSEEKER_SSH_USER\s*=' }) -replace '^[^=]*=\s*', '' -replace '["'']', ''
$allowed = (Get-Content .env | Where-Object { $_ -match '^\s*LOGSEEKER_ALLOWED_NETWORKS\s*=' }) -replace '^[^=]*=\s*', '' -replace '["'']', ''

Write-Host ""
Write-Host "LogSeeker" -ForegroundColor Cyan
Write-Host "  searches run on the log host as : $envUser"
if ([string]::IsNullOrWhiteSpace($allowed)) {
    Write-Host "  network restriction             : NONE - anyone who can reach the port can pull logs" -ForegroundColor Yellow
} else {
    Write-Host "  network restriction             : $allowed"
}

if ($LocalOnly) {
    $address = "127.0.0.1"
    Write-Host "  listening on                    : http://localhost:$Port (this machine only)"
} else {
    $address = "0.0.0.0"
    $ip = (Get-NetIPAddress -AddressFamily IPv4 |
           Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
           Select-Object -First 1).IPAddress
    Write-Host "  share this link with colleagues : http://${ip}:$Port"
}
Write-Host ""

streamlit run streamlit_app.py --server.address $address --server.port $Port --server.headless true
