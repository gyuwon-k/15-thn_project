param([switch]$SkipFrontend,[switch]$SkipStage,[switch]$SkipInstaller,[switch]$InstallerOnly,[string]$Python,[string]$InnoCompiler)
$ErrorActionPreference='Stop'
$projectRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not $Python) { $Python=Join-Path $projectRoot '.venv/Scripts/python.exe' }
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw 'Create the project .venv or supply -Python.' }
if (-not $InnoCompiler) { $compiler=Get-Command ISCC.exe -ErrorAction SilentlyContinue; if ($compiler) { $InnoCompiler=$compiler.Source } }
$arguments=@('-X','utf8', (Join-Path $PSScriptRoot 'build_release.py'),'--python',$Python)
if ($InnoCompiler) { $arguments+=@('--inno',$InnoCompiler) }
if ($SkipFrontend) { $arguments+='--skip-frontend' }
if ($SkipStage) { $arguments+='--skip-stage' }
if ($SkipInstaller) { $arguments+='--skip-installer' }
if ($InstallerOnly) { $arguments+='--installer-only' }
& $Python @arguments
if ($LASTEXITCODE -ne 0) { throw 'Release build failed. No unverified package should be distributed.' }
