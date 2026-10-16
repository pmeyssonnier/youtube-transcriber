[CmdletBinding()]
param(
    # Facultatif : chemin d'un zip deja telecharge (ou glissez le zip sur METTRE_A_JOUR.bat).
    [string]$ZipPath,
    # Reserve aux tests : interpreteur Python a utiliser a la place de celui de .venv.
    [string]$PythonPath
)

$ErrorActionPreference = 'Stop'
$ProjectDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDirectory

$ZipUrl = 'https://github.com/pmeyssonnier/youtube-transcriber/archive/refs/heads/main.zip'
$HealthUrl = 'http://127.0.0.1:8765/api/health'

# Fichiers et dossiers remplaces. Ne sont JAMAIS touches : .venv, .env, data, .app.lock,
# .sauvegarde_code. METTRE_A_JOUR.bat n'est pas remplace pendant son execution.
$CodeItems = @(
    'app', 'tests', 'README.md', 'LICENSE', 'VERSION', 'requirements.txt', 'constraints.txt',
    'run.py', 'INSTALLER.bat', 'demarrer.bat', 'installer_windows.ps1',
    'mise_a_jour.ps1', '.env.example', '.gitignore', 'docs'
)

# Un zip qui ne contient pas tout cela est incomplet : il est refuse avant toute modification.
$RequiredFiles = @(
    @('app', 'main.py'), @('app', 'pipeline.py'), @('app', 'static', 'index.html'),
    @('run.py'), @('VERSION'), @('requirements.txt'), @('constraints.txt'),
    @('demarrer.bat'), @('mise_a_jour.ps1'), @('installer_windows.ps1')
)

# Dossier de travail de la mise a jour, dans le projet pour que les deplacements restent de simples
# renommages : 'nouveau' = code prepare, 'ancien' = code remplace (permet de tout remettre en place).
$WorkRoot = Join-Path $ProjectDirectory '.mise_a_jour_en_cours'
$Staging = Join-Path $WorkRoot 'nouveau'
$Previous = Join-Path $WorkRoot 'ancien'
$AddedList = Join-Path $WorkRoot 'ajoutes.txt'
$BackupRoot = Join-Path $ProjectDirectory '.sauvegarde_code'
$LogFile = Join-Path $BackupRoot 'mise_a_jour.log'

function Write-Log([string]$Message) {
    try {
        if (-not (Test-Path -LiteralPath $BackupRoot)) { New-Item -ItemType Directory -Path $BackupRoot -Force | Out-Null }
        Add-Content -LiteralPath $LogFile -Value ('{0} {1}' -f (Get-Date -Format 's'), $Message) -Encoding UTF8
    }
    catch { }
}

function Write-Step([string]$Message, [string]$Color = 'Cyan') {
    Write-Host $Message -ForegroundColor $Color
    Write-Log $Message
}

function Join-Parts([string]$Base, [string[]]$Parts) {
    foreach ($Part in $Parts) { $Base = Join-Path $Base $Part }
    return $Base
}

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

function Get-VenvPython {
    if ($PythonPath) { return $PythonPath }
    foreach ($Candidate in @((Join-Path $ProjectDirectory '.venv\Scripts\python.exe'), (Join-Path $ProjectDirectory '.venv/bin/python'))) {
        if (Test-Path -LiteralPath $Candidate) { return $Candidate }
    }
    return $null
}

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

function Test-ApplicationRunning {
    try {
        Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 2 | Out-Null
        return $true
    }
    catch {
        return $false
    }
}

function Restore-FromPrevious {
    # Remet en place le code d'avant : supprime ce que la mise a jour a ajoute ou place, puis
    # replace chaque element deplace dans 'ancien'. Peut etre relance sans danger.
    if (Test-Path -LiteralPath $AddedList) {
        foreach ($Name in @(Get-Content -LiteralPath $AddedList | Where-Object { $_ })) {
            $Target = Join-Path $ProjectDirectory $Name
            if (Test-Path -LiteralPath $Target) { Remove-Item -LiteralPath $Target -Recurse -Force }
        }
    }
    if (Test-Path -LiteralPath $Previous) {
        foreach ($Entry in @(Get-ChildItem -LiteralPath $Previous -Force)) {
            $Target = Join-Path $ProjectDirectory $Entry.Name
            if (Test-Path -LiteralPath $Target) { Remove-Item -LiteralPath $Target -Recurse -Force }
            Move-Item -LiteralPath $Entry.FullName -Destination $Target -Force
        }
    }
    if (Test-Path -LiteralPath $WorkRoot) { Remove-Item -LiteralPath $WorkRoot -Recurse -Force }
}

function Invoke-Update {
    if (Test-ApplicationRunning) {
        throw "L'application est en cours d'execution. Fermez sa fenetre noire, puis relancez METTRE_A_JOUR.bat."
    }
    if (-not (Test-Path -LiteralPath (Join-Path $ProjectDirectory 'app'))) {
        if (-not (Test-Path -LiteralPath $Previous)) {
            throw "Ce dossier ne contient pas l'application (dossier 'app' introuvable)."
        }
    }

    # Une mise a jour precedente interrompue (coupure, fenetre fermee) laisse l'ancien code a cote :
    # on le remet en place avant toute chose.
    if (Test-Path -LiteralPath $WorkRoot) {
        Write-Step 'Une mise a jour precedente a ete interrompue : restauration de l''ancienne version...' 'Yellow'
        Restore-FromPrevious
        Write-Step 'Ancienne version restauree.' 'Yellow'
    }

    $CurrentVersion = Get-Version $ProjectDirectory
    $WorkDirectory = Join-Path ([IO.Path]::GetTempPath()) ("yt-transcriber-update-" + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $WorkDirectory | Out-Null
    Write-Log "Debut de la mise a jour depuis la version $CurrentVersion"

    try {
        if ($ZipPath) {
            if (-not (Test-Path -LiteralPath $ZipPath)) { throw "Fichier introuvable : $ZipPath" }
            $Zip = (Resolve-Path -LiteralPath $ZipPath).Path
            Write-Step "Utilisation du zip local : $Zip"
        }
        else {
            $Zip = Join-Path $WorkDirectory 'source.zip'
            Write-Step 'Telechargement de la derniere version...'
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
            if ((Test-Path -LiteralPath (Join-Parts $Candidate @('app', 'main.py'))) -and (Test-Path -LiteralPath (Join-Path $Candidate 'VERSION'))) {
                $NewRoot = $Candidate
                break
            }
        }
        if (-not $NewRoot) { throw "Ce zip ne ressemble pas a l'application (app\main.py ou VERSION manquant)." }

        $Missing = @($RequiredFiles | Where-Object { -not (Test-Path -LiteralPath (Join-Parts $NewRoot $_)) } | ForEach-Object { $_ -join '\' })
        if ($Missing.Count -gt 0) {
            throw ("Le zip est incomplet (fichiers manquants : " + ($Missing -join ', ') + "). Rien n'a ete modifie ; telechargez-le a nouveau.")
        }

        $NewVersion = Get-Version $NewRoot
        if ($NewVersion -eq $CurrentVersion -and -not $ZipPath) {
            Write-Step "Vous avez deja la derniere version ($CurrentVersion). Rien a faire." 'Green'
            return
        }
        Write-Step "Mise a jour : $CurrentVersion -> $NewVersion"

        # Le nouveau code doit au moins etre du Python valide avant de remplacer quoi que ce soit.
        $Python = Get-VenvPython
        if ($Python) {
            $Check = Invoke-Native $Python @('-m', 'compileall', '-q', (Join-Path $NewRoot 'app'))
            if ($Check.Code -ne 0) {
                throw ("Le code telecharge contient une erreur ; rien n'a ete modifie.`n" + $Check.Output)
            }
        }

        $OldRequirements = Get-RequirementLines (Join-Path $ProjectDirectory 'requirements.txt')
        $OldConstraints = Get-RequirementLines (Join-Path $ProjectDirectory 'constraints.txt')

        # Sauvegarde du code actuel (petite, sans donnees ni .venv), verifiee avant d'aller plus loin.
        $Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $BackupDirectory = Join-Path $BackupRoot "$CurrentVersion-$Stamp"
        New-Item -ItemType Directory -Path $BackupDirectory -Force | Out-Null
        foreach ($Item in $CodeItems) {
            $Source = Join-Path $ProjectDirectory $Item
            if (Test-Path -LiteralPath $Source) {
                Copy-Item -LiteralPath $Source -Destination $BackupDirectory -Recurse -Force
                if (-not (Test-Path -LiteralPath (Join-Path $BackupDirectory $Item))) {
                    throw "La sauvegarde de '$Item' a echoue ; rien n'a ete modifie."
                }
            }
        }
        Get-ChildItem -LiteralPath $BackupDirectory -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        Write-Step "Ancien code sauvegarde dans .sauvegarde_code\$CurrentVersion-$Stamp" 'DarkGray'

        # 1. Preparation : le nouveau code est copie a cote, sans toucher a l'ancien.
        New-Item -ItemType Directory -Path $Staging -Force | Out-Null
        New-Item -ItemType Directory -Path $Previous -Force | Out-Null
        foreach ($Item in $CodeItems) {
            $Source = Join-Path $NewRoot $Item
            if (-not (Test-Path -LiteralPath $Source)) { continue }
            Copy-Item -LiteralPath $Source -Destination (Join-Path $Staging $Item) -Recurse -Force
            if (-not (Test-Path -LiteralPath (Join-Path $Staging $Item))) {
                throw "La preparation de '$Item' a echoue ; rien n'a ete modifie."
            }
        }

        # 2. Remplacement par simples renommages, puis composants Python et controle de demarrage.
        # Au moindre echec, l'ancien code est remis en place.
        $PipAttempted = $false
        try {
            foreach ($Item in $CodeItems) {
                $Prepared = Join-Path $Staging $Item
                if (-not (Test-Path -LiteralPath $Prepared)) { continue }
                $Target = Join-Path $ProjectDirectory $Item
                if (Test-Path -LiteralPath $Target) {
                    Move-Item -LiteralPath $Target -Destination (Join-Path $Previous $Item) -Force
                }
                else {
                    Add-Content -LiteralPath $AddedList -Value $Item -Encoding UTF8
                }
                # Point d'echec simule pour les tests automatiques (jamais defini en usage normal).
                if ($env:YTT_SIMULATE_SWAP_FAILURE -eq $Item) { throw "Echec simule pendant le remplacement de '$Item'." }
                Move-Item -LiteralPath $Prepared -Destination $Target -Force
            }
            Get-ChildItem -LiteralPath (Join-Path $ProjectDirectory 'app') -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
                Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

            $NewRequirements = Get-RequirementLines (Join-Path $ProjectDirectory 'requirements.txt')
            $NewConstraints = Get-RequirementLines (Join-Path $ProjectDirectory 'constraints.txt')
            $Python = Get-VenvPython
            if ($OldRequirements -ne $NewRequirements -or $OldConstraints -ne $NewConstraints) {
                if ($Python) {
                    Write-Step 'Les composants Python ont change : installation des nouvelles versions...'
                    $PipArguments = @('-m', 'pip', 'install', '--upgrade', '-r', (Join-Path $ProjectDirectory 'requirements.txt'))
                    if (Test-Path -LiteralPath (Join-Path $ProjectDirectory 'constraints.txt')) {
                        $PipArguments += @('-c', (Join-Path $ProjectDirectory 'constraints.txt'))
                    }
                    $PipAttempted = $true
                    $Pip = Invoke-Native $Python $PipArguments
                    if ($Pip.Output) { Write-Host $Pip.Output }
                    if ($Pip.Code -ne 0) { throw "L'installation des composants Python a echoue." }
                }
                else {
                    Write-Step "Les composants Python ont change mais .venv est absent : lancez INSTALLER.bat." 'Yellow'
                }
            }

            if ($Python) {
                $Import = Invoke-Native $Python @('-c', 'import sys; sys.path.insert(0, sys.argv[1]); import app.main', $ProjectDirectory)
                if ($Import.Code -ne 0) {
                    throw ("La nouvelle version ne demarre pas.`n" + $Import.Output)
                }
            }
        }
        catch {
            $Cause = $_.Exception.Message
            Write-Log "ECHEC : $Cause"
            Write-Step 'Echec : restauration de l''ancienne version...' 'Yellow'
            try {
                Restore-FromPrevious
                if ($PipAttempted -and $Python) {
                    # Les composants ont pu etre modifies a moitie : on remet ceux de l'ancienne version.
                    $Back = @('-m', 'pip', 'install', '--upgrade', '-r', (Join-Path $ProjectDirectory 'requirements.txt'))
                    if (Test-Path -LiteralPath (Join-Path $ProjectDirectory 'constraints.txt')) {
                        $Back += @('-c', (Join-Path $ProjectDirectory 'constraints.txt'))
                    }
                    $Reinstall = Invoke-Native $Python $Back
                    if ($Reinstall.Code -ne 0) {
                        Write-Step 'Les anciens composants Python n''ont pas pu etre reinstalles : lancez INSTALLER.bat.' 'Yellow'
                    }
                }
            }
            catch {
                throw ("La mise a jour a echoue ($Cause) ET la restauration automatique a echoue ($($_.Exception.Message)). " +
                    "Votre ancien code est dans .sauvegarde_code\$CurrentVersion-$Stamp : copiez son contenu dans ce dossier.")
            }
            throw ("La mise a jour a echoue : $Cause`nL'ancienne version ($CurrentVersion) a ete restauree ; " +
                "vos donnees (.env, data) n'ont pas ete touchees. Details : .sauvegarde_code\mise_a_jour.log")
        }

        if (Test-Path -LiteralPath $WorkRoot) { Remove-Item -LiteralPath $WorkRoot -Recurse -Force -ErrorAction SilentlyContinue }
        Write-Host ''
        Write-Step "Mise a jour terminee : version $NewVersion." 'Green'
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
    Write-Log "ERREUR : $($_.Exception.Message)"
    exit 1
}
