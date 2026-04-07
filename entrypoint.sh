#!/bin/bash
set -e

export INSTALL_DIR="/workspace"
export SCRIPT_DIR="/"
export LOG_DIR="${INSTALL_DIR}/logs"
export VENV_DIR="${INSTALL_DIR}/venv"
export VENV_PYTHON="${VENV_DIR}/bin/python"

mkdir -p "$LOG_DIR"

echo "🚀 Starting RunPod Native Boot sequence..."

# 1. Install 'uv' if it doesn't exist (blazing fast python package manager)
if ! command -v uv &> /dev/null; then
    echo "📦 Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="/root/.local/bin" sh
fi
export PATH="/root/.local/bin:$PATH"

# 2. Check for and create persistent Virtual Environment
if [ ! -d "$VENV_DIR" ]; then
    echo "🌱 Creating persistent virtual environment in $VENV_DIR..."
    uv venv "$VENV_DIR"
fi

# 3. Install Sidecar core dependencies
echo "⚙️ Hydrating Sidecar dependencies..."
uv pip install --python "$VENV_PYTHON" gradio huggingface_hub requests pillow psutil

# 4. Start the Universal Sidecar App
echo "🛰️ Starting Universal Sidecar on Port 8080..."
nohup "$VENV_PYTHON" "$SCRIPT_DIR/sidecar_app.py" > "$LOG_DIR/sidecar.log" 2>&1 &

echo "✅ Boot complete! Access the Sidecar on Port 8080."

# Keep container alive so RunPod doesn't exit
tail -f /dev/null
