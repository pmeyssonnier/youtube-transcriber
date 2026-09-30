[CmdletBinding()]
param(
    # Facultatif : chemin d'un zip deja telecharge (ou glissez le zip sur METTRE_A_JOUR.bat).
    [string]$ZipPath
)

$ErrorActionPreference = 'Stop'
$ProjectDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDirectory

$ZipUrl = 'https://github.com/pmeyssonnier/youtube-transcriber/archive/refs/heads/main.zip'
$HealthUrl = 'http://127.0.0.1:8765/api/health'

# Fichiers et dossiers remplaces. Ne sont JAMAIS touches : .venv, .env, data, .app.lock,
# .sauvegarde_code. METTRE_A_JOUR.bat n'est pas remplace pendant son execution.
$CodeItems = @(
    'app', 'tests', 'README.md', 'VERSION', 'requirements.txt', 'constraints.txt',
    'run.py', 'INSTALLER.bat', 'demarrer.bat', 'installer_windows.ps1',
    'mise_a_jour.ps1', '.env.example', '.gitignore'
)

function Get-Version([string]$Directory) {
    $File = Join-Path $Directory 'VERSION'
    if (Test-Path -LiteralPath $File) { return (Get-Content -LiteralPath $File -Raw).Trim() }
    return 'inconnue'
}

function Get-RequirementLines([string]$Path) {
    # Seules les lignes utiles comptent : un commentaire modifie ne doit pas declencher une reinstallation.
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    $Lines = Get-Content -LiteralPath $Path | ForEach-Object { $_.Trim() } | Where-Object { $_ -and -not $_.StartsWith('#') }
    return (($Lines | Sort-Object) -join "`n")
}

function Test-ApplicationRunning {
    try {
        Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 2 | Out-Null
        return $true
    }
    catch {
        return $false
    }
}

function Invoke-Update {
    if (Test-ApplicationRunning) {
        throw "L'application est en cours d'execution. Fermez sa fenetre noire, puis relancez METTRE_A_JOUR.bat."
    }
    if (-not (Test-Path -LiteralPath (Join-Path $ProjectDirectory 'app'))) {
        throw "Ce dossier ne contient pas l'application (dossier 'app' introuvable)."
    }

    $CurrentVersion = Get-Version $ProjectDirectory
    $WorkDirectory = Join-Path ([IO.Path]::GetTempPath()) ("yt-transcriber-update-" + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $WorkDirectory | Out-Null

    try {
        if ($ZipPath) {
            if (-not (Test-Path -LiteralPath $ZipPath)) { throw "Fichier introuvable : $ZipPath" }
            $Zip = (Resolve-Path -LiteralPath $ZipPath).Path
            Write-Host "Utilisation du zip local : $Zip" -ForegroundColor Cyan
        }
        else {
            $Zip = Join-Path $WorkDirectory 'source.zip'
            Write-Host 'Telechargement de la derniere version...' -ForegroundColor Cyan
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            try {
                Invoke-WebRequest -Uri $ZipUrl -OutFile $Zip -UseBasicParsing
            }
            catch {
                throw ("Telechargement impossible ($($_.Exception.Message)). " +
                    "Si le depot est prive, telechargez le zip depuis GitHub (Code > Download ZIP), " +
                    "puis glissez-le sur METTRE_A_JOUR.bat.")
            }
        }

        $Extracted = Join-Path $WorkDirectory 'extracted'
        try {
            Expand-Archive -LiteralPath $Zip -DestinationPath $Extracted -Force
        }
        catch {
            throw "Le fichier zip est illisible ou corrompu. Telechargez-le a nouveau."
        }

        # Le zip GitHub contient un dossier racine (youtube-transcriber-main) ; un zip manuel peut ne pas en avoir.
        $NewRoot = $null
        $Candidates = @($Extracted) + @(Get-ChildItem -LiteralPath $Extracted -Directory | ForEach-Object { $_.FullName })
        foreach ($Candidate in $Candidates) {
            if ((Test-Path -LiteralPath (Join-Path $Candidate 'app\main.py')) -and (Test-Path -LiteralPath (Join-Path $Candidate 'VERSION'))) {
                $NewRoot = $Candidate
                break
            }
        }
        if (-not $NewRoot) { throw "Ce zip ne ressemble pas a l'application (app\main.py ou VERSION manquant)." }

        $NewVersion = Get-Version $NewRoot
        if ($NewVersion -eq $CurrentVersion -and -not $ZipPath) {
            Write-Host "Vous avez deja la derniere version ($CurrentVersion). Rien a faire." -ForegroundColor Green
            return
        }
        Write-Host "Mise a jour : $CurrentVersion -> $NewVersion" -ForegroundColor Cyan

        $OldRequirements = Get-RequirementLines (Join-Path $ProjectDirectory 'requirements.txt')
        $OldConstraints = Get-RequirementLines (Join-Path $ProjectDirectory 'constraints.txt')

        # Sauvegarde du code actuel (petite, sans donnees ni .venv).
        $Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $BackupDirectory = Join-Path $ProjectDirectory ".sauvegarde_code\$CurrentVersion-$Stamp"
        New-Item -ItemType Directory -Path $BackupDirectory -Force | Out-Null
        foreach ($Item in $CodeItems) {
            $Source = Join-Path $ProjectDirectory $Item
            if (Test-Path -LiteralPath $Source) {
                Copy-Item -LiteralPath $Source -Destination $BackupDirectory -Recurse -Force
            }
        }
        Get-ChildItem -LiteralPath $BackupDirectory -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "Ancien code sauvegarde dans .sauvegarde_code\$CurrentVersion-$Stamp" -ForegroundColor DarkGray

        # Remplacement du code.
        foreach ($Item in $CodeItems) {
            $Source = Join-Path $NewRoot $Item
            $Target = Join-Path $ProjectDirectory $Item
            if (-not (Test-Path -LiteralPath $Source)) { continue }
            if (Test-Path -LiteralPath $Target -PathType Container) {
                Remove-Item -LiteralPath $Target -Recurse -Force
            }
            Copy-Item -LiteralPath $Source -Destination $Target -Recurse -Force
        }
        Get-ChildItem -LiteralPath (Join-Path $ProjectDirectory 'app') -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

        # Dependances : seulement si la liste a change.
        $NewRequirements = Get-RequirementLines (Join-Path $ProjectDirectory 'requirements.txt')
        $NewConstraints = Get-RequirementLines (Join-Path $ProjectDirectory 'constraints.txt')
        if ($OldRequirements -ne $NewRequirements -or $OldConstraints -ne $NewConstraints) {
            $VenvPython = Join-Path $ProjectDirectory '.venv\Scripts\python.exe'
            if (Test-Path -LiteralPath $VenvPython) {
                Write-Host 'Les composants Python ont change : installation des nouvelles versions...' -ForegroundColor Cyan
                $PipArguments = @('-m', 'pip', 'install', '--upgrade', '-r', (Join-Path $ProjectDirectory 'requirements.txt'))
                if (Test-Path -LiteralPath (Join-Path $ProjectDirectory 'constraints.txt')) {
                    $PipArguments += @('-c', (Join-Path $ProjectDirectory 'constraints.txt'))
                }
                & $VenvPython @PipArguments
                if ($LASTEXITCODE -ne 0) { throw "L'installation des composants Python a echoue." }
            }
            else {
                Write-Host "Les composants Python ont change mais .venv est absent : lancez INSTALLER.bat." -ForegroundColor Yellow
            }
        }

        Write-Host ''
        Write-Host "Mise a jour terminee : version $NewVersion." -ForegroundColor Green
        Write-Host 'Vos donnees (.env, data) n''ont pas ete touchees.'
        Write-Host "Relancez l'application avec demarrer.bat, puis faites Ctrl+F5 dans le navigateur."
    }
    finally {
        Remove-Item -LiteralPath $WorkDirectory -Recurse -Force -ErrorAction SilentlyContinue
    }
}

try {
    Invoke-Update
}
catch {
    Write-Host ''
    Write-Host "ERREUR : $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
