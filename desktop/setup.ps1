# Study Binder setup, for pasting into the Windows Run box (no file download needed):
#   powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/man1hutchinsom-dev/study-binder/main/desktop/setup.ps1 | iex"
# Does the same as "Install Study Binder.bat". Your notes are never deleted or changed.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Host.UI.RawUI.WindowTitle = 'Study Binder setup'
Write-Host ''
Write-Host '  Setting up Study Binder. This takes about a minute...'
Write-Host '  (Your notes are never deleted or changed by this setup.)'
Write-Host ''

function Stop-WithMessage($text) {
  Write-Host ''
  Write-Host "  $text"
  Write-Host ''
  Read-Host '  Press Enter to close'
  exit 1
}

$work = Join-Path $env:TEMP 'study-binder-setup'
if (Test-Path $work) { Remove-Item -Recurse -Force $work }
New-Item -ItemType Directory -Path $work | Out-Null

Write-Host '  Downloading the latest version...'
try {
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  $zip = Join-Path $work 'sb.zip'
  Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/man1hutchinsom-dev/study-binder/archive/refs/heads/main.zip' -OutFile $zip
  Expand-Archive -Force -Path $zip -DestinationPath $work
} catch {
  Stop-WithMessage "Could not download Study Binder. Check you're connected to the internet, then try again."
}
$install = Join-Path $work 'study-binder-main\desktop\install.py'
if (-not (Test-Path $install)) { Stop-WithMessage 'Could not download Study Binder. Please try again.' }

# Find Python (the same one your other apps use).
$py = $null; $pyArgs = @()
$ErrorActionPreference = 'Continue'
if (Get-Command py -ErrorAction SilentlyContinue) {
  & py -3 --version *> $null
  if ($LASTEXITCODE -eq 0) { $py = 'py'; $pyArgs = @('-3') }
}
if (-not $py -and (Get-Command python -ErrorAction SilentlyContinue)) {
  & python --version *> $null
  if ($LASTEXITCODE -eq 0) { $py = 'python' }
}
if (-not $py) {
  $found = @(Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe", "$env:ProgramFiles\Python3*\python.exe" -ErrorAction SilentlyContinue)
  if ($found.Count -gt 0) { $py = $found[-1].FullName }
}
if (-not $py) {
  Stop-WithMessage 'Python was not found. Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH"), then try again.'
}

& $py @pyArgs $install
if ($LASTEXITCODE -ne 0) { Stop-WithMessage 'Setup did not finish. Please send a photo of this window to Claude.' }
Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
Write-Host '  This window will close by itself.'
Start-Sleep -Seconds 10
