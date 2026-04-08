#!/bin/bash
set -e

export INSTALL_DIR="/workspace"
export SCRIPT_DIR="/"
export LOG_DIR="${INSTALL_DIR}/logs"
export VENV_DIR="${INSTALL_DIR}/venv"
export VENV_PYTHON="${VENV_DIR}/bin/python"

mkdir -p "$LOG_DIR"
echo "🚀 Starting RunPod Native Boot sequence..."

# Install system build tools (ADDED zstd for Ollama)
echo "🛠️ Installing system build tools..."
apt-get update -y && apt-get install -y build-essential ninja-build libgl1 libglib2.0-0 curl psmisc zstd

# ----------------------------------------------------------------------------
# 1. GPU DETECTION
# ----------------------------------------------------------------------------
GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -n1 || echo "UNKNOWN")
echo "Detected GPU: $GPU_NAME"

if [[ "$GPU_NAME" == *"5090"* ]]; then
    export GPU_CLASS="BLACKWELL"
elif [[ "$GPU_NAME" == *"4090"* || "$GPU_NAME" == *"6000"* ]]; then
    export GPU_CLASS="ADA"
else
    export GPU_CLASS="OTHER"
fi
echo "GPU Class: $GPU_CLASS"

# ----------------------------------------------------------------------------
# 2. VENV & UV SETUP
# ----------------------------------------------------------------------------
if ! command -v uv &> /dev/null; then
    echo "📦 Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="/root/.local/bin" sh
fi
export PATH="/root/.local/bin:$PATH"

if [ ! -d "$VENV_DIR" ]; then
    echo "🌱 Creating persistent virtual environment in $VENV_DIR..."
    uv venv "$VENV_DIR"
fi

uv pip install --python "$VENV_PYTHON" setuptools wheel

# ----------------------------------------------------------------------------
# 3. DYNAMIC PYTORCH INSTALLATION
# ----------------------------------------------------------------------------
if ! "$VENV_PYTHON" -c "import torch" &> /dev/null; then
    if [[ "$GPU_CLASS" == "BLACKWELL" ]]; then
        echo "⚙️ Installing PyTorch 2.8 Nightly (SDPA Native) for RTX 5090..."
        uv pip install --python "$VENV_PYTHON" --pre torch torchvision torchaudio --index-url https://download.pytorch.org/whl/nightly/cu128
    else
        echo "⚙️ Installing Stable PyTorch 2.5.1 + Xformers for Ampere/Ada..."
        uv pip install --python "$VENV_PYTHON" torch==2.5.1 torchvision==0.20.1 torchaudio==2.5.1 xformers==0.0.28.post3 --index-url https://download.pytorch.org/whl/cu124
    fi

    if [[ "$GPU_CLASS" == "ADA" ]]; then
        echo "⚙️ Attempting SageAttention compilation for ADA..."
        export TORCH_CUDA_ARCH_LIST="8.9+PTX"
        uv pip install --python "$VENV_PYTHON" --no-build-isolation git+https://github.com/thu-ml/SageAttention.git || echo "⚠️ SageAttention failed. Falling back to native SDPA."
    fi
fi

# ----------------------------------------------------------------------------
# 4. VSCODE & FILEBROWSER SETUP
# ----------------------------------------------------------------------------
# Install VSCode Server
if ! command -v code-server &> /dev/null; then
    echo "💻 Installing VS Code Server..."
    curl -fsSL https://code-server.dev/install.sh | sh
fi

# Kill RunPod's default JupyterLab on 8888 so VSCode can take over
fuser -k 8888/tcp || true
echo "▶️ Starting VS Code Server on Port 8888..."
nohup code-server --bind-addr 0.0.0.0:8888 --auth none /workspace > "$LOG_DIR/vscode.log" 2>&1 &


if ! command -v filebrowser &> /dev/null; then
    echo "📂 Installing Filebrowser..."
    curl -fsSL https://raw.githubusercontent.com/filebrowser/get/master/get.sh | bash
fi

export FB_DB="$INSTALL_DIR/filebrowser.db"
ADMIN_PASS=${ACCESS_PASSWORD:-"runpod_default"}

if [ ! -f "$FB_DB" ]; then
    filebrowser config init -d "$FB_DB"
    filebrowser config set -a 0.0.0.0 -p 8083 -r "$INSTALL_DIR" -d "$FB_DB"
    filebrowser users add admin "$ADMIN_PASS" --perm.admin -d "$FB_DB"
else
    filebrowser users update admin --password "$ADMIN_PASS" -d "$FB_DB" || true
fi

echo "▶️ Starting Filebrowser on Port 8083..."
nohup filebrowser -d "$FB_DB" > "$LOG_DIR/filebrowser.log" 2>&1 &

# ----------------------------------------------------------------------------
# 5. START SIDECAR
# ----------------------------------------------------------------------------
echo "⚙️ Hydrating Sidecar dependencies..."
uv pip install --python "$VENV_PYTHON" gradio huggingface_hub requests pillow psutil

echo "🛰️ Starting Universal Sidecar on Port 8080..."
nohup "$VENV_PYTHON" "$SCRIPT_DIR/sidecar_app.py" > "$LOG_DIR/sidecar.log" 2>&1 &

echo "✅ Boot complete!"
tail -f /dev/null