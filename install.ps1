# Daedalus installer for Windows: Python check + install, packages, shortcuts, and the Icarus agent (Ollama + model).
# Run through install.bat (double-click), or:  powershell -ExecutionPolicy Bypass -File install.ps1 [options]
#
#   -AgentOnly     only set up Icarus (Ollama + model)          -NoAgent      skip Icarus
#   -Model NAME    Ollama model to pull (default: picked from RAM)  -NoShortcut  no desktop / Start-menu shortcut
#   -Yes           answer yes to every question (unattended)     -CheckOnly    report only, change nothing
param(
    [switch]$AgentOnly,
    [switch]$NoAgent,
    [string]$Model = "",
    [switch]$NoShortcut,
    [switch]$Yes,
    [switch]$CheckOnly
)

$ErrorActionPreference = "Continue"
Set-Location -LiteralPath $PSScriptRoot
$Root = $PSScriptRoot
$Venv = Join-Path $Root ".venv"
$VPy = Join-Path $Venv "Scripts\python.exe"
$VPyw = Join-Path $Venv "Scripts\pythonw.exe"
$PyVer = "3.11.9"
$PyUrl = "https://www.python.org/ftp/python/$PyVer/python-$PyVer-amd64.exe"
$OllamaUrl = "https://ollama.com/download/OllamaSetup.exe"
$OllamaHost = "http://127.0.0.1:11434"
$script:Problems = @()

function Say($m, $c = "Gray") { Write-Host $m -ForegroundColor $c }
function Head($m) { Write-Host ""; Write-Host "== $m" -ForegroundColor Cyan }
function Ok($m) { Say "  [ok] $m" Green }
function Warn($m) { Say "  [!!] $m" Yellow; $script:Problems += $m }
function Ask($q) {
    if ($Yes) { return $true }
    $a = Read-Host "  $q [Y/n]"
    return ($a -eq "" -or $a -match '^[yY]')
}
function Has-Winget { return [bool](Get-Command winget -ErrorAction SilentlyContinue) }
function Download($url, $out) {
    Say "  downloading $url"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $old = $ProgressPreference; $ProgressPreference = "SilentlyContinue"
    try { Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing; return $true }
    catch { Warn "download failed: $($_.Exception.Message)"; return $false }
    finally { $ProgressPreference = $old }
}

# ------------------------------------------------------------------ Python
function Test-Py($exe) {
    if (-not $exe -or -not (Test-Path -LiteralPath $exe)) { return $false }
    & $exe -c "import sys, struct; sys.exit(0 if sys.version_info[:2] == (3, 11) and struct.calcsize('P') == 8 else 1)" 2>$null
    return ($LASTEXITCODE -eq 0)
}

function Find-Py311 {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $p = (& py -3.11 -c "import sys; print(sys.executable)" 2>$null)
        if ($LASTEXITCODE -eq 0 -and $p) {
            $p = "$p".Trim()
            if ((Test-Py $p) -and ($p -notmatch "WindowsApps")) { return $p }
        }
    }
    foreach ($p in @("$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
                     "$env:ProgramFiles\Python311\python.exe",
                     "C:\Python311\python.exe")) {
        if (Test-Py $p) { return $p }
    }
    return $null
}

function Install-Python {
    if (Has-Winget) {
        Say "  installing Python 3.11 with winget (current user)..."
        winget install -e --id Python.Python.3.11 --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Host
        $p = Find-Py311
        if ($p) { return $p }
    }
    $tmp = Join-Path $env:TEMP "python-$PyVer-amd64.exe"
    if (Download $PyUrl $tmp) {
        Say "  running the python.org installer (current user, no PATH changes)..."
        Start-Process -FilePath $tmp -ArgumentList "/quiet InstallAllUsers=0 PrependPath=0 Include_launcher=1 Include_test=0 Include_doc=0" -Wait
        return (Find-Py311)
    }
    return $null
}

function Setup-Python {
    Head "Python"
    $other = (Get-Command python -ErrorAction SilentlyContinue)
    if ($other -and $other.Source -match "WindowsApps") {
        Say "  note: 'python' on PATH is the Microsoft Store version — Daedalus uses its own Python 3.11 instead." DarkGray
    }
    $py = Find-Py311
    if ($py) { Ok "Python 3.11 64-bit: $py" }
    elseif ($CheckOnly) { Warn "Python 3.11 64-bit not found"; return $false }
    else {
        Warn "Python 3.11 64-bit not found (needed: the sketch solver ships wheels for 3.11)"
        if (-not (Ask "Install Python $PyVer now?")) { return $false }
        $py = Install-Python
        if (-not $py) { Warn "Python install failed — install 3.11 64-bit from https://www.python.org/downloads/ and run this again"; return $false }
        $script:Problems = @($script:Problems | Where-Object { $_ -notmatch "^Python 3.11" })
        Ok "Python 3.11 installed: $py"
    }

    if ((Test-Path $VPy) -and -not (Test-Py $VPy)) {
        Say "  existing .venv isn't Python 3.11 — rebuilding it"
        if (-not $CheckOnly) { Remove-Item -Recurse -Force $Venv }
    }
    if (-not (Test-Path $VPy)) {
        if ($CheckOnly) { Warn "no .venv yet"; return $false }
        Say "  creating the environment (.venv)..."
        & $py -m venv $Venv
        if (-not (Test-Path $VPy)) { Warn "couldn't create .venv"; return $false }
    }
    Ok "environment: $Venv"

    if (-not $CheckOnly) {
        Say "  installing packages (first time: several minutes)..."
        & $VPy -m pip install -q --upgrade pip
        & $VPy -m pip install -q --only-binary=:all: -r (Join-Path $Root "requirements.txt")
        if ($LASTEXITCODE -ne 0) { Warn "package install failed — see the messages above"; return $false }
        & $VPy -m pip install -q --only-binary=:all: -r (Join-Path $Root "requirements-optional.txt") 2>$null
        Ok "packages installed"
        Say "  pre-compiling (faster first launch)..."
        & $VPy -m compileall -q -j 0 (Join-Path $Root "daedalus") 2>$null | Out-Null
    }
    & $VPy -m daedalus --check --python-only
    return ($LASTEXITCODE -eq 0)
}

function Make-Shortcuts {
    if ($NoShortcut -or $CheckOnly) { return }
    Head "Shortcuts"
    $ico = Join-Path $Root "daedalus.ico"
    & $VPy -m daedalus --make-icon $ico | Out-Null
    $shell = New-Object -ComObject WScript.Shell
    $targets = @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs"))
    foreach ($dir in $targets) {
        $lnk = $shell.CreateShortcut((Join-Path $dir "Daedalus.lnk"))
        $lnk.TargetPath = $VPyw
        $lnk.Arguments = "-m daedalus"
        $lnk.WorkingDirectory = $Root
        if (Test-Path $ico) { $lnk.IconLocation = $ico }
        $lnk.Description = "Daedalus CAD"
        $lnk.Save()
    }
    Ok "Desktop and Start-menu shortcuts"
}

# ------------------------------------------------------------------ Icarus agent (Ollama)
function Ollama-Exe {
    $c = Get-Command ollama -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    $p = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
    if (Test-Path $p) { return $p }
    return $null
}
function Ollama-Up {
    try { Invoke-RestMethod -Uri "$OllamaHost/api/version" -TimeoutSec 3 | Out-Null; return $true } catch { return $false }
}
function Ollama-Models {
    try { return @((Invoke-RestMethod -Uri "$OllamaHost/api/tags" -TimeoutSec 5).models | ForEach-Object { $_.name }) }
    catch { return @() }
}
function Pick-Model {
    if ($Model) { return $Model }
    $gb = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
    if ($gb -ge 24) { $m = "qwen2.5:14b" } elseif ($gb -ge 12) { $m = "qwen2.5:7b" } else { $m = "qwen2.5:3b" }
    Say "  $gb GB RAM -> model $m (use -Model to choose another)" DarkGray
    return $m
}

function Setup-Agent {
    Head "Icarus agent (Ollama)"
    $exe = Ollama-Exe
    if ($exe) { Ok "Ollama installed: $exe" }
    elseif ($CheckOnly) { Warn "Ollama not installed" }
    else {
        Warn "Ollama not installed"
        if (Ask "Install Ollama now (about 1 GB)?") {
            if (Has-Winget) {
                winget install -e --id Ollama.Ollama --silent --accept-package-agreements --accept-source-agreements | Out-Host
                $exe = Ollama-Exe
            }
            if (-not $exe) {
                $tmp = Join-Path $env:TEMP "OllamaSetup.exe"
                if (Download $OllamaUrl $tmp) {
                    Start-Process -FilePath $tmp -ArgumentList "/SILENT /SUPPRESSMSGBOXES /NORESTART" -Wait
                    $exe = Ollama-Exe
                }
            }
            if ($exe) { $script:Problems = @($script:Problems | Where-Object { $_ -ne "Ollama not installed" }); Ok "Ollama installed" }
            else { Warn "Ollama install failed — get it from https://ollama.com/download"; return }
        } else { return }
    }

    if (-not (Ollama-Up)) {
        if ($CheckOnly) { Warn "Ollama isn't running"; return }
        Say "  starting Ollama..."
        Start-Process -FilePath $exe -ArgumentList "serve" -WindowStyle Hidden
        for ($i = 0; $i -lt 40 -and -not (Ollama-Up); $i++) { Start-Sleep -Milliseconds 500 }
    }
    if (Ollama-Up) { Ok "Ollama running at $OllamaHost" } else { Warn "Ollama didn't start — open it from the Start menu"; return }

    $m = Pick-Model
    $have = Ollama-Models
    if ($have -contains $m -or $have -contains "$m`:latest") { Ok "model $m downloaded" }
    elseif ($CheckOnly) { Warn "model $m not downloaded" }
    elseif (Ask "Download model $m now (several GB)?") {
        & $exe pull $m
        if ($LASTEXITCODE -eq 0) { Ok "model $m downloaded" } else { Warn "model download failed — run: ollama pull $m" }
    }
    if ((Test-Path $VPy) -and -not $CheckOnly) {
        & $VPy -m daedalus --set-model $m | Out-Null
        Ok "Icarus set to use $m"
    }
}

# ------------------------------------------------------------------ run
Say "Daedalus setup — $Root" Cyan
if ($env:PROCESSOR_ARCHITECTURE -ne "AMD64") { Say "  note: Daedalus needs 64-bit Windows (this is $env:PROCESSOR_ARCHITECTURE)" Yellow }

$pyOk = $true
if (-not $AgentOnly) {
    $pyOk = Setup-Python
    if ($pyOk) { Make-Shortcuts }
}
if (-not $NoAgent) { Setup-Agent }

Head "Result"
$checkArgs = @("-m", "daedalus", "--check")
if ($NoAgent) { $checkArgs += "--python-only" }
if (Test-Path $VPy) { & $VPy @checkArgs }
if ($script:Problems.Count -eq 0 -and $pyOk) {
    Say "Ready. Start Daedalus from the desktop shortcut or run_windows.bat." Green
    exit 0
}
Say "Finished with problems:" Yellow
$script:Problems | ForEach-Object { Say "  - $_" Yellow }
exit 1
