Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
& "D:\conda-envs\smart_kg\python.exe" -m smart_kg.cli demo
