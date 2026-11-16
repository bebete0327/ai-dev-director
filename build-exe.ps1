$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

Write-Host "=== Building AI-Dev-Director.exe (Standalone Windowless App) ===" -ForegroundColor Cyan

python -m pip install --upgrade pip pyinstaller
python -m pip install -r requirements.txt

if (Test-Path build) { Remove-Item -Recurse -Force build }
if (Test-Path dist)  { Remove-Item -Recurse -Force dist }

python -m PyInstaller --clean --noconfirm AI-Dev-Director.spec

$pkgDir = Join-Path $PSScriptRoot "dist\AI-Dev-Director-win64"
New-Item -ItemType Directory -Force -Path $pkgDir | Out-Null
Copy-Item "dist\AI-Dev-Director.exe" -Destination $pkgDir
Copy-Item "config.yaml" -Destination $pkgDir
Copy-Item "companion.json" -Destination $pkgDir

Write-Host "Running packaged executable smoke test..." -ForegroundColor Yellow
& (Join-Path $pkgDir "AI-Dev-Director.exe") --smoke-test
if ($LASTEXITCODE -ne 0) {
    throw "Smoke test failed with exit code $LASTEXITCODE"
}

$zipPath = Join-Path $PSScriptRoot "dist\AI-Dev-Director-win64.zip"
Compress-Archive -Path "$pkgDir\*" -DestinationPath $zipPath -Force

Write-Host "`nBuild complete!" -ForegroundColor Green
Write-Host "  Executable : $pkgDir\AI-Dev-Director.exe"
Write-Host "  Portable ZIP: $zipPath"
