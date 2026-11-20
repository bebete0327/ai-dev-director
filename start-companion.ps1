$ErrorActionPreference = 'Stop'
$directorRoot = $PSScriptRoot
$directorPython = (Get-Command python).Source
$wantBuildId = (& $directorPython -c "import launcher; print(launcher.get_build_id())" 2>$null).Trim()
try {
    $directorHealth = Invoke-RestMethod 'http://127.0.0.1:8081/ops/state' -TimeoutSec 3
    if ($directorHealth.solomon_url -and ($directorHealth.build_id -eq $wantBuildId)) {
        Write-Output 'AI DEV companion already running (up-to-date)'; exit 0
    }
    if ($directorHealth.csrf) {
        Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8081/ops/shutdown' -Headers @{'X-Solomon-Bridge'=$directorHealth.csrf; 'Origin'='http://127.0.0.1:8081'} -TimeoutSec 2 | Out-Null
        Start-Sleep -Milliseconds 600
    }
} catch { }
Start-Process -FilePath $directorPython -ArgumentList 'companion_server.py' -WorkingDirectory $directorRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $directorRoot 'data/companion.log') -RedirectStandardError (Join-Path $directorRoot 'data/companion.err') | Out-Null
Write-Output 'AI DEV companion: http://127.0.0.1:8081'
