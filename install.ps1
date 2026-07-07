# EchoLocate Windows Installer (PowerShell)
# Run from the echolocate project root: .\install.ps1
# Tested on Windows 10/11 with PowerShell 5.1+

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  EchoLocate Installer (Windows)" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

# --- Step 1: Check Python 3.10+ ---
Write-Host "[1/8] Checking Python version..." -ForegroundColor Yellow
try {
    $pythonVersion = python --version 2>&1
    if ($pythonVersion -match "Python (\d+)\.(\d+)") {
        $major = [int]$Matches[1]
        $minor = [int]$Matches[2]
        if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 10)) {
            Write-Host "[FAIL] Python 3.10+ required. Found: $pythonVersion" -ForegroundColor Red
            Write-Host "       Download from: https://www.python.org/downloads/"
            exit 1
        }
        Write-Host "[PASS] $pythonVersion" -ForegroundColor Green
    }
} catch {
    Write-Host "[FAIL] Python not found. Install from https://www.python.org/downloads/" -ForegroundColor Red
    exit 1
}

# --- Step 2: Create virtualenv ---
Write-Host "[2/8] Creating virtual environment..." -ForegroundColor Yellow
$VenvPath = Join-Path $ProjectRoot ".venv"
if (Test-Path $VenvPath) {
    Write-Host "       Virtual environment already exists, reusing."
} else {
    python -m venv $VenvPath
    Write-Host "[PASS] Virtual environment created at: $VenvPath" -ForegroundColor Green
}

$PipExe = Join-Path $VenvPath "Scripts\pip.exe"
$PythonExe = Join-Path $VenvPath "Scripts\python.exe"

# --- Step 3: Install dependencies ---
Write-Host "[3/8] Installing Python dependencies (this may take a few minutes)..." -ForegroundColor Yellow
Write-Host "      Note: litellm is pinned to 1.82.6 (security requirement)"
& $PipExe install --upgrade pip --quiet
& $PipExe install -r "$ProjectRoot\requirements.txt" -r "$ProjectRoot\requirements-windows.txt"
& $PipExe install -e "$ProjectRoot"
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] pip install failed. Check the output above." -ForegroundColor Red
    exit 1
}

# Optional GPU/CUDA libraries setup
Write-Host ""
$installGpu = Read-Host "      Would you like to install GPU acceleration (CUDA) libraries? (Y/N) [Default: N]"
if ($installGpu -match "^[yY](es)?$") {
    Write-Host "       Installing CUDA/cuDNN libraries (~1.3GB)..." -ForegroundColor Yellow
    & $PipExe install -r "$ProjectRoot\requirements-cuda.txt"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "       [WARN] CUDA library installation failed. The system will fall back to CPU mode." -ForegroundColor Yellow
    } else {
        Write-Host "       [PASS] CUDA libraries installed successfully." -ForegroundColor Green
    }
} else {
    Write-Host "       Skipping GPU libraries (running in CPU mode)." -ForegroundColor Gray
}

Write-Host "[PASS] Dependencies installed." -ForegroundColor Green


# --- Step 4: Check/install Ollama ---
Write-Host "[4/8] Checking Ollama..." -ForegroundColor Yellow
$ollamaFound = $null
try {
    $ollamaFound = Get-Command ollama -ErrorAction SilentlyContinue
} catch {}

if ($null -eq $ollamaFound) {
    Write-Host "       Ollama not found. Downloading installer..."
    Write-Host "       Minimum hardware for standard tier:"
    Write-Host "         RAM: 8GB+ (12GB recommended)"
    Write-Host "         Disk: 8GB+ free (for both Gemma 4 E2B and E4B)"
    $ollamaInstaller = Join-Path $env:TEMP "OllamaSetup.exe"
    Invoke-WebRequest -Uri "https://ollama.com/download/OllamaSetup.exe" -OutFile $ollamaInstaller
    Start-Process -FilePath $ollamaInstaller -Wait
    Write-Host "[PASS] Ollama installed." -ForegroundColor Green
} else {
    Write-Host "[PASS] Ollama found: $(ollama --version 2>&1)" -ForegroundColor Green
}

# --- Step 5: Pull models ---
Write-Host "[5/8] Ensuring Ollama service is running..." -ForegroundColor Yellow
$ollamaRunning = $false
for ($i = 0; $i -lt 10; $i++) {
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:11434/api/tags" -UseBasicParsing -TimeoutSec 2
        if ($resp.StatusCode -eq 200) {
            $ollamaRunning = $true
            break
        }
    } catch {}
    Write-Host "       Ollama service not responding. Spawning 'ollama serve' in background..." -ForegroundColor Gray
    Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden
    Start-Sleep -Seconds 3
}

if (-not $ollamaRunning) {
    Write-Host "[WARN] Ollama service did not respond. Pulling models might fail." -ForegroundColor Yellow
} else {
    Write-Host "[PASS] Ollama service is active." -ForegroundColor Green
}

Write-Host ""
Write-Host "[5/8] Pulling Gemma 4 models (this downloads ~7.5GB, may take several minutes)..." -ForegroundColor Yellow
Write-Host "      Disk space required: ~8GB"
Write-Host "      RAM required during inference: ~8GB (standard tier)"
Write-Host ""

# Detect available RAM
$availableRAMGB = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1)
Write-Host "      Detected RAM: ${availableRAMGB}GB"

if ($availableRAMGB -lt 8) {
    Write-Host "      [WARN] Less than 8GB RAM detected. Using constrained tier (E2B only)." -ForegroundColor Yellow
    ollama pull gemma4:e2b
    # Update config to use constrained tier
    $Tier = "constrained"
} else {
    ollama pull gemma4:e2b
    if ($LASTEXITCODE -ne 0) { Write-Host "[FAIL] Could not pull gemma4:e2b" -ForegroundColor Red; exit 1 }
    ollama pull gemma4:e4b
    if ($LASTEXITCODE -ne 0) { Write-Host "[FAIL] Could not pull gemma4:e4b" -ForegroundColor Red; exit 1 }
    $Tier = "standard"
}
Write-Host "[PASS] Models ready (tier: $Tier)." -ForegroundColor Green

# --- Step 5.5: Download Kokoro & Wake Word Model Files ---
Write-Host "[5.5/8] Downloading Kokoro & Wake Word model files..." -ForegroundColor Yellow
    $TtsDir = Join-Path $ProjectRoot "models\tts"
    $WakeWordsDir = Join-Path $ProjectRoot "assets\wakewords"
    if (-not (Test-Path $TtsDir)) { New-Item -ItemType Directory -Force -Path $TtsDir | Out-Null }
    if (-not (Test-Path $WakeWordsDir)) { New-Item -ItemType Directory -Force -Path $WakeWordsDir | Out-Null }

    $KokoroModelPath = Join-Path $TtsDir "kokoro-v1.0.onnx"
    $KokoroVoicesPath = Join-Path $TtsDir "voices-v1.0.bin"
    $WakeWordModelPath = Join-Path $WakeWordsDir "hey_jarvis_v0.1.onnx"

if (-not (Test-Path $KokoroModelPath)) {
    Write-Host "       Downloading kokoro-v1.0.onnx (340MB)..."
    Invoke-WebRequest -Uri "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx" -OutFile $KokoroModelPath
} else {
    Write-Host "       kokoro-v1.0.onnx already exists."
}

if (-not (Test-Path $KokoroVoicesPath)) {
    Write-Host "       Downloading voices-v1.0.bin (20MB)..."
    Invoke-WebRequest -Uri "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin" -OutFile $KokoroVoicesPath
} else {
    Write-Host "       voices-v1.0.bin already exists."
}

if (-not (Test-Path $WakeWordModelPath)) {
    Write-Host "       Downloading hey_jarvis_v0.1.onnx (4MB)..."
    Invoke-WebRequest -Uri "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/hey_jarvis_v0.1.onnx" -OutFile $WakeWordModelPath
} else {
    Write-Host "       hey_jarvis_v0.1.onnx already exists."
}



# --- Step 6: Configure sandbox root ---
Write-Host "[6/8] Configure sandbox directory..." -ForegroundColor Yellow
Write-Host "      This is the folder EchoLocate can access. Files outside this"
Write-Host "      folder are completely invisible to the agent."
Write-Host ""
$defaultSandbox = Join-Path $HOME "EchoLocateSandbox"
$sandboxInput = Read-Host "      Enter sandbox directory path (default: $defaultSandbox)"
if ([string]::IsNullOrWhiteSpace($sandboxInput)) {
    $SandboxRoot = $defaultSandbox
} else {
    $SandboxRoot = $sandboxInput
}

if (-not (Test-Path $SandboxRoot)) {
    New-Item -ItemType Directory -Path $SandboxRoot -Force | Out-Null
    Write-Host "      Created sandbox directory: $SandboxRoot"
}
Write-Host "[PASS] Sandbox root: $SandboxRoot" -ForegroundColor Green

# --- Step 7: Write config ---
Write-Host "[7/8] Writing configuration..." -ForegroundColor Yellow
$ConfigPath = Join-Path $ProjectRoot "config\default_config.yaml"
$ConfigContent = Get-Content $ConfigPath -Raw
$ConfigContent = $ConfigContent -replace 'sandbox_root: ""', "sandbox_root: `"$($SandboxRoot.Replace('\', '/'))`""
$ConfigContent = $ConfigContent -replace 'active_tier: "standard"', "active_tier: `"$Tier`""
Set-Content -Path $ConfigPath -Value $ConfigContent -Encoding UTF8
Write-Host "[PASS] Configuration written to: $ConfigPath" -ForegroundColor Green

# Also update models.yaml active_tier
$ModelsPath = Join-Path $ProjectRoot "config\models.yaml"
$ModelsContent = Get-Content $ModelsPath -Raw
$ModelsContent = $ModelsContent -replace 'active_tier: "standard"', "active_tier: `"$Tier`""
Set-Content -Path $ModelsPath -Value $ModelsContent -Encoding UTF8

# --- Step 7.5: Add to PATH ---
Write-Host "[8/9] Adding EchoLocate to User PATH..." -ForegroundColor Yellow
$UserPath = [Environment]::GetEnvironmentVariable("PATH", "User")
$ScriptsPath = (Join-Path $ProjectRoot ".venv\Scripts")
if ($UserPath -notmatch [regex]::Escape($ScriptsPath)) {
    $NewPath = $UserPath + ";" + $ScriptsPath
    [Environment]::SetEnvironmentVariable("PATH", $NewPath, "User")
    Write-Host "[PASS] Added $ScriptsPath to User PATH." -ForegroundColor Green
    Write-Host "       (You may need to restart your terminal for this to take effect.)" -ForegroundColor Yellow
} else {
    Write-Host "[PASS] EchoLocate is already in your PATH." -ForegroundColor Green
}

# --- Step 9: Smoke test ---
Write-Host "[9/9] Running startup smoke test..." -ForegroundColor Yellow
& $PythonExe -c "
from echolocate.mcp_server.sandbox import resolve_and_check, IS_WINDOWS
import sys, pathlib
platform = 'Windows' if IS_WINDOWS else 'Unix'
print(f'[PASS] Sandbox module loaded. Platform: {platform}')
root = pathlib.Path(r'$SandboxRoot')
if root.exists():
    print(f'[PASS] Sandbox root accessible.')
else:
    print('[FAIL] Sandbox root not accessible.')
    sys.exit(1)
"
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] Smoke test failed." -ForegroundColor Red
    exit 1
}

# --- Done ---
Write-Host ""
Write-Host "================================================" -ForegroundColor Green
Write-Host "  EchoLocate installation complete!" -ForegroundColor Green
Write-Host "================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  To start EchoLocate in the background:
    echolocate start

  To stop EchoLocate:
    echolocate stop

  To update configuration:
    echolocate config set sandbox_root "C:\New\Path"

  Note: You may need to open a NEW terminal window 
  for the 'echolocate' command to be recognized.

  To run tests:
    pytest tests\ -v"
Write-Host ""
Write-Host "  Sandbox root: $SandboxRoot"
Write-Host "  Audit log:    $HOME\.echolocate\audit.log"
Write-Host ""
Write-Host "  Hold SPACE to speak, ESC to stop TTS playback."
Write-Host ""
