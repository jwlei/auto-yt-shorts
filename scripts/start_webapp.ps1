# Starts the AutoShorts web UI (if it isn't already running) and opens it in the browser.
# The server runs in its own minimised console window: close that window to stop it.
# Run by the "AutoShorts" desktop shortcut (see scripts/create_shortcut.ps1).

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$port = 8800

function Test-Server {
    try {
        $null = Invoke-WebRequest "http://127.0.0.1:$port/login" -UseBasicParsing -TimeoutSec 2
        return $true
    } catch {
        return $false
    }
}

if (-not (Test-Server)) {
    # 'py -3' resolves the real interpreter even when a shortcut's PATH lacks it.
    $python = (Get-Command py -ErrorAction SilentlyContinue).Source
    $pyArgs = @('-3')
    if (-not $python) {
        $python = (Get-Command python).Source
        $pyArgs = @()
    }
    # 0.0.0.0 so your phone on the same Wi-Fi can reach it (APP_PASSWORD in .env protects it).
    Start-Process -FilePath $python -ArgumentList ($pyArgs + @('webapp.py', '--host', '0.0.0.0', '--port', "$port")) `
        -WorkingDirectory $root -WindowStyle Minimized

    for ($i = 0; $i -lt 40 -and -not (Test-Server); $i++) {
        Start-Sleep -Milliseconds 500
    }
}

Start-Process "http://localhost:$port"
