$ErrorActionPreference = 'Stop'
$app = Join-Path $PSScriptRoot 'APKDesk.exe'
Start-Process -FilePath $app -ArgumentList '--install-menu' -WindowStyle Hidden -Wait
