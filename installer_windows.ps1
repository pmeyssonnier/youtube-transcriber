[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProjectDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDirectory

function Refresh-ProcessPath {
    $MachinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $UserPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = @($MachinePath, $UserPath) -join ';'
}

function Assert-Winget {
    if (-not (Get-Command 'winget' -ErrorAction SilentlyContinue)) {
        throw "winget est introuvable. Installez ou mettez a jour 'App Installer' depuis le Microsoft Store, puis relancez INSTALLER.bat."
    }
}

function Ensure-WingetPackage {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Command,
        [Parameter(Mandatory = $true)]
        [string]$PackageId
    )

    if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) {
        Assert-Winget
        Write-Host "Installation de $PackageId..." -ForegroundColor Cyan
        & winget install --source winget --id $PackageId --exact --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) {
            throw "L'installation de $PackageId a echoue."
        }
        Refresh-ProcessPath
    }
}

function Get-PythonInvocation {
    $Candidates = @()

    $KnownPaths = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
        (Join-Path $env:ProgramFiles 'Python312\python.exe')
    )
    foreach ($KnownPath in $KnownPaths) {
        if (Test-Path -LiteralPath $KnownPath) {
            $Candidates += @{
                Executable = $KnownPath
                Prefix = @()
            }
        }
    }

    $PythonCommand = Get-Command 'python' -ErrorAction SilentlyContinue
    if ($PythonCommand -and $PythonCommand.Source -notmatch '\\WindowsApps\\python(3)?\.exe$') {
        $Candidates += @{
            Executable = $PythonCommand.Source
            Prefix = @()
        }
    }

    # Le lanceur "py.exe" peut etre present alors qu'aucun runtime Python
    # n'est installe. On le teste donc en dernier et sans laisser son message
    # d'erreur interrompre le script lorsque ErrorActionPreference vaut Stop.
    $PyLauncher = Get-Command 'py' -ErrorAction SilentlyContinue
    if ($PyLauncher) {
        $Candidates += @{
            Executable = $PyLauncher.Source
            Prefix = @('-3.12')
        }
    }

    foreach ($Candidate in $Candidates) {
        $Prefix = $Candidate.Prefix
        $PreviousErrorActionPreference = $ErrorActionPreference
        $VersionCheck = $null
        $ExitCode = 1

        try {
            $ErrorActionPreference = 'SilentlyContinue'
            $VersionCheck = & $Candidate.Executable @Prefix -c "import sys; print('OK' if sys.version_info >= (3, 11) else 'OLD')" 2>$null
            $ExitCode = $LASTEXITCODE
        }
        catch {
            $ExitCode = 1
        }
        finally {
            $ErrorActionPreference = $PreviousErrorActionPreference
        }

        if ($ExitCode -eq 0 -and $VersionCheck -eq 'OK') {
            return $Candidate
        }
    }

    return $null
}

$PythonInvocation = Get-PythonInvocation
if (-not $PythonInvocation) {
    Assert-Winget
    Write-Host 'Installation de Python.Python.3.12...' -ForegroundColor Cyan
    & winget install --source winget --id Python.Python.3.12 --exact --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "L'installation de Python a echoue."
    }

    Refresh-ProcessPath
    $PythonInvocation = Get-PythonInvocation
}
Ensure-WingetPackage -Command 'ffmpeg' -PackageId 'Gyan.FFmpeg'
Ensure-WingetPackage -Command 'deno' -PackageId 'DenoLand.Deno'

if (-not $PythonInvocation) {
    throw "Python vient d'etre installe, mais cette fenetre ne le trouve pas encore. Fermez-la puis relancez INSTALLER.bat."
}
if (-not (Get-Command 'ffmpeg' -ErrorAction SilentlyContinue) -or -not (Get-Command 'ffprobe' -ErrorAction SilentlyContinue)) {
    throw "FFmpeg vient d'etre installe, mais cette fenetre ne le trouve pas encore. Fermez-la puis relancez INSTALLER.bat."
}
if (-not (Get-Command 'deno' -ErrorAction SilentlyContinue)) {
    throw "Deno vient d'etre installe, mais cette fenetre ne le trouve pas encore. Fermez-la puis relancez INSTALLER.bat."
}

$PythonExecutable = $PythonInvocation.Executable
$PythonPrefix = $PythonInvocation.Prefix
$VenvPython = Join-Path $ProjectDirectory '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Host "Creation de l'environnement Python..." -ForegroundColor Cyan
    & $PythonExecutable @PythonPrefix -m venv (Join-Path $ProjectDirectory '.venv')
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPython)) {
        throw "La creation de l'environnement Python a echoue."
    }
}

Write-Host "Installation des composants de l'application..." -ForegroundColor Cyan
& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    throw "La mise a jour de pip a echoue."
}

& $VenvPython -m pip install --upgrade -r (Join-Path $ProjectDirectory 'requirements.txt') -c (Join-Path $ProjectDirectory 'constraints.txt')

if ($LASTEXITCODE -ne 0) {
    throw "L'installation des composants Python a echoue."
}

Write-Host ''
Write-Host 'Installation terminee (version 1.3.0).' -ForegroundColor Green
Write-Host "Utilisez maintenant demarrer.bat pour ouvrir l'application."
Write-Host "Si Windows signale encore une commande introuvable, fermez cette fenetre puis relancez INSTALLER.bat."
