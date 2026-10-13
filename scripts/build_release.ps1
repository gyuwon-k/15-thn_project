param([switch]$SkipFrontend,[switch]$SkipStage,[switch]$SkipInstaller)
$ErrorActionPreference='Stop'
$projectRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$buildPython=Join-Path $projectRoot '../pilot-studio/.venv/Scripts/python.exe'
Push-Location -LiteralPath $projectRoot
try {
    if (-not $SkipFrontend) {
        Push-Location -LiteralPath (Join-Path $projectRoot 'frontend')
        try { & npm.cmd run build; if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed' } } finally { Pop-Location }
    }
    if (-not $SkipStage) { & $buildPython -X utf8 scripts/stage_runtime.py; if ($LASTEXITCODE -ne 0) { throw 'Offline runtime staging failed' } }
    & $buildPython -X utf8 -m PyInstaller --noconfirm packaging/pilot-final.spec
    if ($LASTEXITCODE -ne 0) { throw 'Executable build failed' }
    if (-not $SkipInstaller) {
        $compiler=Join-Path $projectRoot 'packaging/tools/InnoSetup/ISCC.exe'
        if (-not (Test-Path -LiteralPath $compiler)) { throw 'Official Inno Setup compiler is required at packaging/tools/InnoSetup/ISCC.exe' }
        & $compiler packaging/installer.iss
        if ($LASTEXITCODE -ne 0) { throw 'Installer build failed' }
    }
    Write-Output (Join-Path $projectRoot 'dist/PILOT-Studio-Setup-1.0.0.exe')
} finally { Pop-Location }
