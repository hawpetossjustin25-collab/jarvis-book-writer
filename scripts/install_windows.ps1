# Installs Jarvis Book Writer for the current Windows user and puts an icon on the OneDrive desktop.
# Usage: powershell -ExecutionPolicy Bypass -File install_windows.ps1 [-Source <folder>] [-SkipTests]
#        [-Commit <sha>] [-Channel stable|preview|internal|development|emergency]
# Writes app\version.json (commit, channel, test result) for the NEXUS Release Center.
param([string]$Source = '', [switch]$SkipTests, [string]$Commit = '', [string]$Channel = 'stable')
$ErrorActionPreference = 'Stop'
$repoApi = 'https://api.github.com/repos/hawpetossjustin25-collab/jarvis-book-writer/commits/main'
$repoArchive = 'https://github.com/hawpetossjustin25-collab/jarvis-book-writer/archive'
$root = Join-Path $env:LOCALAPPDATA 'JarvisBookWriter'
$app = Join-Path $root 'app'

$py = (Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue |
       Sort-Object FullName -Descending | Select-Object -First 1).FullName
if (-not $py) { $py = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $py) { throw 'Python 3 not found' }
$pyw = Join-Path (Split-Path $py) 'pythonw.exe'

New-Item -ItemType Directory -Force -Path $root | Out-Null
# While the lock exists, a Jarvis opened by hand says it is updating instead of starting on half-replaced files.
$lock = Join-Path $root 'installing.lock'
Set-Content -Path $lock -Value (Get-Date -Format o)
try {
Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like '*-m jarvis_books*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
$tmp = Join-Path $root 'extract'
Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
if ($Source) {
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    Copy-Item -Recurse -Force $Source (Join-Path $tmp 'src')
    if (-not $Commit -and (Test-Path (Join-Path $Source '.git'))) {
        $Commit = (& git -C $Source rev-parse HEAD 2>$null)
    }
} else {
    if (-not $Commit) {
        $Commit = (Invoke-RestMethod -UseBasicParsing $repoApi).sha
    }
    $zip = Join-Path $root 'src.zip'
    Invoke-WebRequest -UseBasicParsing "$repoArchive/$Commit.zip" -OutFile $zip
    Expand-Archive -Force $zip $tmp
    Remove-Item -Force $zip
}
# App code is replaced; books, settings and the key live in %LOCALAPPDATA%\JarvisBooks and OneDrive, untouched.
Remove-Item -Recurse -Force $app -ErrorAction SilentlyContinue
Move-Item (Get-ChildItem $tmp | Select-Object -First 1).FullName $app
Remove-Item -Recurse -Force $tmp

$tests = @{ ran = $false }
$ErrorActionPreference = 'Continue'
& $py -m pip install --user --quiet --disable-pip-version-check -r (Join-Path $app 'requirements-dev.txt') 2>&1 |
    Select-Object -Last 3
& $py -c "import PyQt6.QtWidgets, PyQt6.QtMultimedia, docx, websockets, edge_tts, imageio_ffmpeg"
if ($LASTEXITCODE -ne 0) { throw 'dependency install failed' }
# Download the 'Hey Jarvis' wake-word model once (runs on the laptop; optional).
Push-Location $app
& $py -c "from jarvis_books.ui.voice_bridge import ensure_wake_model; ensure_wake_model()" 2>&1 | Select-Object -Last 2
Pop-Location

# The Release Center reads version.json while the tests run, so the commit is recorded before them.
if ($Commit) {
    @{ commit = "$Commit".Trim().ToLower(); channel = $Channel; tests = @{ ran = $false; summary = 'tests running' }
       installed_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() } |
        ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 (Join-Path $app 'version.json')
}
if (-not $SkipTests) {
    Push-Location $app
    $env:QT_QPA_PLATFORM = 'offscreen'
    $summary = & $py -m pytest -q -p no:cacheprovider 2>&1 | Select-Object -Last 3
    $testExit = $LASTEXITCODE
    $summary
    $tests = @{ ran = $true; exit = $testExit; summary = (($summary | Out-String).Trim()) }
    Remove-Item Env:QT_QPA_PLATFORM
    Pop-Location
    if ($testExit -ne 0) { throw "tests failed ($testExit)" }
}
if ($Commit) {
    $meta = @{ commit = "$Commit".Trim().ToLower(); channel = $Channel; tests = $tests
               installed_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }
    $meta | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 (Join-Path $app 'version.json')
}
} finally {
    Remove-Item -Force $lock -ErrorAction SilentlyContinue
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
