#!/usr/bin/env bash
# EchoLocate Installer — macOS / Linux
# Usage: ./install.sh
# Requires: Python 3.10+, internet connection (for model download)

set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

echo -e "${CYAN}================================================${NC}"
echo -e "${CYAN}  EchoLocate Installer (macOS / Linux)${NC}"
echo -e "${CYAN}================================================${NC}"
echo ""

# --- Step 1: Check Python 3.10+ ---
echo -e "${YELLOW}[1/8] Checking Python version...${NC}"
PYTHON_CMD=""
for cmd in python3.12 python3.11 python3.10 python3 python; do
    if command -v "$cmd" &>/dev/null; then
        VERSION=$("$cmd" --version 2>&1 | grep -oP '\d+\.\d+' | head -1)
        MAJOR=$(echo "$VERSION" | cut -d. -f1)
        MINOR=$(echo "$VERSION" | cut -d. -f2)
        if [ "$MAJOR" -ge 3 ] && [ "$MINOR" -ge 10 ]; then
            PYTHON_CMD="$cmd"
            echo -e "${GREEN}[PASS] Found $cmd ($VERSION)${NC}"
            break
        fi
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    echo -e "${RED}[FAIL] Python 3.10+ not found.${NC}"
    echo "       macOS: brew install python@3.11"
    echo "       Ubuntu: sudo apt install python3.11"
    exit 1
fi

# --- Step 2: Create virtualenv ---
echo -e "${YELLOW}[2/8] Creating virtual environment...${NC}"
VENV_PATH="$PROJECT_ROOT/.venv"
if [ -d "$VENV_PATH" ]; then
    echo "       Reusing existing virtual environment."
else
    "$PYTHON_CMD" -m venv "$VENV_PATH"
    echo -e "${GREEN}[PASS] Virtual environment created.${NC}"
fi

PIP="$VENV_PATH/bin/pip"
PYTHON="$VENV_PATH/bin/python"

# --- Step 3: Install dependencies ---
echo -e "${YELLOW}[3/8] Installing Python dependencies...${NC}"
echo "      Note: litellm is pinned to 1.82.6 (security requirement)"
"$PIP" install --upgrade pip --quiet
"$PIP" install -r "$PROJECT_ROOT/requirements.txt"
"$PIP" install -e "$PROJECT_ROOT"
echo -e "${GREEN}[PASS] Dependencies installed.${NC}"

# --- Step 4: Check/install Ollama ---
echo -e "${YELLOW}[4/8] Checking Ollama...${NC}"
if ! command -v ollama &>/dev/null; then
    echo "       Ollama not found. Installing..."
    if [[ "$OSTYPE" == "darwin"* ]]; then
        if command -v brew &>/dev/null; then
            brew install ollama
        else
            curl -fsSL https://ollama.com/install.sh | sh
        fi
    else
        curl -fsSL https://ollama.com/install.sh | sh
    fi
    echo -e "${GREEN}[PASS] Ollama installed.${NC}"
else
    echo -e "${GREEN}[PASS] Ollama found: $(ollama --version 2>&1)${NC}"
fi

# Start Ollama if not running
echo "       Ensuring Ollama service is active..."
OLLAMA_RUNNING=false
for i in {1..10}; do
    if curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:11434/api/tags | grep -q "200"; then
        OLLAMA_RUNNING=true
        break
    fi
    echo "       Ollama offline. Spawning 'ollama serve' in background..."
    ollama serve &>/dev/null &
    sleep 3
done

if [ "$OLLAMA_RUNNING" = false ]; then
    echo -e "${YELLOW}[WARN] Ollama did not respond. Pulling models might fail.${NC}"
else
    echo -e "${GREEN}[PASS] Ollama service is active.${NC}"
fi

# --- Step 5: Pull models ---
echo -e "${YELLOW}[5/8] Pulling Gemma 4 models...${NC}"
echo "      Disk required: ~8GB | RAM required: ~8GB (standard tier)"

# Detect available RAM (cross-platform)
if [[ "$OSTYPE" == "darwin"* ]]; then
    RAM_BYTES=$(sysctl -n hw.memsize 2>/dev/null || echo "0")
else
    RAM_BYTES=$(grep MemTotal /proc/meminfo | awk '{print $2 * 1024}' 2>/dev/null || echo "0")
fi
RAM_GB=$(( RAM_BYTES / 1024 / 1024 / 1024 ))
echo "      Detected RAM: ${RAM_GB}GB"

TIER="standard"
if [ "$RAM_GB" -lt 8 ]; then
    echo "      [WARN] Less than 8GB RAM. Using constrained tier (E2B only)."
    ollama pull gemma4:e2b
    TIER="constrained"
else
    ollama pull gemma4:e2b
    ollama pull gemma4:e4b
fi
echo -e "${GREEN}[PASS] Models ready (tier: $TIER).${NC}"

# --- Step 5.5: Download Kokoro & Wake Word Model Files ---
echo -e "${YELLOW}[5.5/8] Downloading Kokoro & Wake Word model files...${NC}"
    TTS_DIR="$PROJECT_ROOT/models/tts"
    WAKEWORDS_DIR="$PROJECT_ROOT/assets/wakewords"
    mkdir -p "$TTS_DIR" "$WAKEWORDS_DIR"

    KOKORO_MODEL="$TTS_DIR/kokoro-v1.0.onnx"
    KOKORO_VOICES="$TTS_DIR/voices-v1.0.bin"
    WAKE_WORD_MODEL="$WAKEWORDS_DIR/hey_jarvis_v0.1.onnx"

if [ ! -f "$KOKORO_MODEL" ]; then
    echo "       Downloading kokoro-v1.0.onnx (340MB)..."
    curl -L -o "$KOKORO_MODEL" "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx"
else
    echo "       kokoro-v1.0.onnx already exists."
fi

if [ ! -f "$KOKORO_VOICES" ]; then
    echo "       Downloading voices-v1.0.bin (20MB)..."
    curl -L -o "$KOKORO_VOICES" "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"
else
    echo "       voices-v1.0.bin already exists."
fi

if [ ! -f "$WAKE_WORD_MODEL" ]; then
    echo "       Downloading hey_jarvis_v0.1.onnx (4MB)..."
    curl -L -o "$WAKE_WORD_MODEL" "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/hey_jarvis_v0.1.onnx"
else
    echo "       hey_jarvis_v0.1.onnx already exists."
fi



# --- Step 6: Configure sandbox root ---
echo -e "${YELLOW}[6/8] Configure sandbox directory...${NC}"
DEFAULT_SANDBOX="$HOME/EchoLocateSandbox"
read -rp "      Sandbox directory (default: $DEFAULT_SANDBOX): " SANDBOX_INPUT
SANDBOX_ROOT="${SANDBOX_INPUT:-$DEFAULT_SANDBOX}"
mkdir -p "$SANDBOX_ROOT"
echo -e "${GREEN}[PASS] Sandbox root: $SANDBOX_ROOT${NC}"

# --- Step 7: Write config ---
echo -e "${YELLOW}[7/8] Writing configuration...${NC}"
CONFIG_PATH="$PROJECT_ROOT/config/default_config.yaml"
sed -i.bak "s|sandbox_root: \"\"|sandbox_root: \"$SANDBOX_ROOT\"|g" "$CONFIG_PATH"
sed -i.bak "s|active_tier: \"standard\"|active_tier: \"$TIER\"|g" "$CONFIG_PATH"
rm -f "$CONFIG_PATH.bak"
echo -e "${GREEN}[PASS] Configuration written.${NC}"

# --- Step 7.5: Add to PATH ---
echo -e "${YELLOW}[8/9] Adding EchoLocate to User PATH...${NC}"
BIN_PATH="$PROJECT_ROOT/.venv/bin"
BASHRC="$HOME/.bashrc"
ZSHRC="$HOME/.zshrc"

add_to_path() {
    local rc_file=$1
    if [ -f "$rc_file" ]; then
        if ! grep -q "$BIN_PATH" "$rc_file"; then
            echo "export PATH=\"$BIN_PATH:\$PATH\"" >> "$rc_file"
            echo "       Added to $rc_file"
        fi
    fi
}

add_to_path "$BASHRC"
add_to_path "$ZSHRC"

# Also try to make it available in current script execution context
export PATH="$BIN_PATH:$PATH"
echo -e "${GREEN}[PASS] Added $BIN_PATH to PATH.${NC}"
echo -e "${YELLOW}       (You may need to restart your terminal or run 'source ~/.bashrc')${NC}"

# --- Step 9: Smoke test ---
echo -e "${YELLOW}[9/9] Running startup smoke test...${NC}"
"$PYTHON" -c "
from echolocate.mcp_server.sandbox import resolve_and_check, IS_WINDOWS
import pathlib, sys
platform = 'Windows' if IS_WINDOWS else 'Unix'
print(f'[PASS] Sandbox module. Platform branch: {platform}')
root = pathlib.Path('$SANDBOX_ROOT')
if root.exists():
    print('[PASS] Sandbox root accessible.')
else:
    print('[FAIL] Sandbox root not accessible.')
    sys.exit(1)
"

echo ""
echo -e "${GREEN}================================================${NC}"
echo -e "${GREEN}  EchoLocate installation complete!${NC}"
echo -e "${GREEN}================================================${NC}"
echo ""
echo "  To start EchoLocate in the background:
    echolocate start

  To stop EchoLocate:
    echolocate stop

  To update configuration:
    echolocate config set sandbox_root \"/new/path\"

  Note: You may need to run 'source ~/.bashrc' or 
  open a NEW terminal for the 'echolocate' command.

  To run tests:
    pytest tests/ -v"
echo ""
echo "  Sandbox: $SANDBOX_ROOT"
echo "  Audit log: $HOME/.echolocate/audit.log"
echo ""
echo "  Hold SPACE to speak, ESC to stop TTS playback."
echo ""
