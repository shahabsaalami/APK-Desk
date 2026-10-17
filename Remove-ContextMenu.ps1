$ErrorActionPreference = 'Stop'
$app = Join-Path $PSScriptRoot 'APKDesk.exe'
Start-Process -FilePath $app -ArgumentList '--remove-menu' -WindowStyle Hidden -Wait
