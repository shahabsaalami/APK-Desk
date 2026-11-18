$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    python -m PyInstaller --noconfirm --clean --onefile --windowed --collect-all tkinterdnd2 `
        --add-data 'assets/fonts/Dastkhat-Regular.ttf;assets/fonts' `
        --add-data 'assets/fonts/OFL.txt;assets/fonts' --name APKDesk apk_desk.py
    if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'dist\APKDesk.exe') -Destination (Join-Path $PSScriptRoot 'APKDesk.exe') -Force
} finally { Pop-Location }
