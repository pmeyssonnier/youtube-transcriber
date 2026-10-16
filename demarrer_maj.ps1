[CmdletBinding()]
param(
    # Reserve aux tests : interpreteur Python a utiliser a la place de celui de .venv.
    [string]$PythonPath
)

# Mise a jour automatique d'un dossier clone avec git, appelee par demarrer.bat.
# Ce script ne bloque JAMAIS le demarrage (il se termine toujours avec le code 0). Si la nouvelle
# version ne s'installe pas ou ne demarre pas, le depot revient a la version precedente.

$ErrorActionPreference = 'Stop'
$ProjectDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDirectory

function Invoke-Native([string]$Executable, [string[]]$Arguments) {
    # Un programme externe qui ecrit sur stderr ne doit pas interrompre le script (Windows PowerShell 5.1).
    $SavedPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $Output = (& $Executable @Arguments 2>&1 | Out-String).Trim()
        return @{ Code = $LASTEXITCODE; Output = $Output }
    }
    finally { $ErrorActionPreference = $SavedPreference }
}

function Get-VenvPython {
    if ($PythonPath) { return $PythonPath }
    foreach ($Candidate in @((Join-Path $ProjectDirectory '.venv\Scripts\python.exe'), (Join-Path $ProjectDirectory '.venv/bin/python'))) {
        if (Test-Path -LiteralPath $Candidate) { return $Candidate }
    }
    return $null
}

function Install-Requirements([string]$Python) {
    $Arguments = @('-m', 'pip', 'install', '--quiet', '--disable-pip-version-check', '--upgrade', '-r', 'requirements.txt')
    if (Test-Path -LiteralPath (Join-Path $ProjectDirectory 'constraints.txt')) { $Arguments += @('-c', 'constraints.txt') }
    return Invoke-Native $Python $Arguments
}

try {
    $Old = Invoke-Native 'git' @('rev-parse', 'HEAD')
    if ($Old.Code -ne 0) { exit 0 }   # pas un depot git exploitable : on demarre tel quel
    $OldCommit = $Old.Output

    Write-Host 'Recuperation de la derniere version depuis GitHub...'
    $Pull = Invoke-Native 'git' @('pull', '--ff-only')
    if ($Pull.Code -ne 0) {
        Write-Host 'Mise a jour du code impossible (modifications locales, reseau...). Demarrage avec la version actuelle.'
        exit 0
    }
    $NewCommit = (Invoke-Native 'git' @('rev-parse', 'HEAD')).Output
    $Moved = ($NewCommit -ne $OldCommit)

    $Python = Get-VenvPython
    if (-not $Python) { exit 0 }

    Write-Host 'Verification des composants Python...'
    $Problem = $null
    $Pip = Install-Requirements $Python
    if ($Pip.Code -ne 0) {
        $Problem = "Les composants Python n'ont pas pu etre installes.`n$($Pip.Output)"
    }
    else {
        $Import = Invoke-Native $Python @('-c', 'import sys; sys.path.insert(0, sys.argv[1]); import app.main', $ProjectDirectory)
        if ($Import.Code -ne 0) { $Problem = "La nouvelle version ne demarre pas.`n$($Import.Output)" }
    }
    if (-not $Problem) { exit 0 }

    if (-not $Moved) {
        Write-Host "Probleme avec les composants Python (le code n'a pas change) : lancez INSTALLER.bat." -ForegroundColor Yellow
        Write-Host $Problem
        exit 0
    }

    # La mise a jour du code vient d'arriver mais ne fonctionne pas : retour a la version precedente.
    # --keep conserve vos modifications locales et refuse d'ecraser quoi que ce soit d'autre.
    Write-Host 'La mise a jour ne fonctionne pas : retour a la version precedente.' -ForegroundColor Yellow
    Write-Host $Problem
    $Back = Invoke-Native 'git' @('reset', '--keep', $OldCommit)
    if ($Back.Code -ne 0) {
        Write-Host "Retour automatique impossible. Pour revenir a la version precedente : git reset --hard $OldCommit" -ForegroundColor Red
        Write-Host $Back.Output
        exit 0
    }
    $Changed = Invoke-Native 'git' @('diff', '--quiet', $NewCommit, $OldCommit, '--', 'requirements.txt', 'constraints.txt')
    if ($Changed.Code -ne 0) {
        Write-Host 'Reinstallation des anciens composants Python...'
        $Again = Install-Requirements $Python
        if ($Again.Code -ne 0) { Write-Host 'Les anciens composants n''ont pas pu etre reinstalles : lancez INSTALLER.bat.' -ForegroundColor Yellow }
    }
    Write-Host 'Version precedente restauree. Reessayez plus tard ou signalez le probleme.' -ForegroundColor Yellow
}
catch {
    Write-Host "Mise a jour automatique ignoree : $($_.Exception.Message)" -ForegroundColor Yellow
}
exit 0
