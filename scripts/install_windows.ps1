# Installs Jarvis Book Writer for the current Windows user and puts an icon on the OneDrive desktop.
# Usage: powershell -ExecutionPolicy Bypass -File install_windows.ps1 [-Source <folder>] [-SkipTests]
param([string]$Source = '', [switch]$SkipTests)
$ErrorActionPreference = 'Stop'
$repoZip = 'https://github.com/hawpetossjustin25-collab/jarvis-book-writer/archive/refs/heads/main.zip'
$root = Join-Path $env:LOCALAPPDATA 'JarvisBookWriter'
$app = Join-Path $root 'app'

$py = (Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue |
       Sort-Object FullName -Descending | Select-Object -First 1).FullName
if (-not $py) { $py = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $py) { throw 'Python 3 not found' }
$pyw = Join-Path (Split-Path $py) 'pythonw.exe'

New-Item -ItemType Directory -Force -Path $root | Out-Null
$tmp = Join-Path $root 'extract'
Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
if ($Source) {
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    Copy-Item -Recurse -Force $Source (Join-Path $tmp 'src')
} else {
    $zip = Join-Path $root 'src.zip'
    Invoke-WebRequest -UseBasicParsing $repoZip -OutFile $zip
    Expand-Archive -Force $zip $tmp
    Remove-Item -Force $zip
}
# App code is replaced; books, settings and the key live in %LOCALAPPDATA%\JarvisBooks and OneDrive, untouched.
Remove-Item -Recurse -Force $app -ErrorAction SilentlyContinue
Move-Item (Get-ChildItem $tmp | Select-Object -First 1).FullName $app
Remove-Item -Recurse -Force $tmp

$ErrorActionPreference = 'Continue'
& $py -m pip install --user --quiet --disable-pip-version-check -r (Join-Path $app 'requirements-dev.txt') 2>&1 |
    Select-Object -Last 3
& $py -c "import PyQt6.QtWidgets, PyQt6.QtMultimedia, docx, websockets, edge_tts, imageio_ffmpeg"
if ($LASTEXITCODE -ne 0) { throw 'dependency install failed' }
# Download the 'Hey Jarvis' wake-word model once (runs on the laptop; optional).
& $py -c "import openwakeword.utils as u; u.download_models(['hey_jarvis'])" 2>&1 | Out-Null

if (-not $SkipTests) {
    Push-Location $app
    $env:QT_QPA_PLATFORM = 'offscreen'
    & $py -m pytest -q -p no:cacheprovider 2>&1 | Select-Object -Last 3
    $testExit = $LASTEXITCODE
    Remove-Item Env:QT_QPA_PLATFORM
    Pop-Location
    if ($testExit -ne 0) { throw "tests failed ($testExit)" }
}

$desktop = Join-Path $env:OneDrive 'Desktop'
if (-not $env:OneDrive -or -not (Test-Path $desktop)) { $desktop = [Environment]::GetFolderPath('Desktop') }
$lnk = Join-Path $desktop 'Jarvis Book Writer.lnk'
$shell = New-Object -ComObject WScript.Shell
$s = $shell.CreateShortcut($lnk)
$s.TargetPath = $pyw
$s.Arguments = '-m jarvis_books'
$s.WorkingDirectory = $app
$s.IconLocation = "$env:SystemRoot\System32\imageres.dll,186"
$s.Description = 'Jarvis Book Writer'
$s.Save()
Write-Output "INSTALLED $app"
Write-Output "SHORTCUT $lnk"
