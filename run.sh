#!/bin/bash
set -e

cd "$(dirname "$0")"

# On Ubuntu/Debian, python3-venv and ensurepip must be installed separately
if ! python3 -c "import ensurepip" 2>/dev/null; then
    echo "python3-venv not found. Installing..."
    PYTHON_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    sudo apt-get update -q 2>/dev/null || true
    sudo apt-get install -y "python${PYTHON_VER}-venv" python3-pip
fi

# Determine venv location - use home dir if current fs doesn't support symlinks (NTFS/exFAT)
VENV_DIR="venv"
if [ ! -f "venv/bin/activate" ]; then
    echo "Creating virtual environment..."
    rm -rf venv
    python3 -m venv venv 2>/dev/null
    if [ ! -f "venv/bin/activate" ]; then
        # Symlink failed (NTFS/exFAT mount) - create venv in home directory instead
        VENV_DIR="$HOME/.local/share/ui-autotests-venv"
        echo "Note: filesystem does not support symlinks, using $VENV_DIR"
        rm -rf "$VENV_DIR"
        python3 -m venv "$VENV_DIR"
    fi
else
    # Check if existing venv is in home dir
    if [ -f "$HOME/.local/share/ui-autotests-venv/bin/activate" ]; then
        VENV_DIR="$HOME/.local/share/ui-autotests-venv"
    fi
fi

source "$VENV_DIR/bin/activate"

echo "Installing dependencies..."
pip install -r requirements.txt -q

echo "Installing Playwright browsers..."
playwright install chromium

# ── Allure CLI ────────────────────────────────────────────────────────────────
if ! which allure &>/dev/null; then
    OS="$(uname -s)"
    if [ "$OS" = "Linux" ]; then
        echo "Allure CLI not found. Installing locally (~50MB)..."
        ALLURE_VERSION="2.27.0"
        ALLURE_DIR="$HOME/.local/share/allure-$ALLURE_VERSION"
        ALLURE_BIN="$HOME/.local/bin"
        mkdir -p "$ALLURE_BIN"
        TMP_TGZ="/tmp/allure-$ALLURE_VERSION.tgz"
        DOWNLOAD_URL="https://github.com/allure-framework/allure2/releases/download/$ALLURE_VERSION/allure-$ALLURE_VERSION.tgz"
        if curl -fsSL "$DOWNLOAD_URL" -o "$TMP_TGZ" 2>/dev/null; then
            tar -xzf "$TMP_TGZ" -C "$HOME/.local/share/" 2>/dev/null
            rm -f "$TMP_TGZ"
            ln -sf "$ALLURE_DIR/bin/allure" "$ALLURE_BIN/allure"
            export PATH="$ALLURE_BIN:$PATH"
            echo "Allure installed to $ALLURE_DIR"
        else
            echo "WARNING: Could not download Allure. Install manually: https://github.com/allure-framework/allure2/releases"
        fi
    elif [ "$OS" = "Darwin" ]; then
        if which brew &>/dev/null; then
            echo "Allure CLI not found. Installing via Homebrew..."
            brew install allure
        else
            echo "WARNING: Allure CLI not found. Install with: brew install allure"
        fi
    fi
else
    echo "Allure CLI found: $(which allure)"
fi

# Find a free port starting from 5001 (5000 is taken by AirPlay on macOS)
find_free_port() {
    local port=${1:-5001}
    while lsof -i TCP:"$port" &>/dev/null; do
        port=$((port + 1))
    done
    echo "$port"
}

PORT=${PORT:-$(find_free_port 5001)}
echo ""
echo "Starting UI Autotest Generator at http://localhost:$PORT"
echo ""

# Open browser after a short delay (wait for Flask to start)
OS="$(uname -s)"
if [ "$OS" = "Darwin" ]; then
    (sleep 1.5 && open "http://localhost:$PORT") &
elif [ "$OS" = "Linux" ]; then
    (sleep 1.5 && xdg-open "http://localhost:$PORT" 2>/dev/null || true) &
fi

# Ensure ~/.local/bin is in PATH (for locally installed allure)
export PATH="$HOME/.local/bin:$PATH"

PORT=$PORT python3 app.py
