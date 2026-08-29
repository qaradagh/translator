<#
    gametrans - self-update without git.

    Many people download the project as a ZIP rather than cloning it, so the
    folder is not a git repository and `git pull` cannot work there. This script
    fetches the current ZIP from GitHub and copies it over the installation,
    deliberately preserving everything the user owns: their API keys, their
    settings, their translation cache and their virtualenv.

    It is launched detached by START-HERE.bat, which exits immediately, because
    cmd.exe keeps a handle on a running batch file and rewriting it underneath
    a live process corrupts the rest of the run.
#>

param(
    [Parameter(Mandatory = $true)][string]$ProjectDir,
    [string]$Repo = "qaradagh/translator",
    [string]$Branch = "main"
)

$ErrorActionPreference = "Stop"

# Files and folders that belong to the user, never to the update.
$Preserve = @(".env", ".env.local", "config.toml", ".venv", "translation-cache.sqlite3")

function Fail($message) {
    Write-Host ""
    Write-Host "  Update failed: $message" -ForegroundColor Red
    Write-Host ""
    Write-Host "  Nothing was changed. Your existing installation still works."
    Write-Host ""
    Read-Host "  Press Enter to close"
    exit 1
}

Write-Host ""
Write-Host "  ============================================================"
Write-Host "    Updating gametrans"
Write-Host "  ============================================================"
Write-Host ""

$ProjectDir = (Resolve-Path $ProjectDir).Path
Write-Host "  Folder: $ProjectDir"

$work = Join-Path $env:TEMP "gametrans-update"
if (Test-Path $work) { Remove-Item $work -Recurse -Force }
New-Item -ItemType Directory -Path $work | Out-Null

$zipPath = Join-Path $work "source.zip"
$url = "https://github.com/$Repo/archive/refs/heads/$Branch.zip"

Write-Host "  Downloading the newest version..."
try {
    # TLS 1.2 for older PowerShell hosts, and no progress bar (it is very slow).
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $ProgressPreference = "SilentlyContinue"
    Invoke-WebRequest -Uri $url -OutFile $zipPath -UseBasicParsing
} catch {
    Fail "could not download from GitHub. Check your internet connection.`n  ($($_.Exception.Message))"
}

Write-Host "  Unpacking..."
try {
    Expand-Archive -Path $zipPath -DestinationPath $work -Force
} catch {
    Fail "the downloaded file could not be unpacked. ($($_.Exception.Message))"
}

# GitHub wraps the tree in a single folder whose name encodes the branch, so
# find it rather than assuming what it is called.
$extracted = Get-ChildItem -Path $work -Directory | Select-Object -First 1
if (-not $extracted) { Fail "the download did not contain the expected files." }

# Sanity check: refuse to copy something that is not actually this project.
if (-not (Test-Path (Join-Path $extracted.FullName "src\gametrans\cli.py"))) {
    Fail "the download does not look like gametrans. Aborting rather than overwriting your folder."
}

Write-Host "  Copying files (your key and settings are kept)..."
$copied = 0
Get-ChildItem -Path $extracted.FullName -Force | ForEach-Object {
    if ($Preserve -contains $_.Name) {
        Write-Host "    keeping your $($_.Name)"
        return
    }
    $target = Join-Path $ProjectDir $_.Name
    try {
        if ($_.PSIsContainer) {
            Copy-Item $_.FullName -Destination $ProjectDir -Recurse -Force
        } else {
            Copy-Item $_.FullName -Destination $target -Force
        }
        $copied++
    } catch {
        Write-Host "    could not replace $($_.Name): $($_.Exception.Message)" -ForegroundColor Yellow
    }
}
Write-Host "  Updated $copied items."

$python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
if (Test-Path $python) {
    Write-Host "  Reinstalling..."
    Push-Location $ProjectDir
    & $python -m pip install -e ".[windows,dxcam,hotkeys]" --quiet --disable-pip-version-check
    Pop-Location
} else {
    Write-Host "  No virtualenv found - run setup-windows.bat once." -ForegroundColor Yellow
}

Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "  ============================================================"
Write-Host "    Update complete."
Write-Host ""
Write-Host "    Your API key and settings were kept."
Write-Host "    Double-click START-HERE.bat to continue."
Write-Host "  ============================================================"
Write-Host ""
Read-Host "  Press Enter to close"
