# MedPeek v2-local: one-command setup for Windows
#
# Run it from the v2-local folder in PowerShell:
#     powershell -ExecutionPolicy Bypass -File .\setup.ps1
#
# It installs Python and Ollama if they're missing, downloads Gemma 4,
# installs the Python packages, builds the reference database and starts MedPeek.
# It is safe to run again; anything already done is skipped.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$Model = if ($env:MEDPEEK_MODEL) { $env:MEDPEEK_MODEL } else { "gemma4" }
$OllamaUrl = "http://localhost:11434"

function Step($number, $text) {
    Write-Host ""
    Write-Host "[$number/5] $text" -ForegroundColor Cyan
}

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user;$env:LOCALAPPDATA\Programs\Ollama"
}

function Has-Python {
    try {
        $version = & python --version 2>&1
        return ($LASTEXITCODE -eq 0 -and "$version" -match "Python 3")
    } catch {
        return $false
    }
}

function Ollama-Running {
    try {
        Invoke-RestMethod "$OllamaUrl/api/version" -TimeoutSec 2 | Out-Null
        return $true
    } catch {
        return $false
    }
}

Write-Host "MedPeek setup" -ForegroundColor Green

# 1. Python
Step 1 "Checking Python"
if (-not (Has-Python)) {
    Write-Host "Python not found. Installing Python 3.12 with winget..."
    winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
    Refresh-Path
    if (-not (Has-Python)) {
        Write-Host "Python was installed, but this window can't see it yet. Close PowerShell, open a new one and run setup.ps1 again." -ForegroundColor Yellow
        exit 1
    }
}
python --version

# 2. Ollama
Step 2 "Checking Ollama"
Refresh-Path
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    Write-Host "Ollama not found. Installing it from ollama.com..."
    Invoke-RestMethod https://ollama.com/install.ps1 | Invoke-Expression
    Refresh-Path
    if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
        Write-Host "Ollama was installed, but this window can't see it yet. Close PowerShell, open a new one and run setup.ps1 again." -ForegroundColor Yellow
        exit 1
    }
}
ollama --version

if (-not (Ollama-Running)) {
    Write-Host "Starting Ollama..."
    Start-Process ollama -ArgumentList "serve" -WindowStyle Hidden
    for ($i = 0; $i -lt 30 -and -not (Ollama-Running); $i++) { Start-Sleep -Seconds 1 }
    if (-not (Ollama-Running)) {
        Write-Host "Ollama didn't start. Open the Ollama app from the Start menu, then run setup.ps1 again." -ForegroundColor Yellow
        exit 1
    }
}

# 3. Gemma 4
Step 3 "Downloading $Model (about 7 GB the first time; skipped if you already have it)"
ollama pull $Model
if ($LASTEXITCODE -ne 0) {
    Write-Host "Couldn't download $Model. Check your internet connection and run setup.ps1 again." -ForegroundColor Yellow
    exit 1
}

# 4. Python packages
Step 4 "Installing Python packages"
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt --quiet

# 5. Reference database
Step 5 "Building the reference database (images.db)"
python create_db.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "create_db.py failed. Check your internet connection and run setup.ps1 again." -ForegroundColor Yellow
    exit 1
}

Write-Host ""
Write-Host "All set! Starting MedPeek. Open http://127.0.0.1:5050 in your browser." -ForegroundColor Green
Write-Host "Next time, just run:  python app.py   (from this folder)"
Write-Host "Press Ctrl+C to stop."
Write-Host ""
python app.py