#!/usr/bin/env bash
set -e

PYTHON_VERSION="3.11.9"
VENV_DIR=".venv"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()    { echo -e "${GREEN}[setup]${NC} $1"; }
warn()    { echo -e "${YELLOW}[warn]${NC}  $1"; }
error()   { echo -e "${RED}[error]${NC} $1"; exit 1; }
divider() { echo -e "\n${YELLOW}────────────────────────────────────────${NC}"; }

echo ""
echo "  Passive Perception — Setup"
echo "  D&D Session Scribe for macOS"
echo ""

# ── 1. Homebrew ───────────────────────────────────────────────────────────────
divider
info "Checking Homebrew..."
if ! command -v brew &>/dev/null; then
  error "Homebrew is not installed. Install it from https://brew.sh then re-run this script."
fi
info "Homebrew found: $(brew --version | head -1)"

# ── 2. pyenv + Python 3.11 ───────────────────────────────────────────────────
divider
info "Checking pyenv..."
if ! command -v pyenv &>/dev/null; then
  info "Installing pyenv via Homebrew..."
  brew install pyenv
  # Add pyenv to shell profile
  SHELL_PROFILE=""
  if [ -f "$HOME/.zshrc" ]; then SHELL_PROFILE="$HOME/.zshrc"
  elif [ -f "$HOME/.bash_profile" ]; then SHELL_PROFILE="$HOME/.bash_profile"
  fi
  if [ -n "$SHELL_PROFILE" ]; then
    echo '' >> "$SHELL_PROFILE"
    echo '# pyenv' >> "$SHELL_PROFILE"
    echo 'export PYENV_ROOT="$HOME/.pyenv"' >> "$SHELL_PROFILE"
    echo 'export PATH="$PYENV_ROOT/bin:$PATH"' >> "$SHELL_PROFILE"
    echo 'eval "$(pyenv init -)"' >> "$SHELL_PROFILE"
  fi
  export PYENV_ROOT="$HOME/.pyenv"
  export PATH="$PYENV_ROOT/bin:$PATH"
  eval "$(pyenv init -)"
fi
info "pyenv found: $(pyenv --version)"

info "Installing Python $PYTHON_VERSION (this may take a few minutes)..."
if ! pyenv versions | grep -q "$PYTHON_VERSION"; then
  pyenv install "$PYTHON_VERSION"
else
  info "Python $PYTHON_VERSION already installed."
fi
pyenv local "$PYTHON_VERSION"
PYTHON_BIN="$(pyenv prefix "$PYTHON_VERSION")/bin/python3"
info "Using Python: $($PYTHON_BIN --version)"

# ── 3. Virtual environment ───────────────────────────────────────────────────
divider
info "Creating virtual environment in $VENV_DIR/..."
if [ ! -d "$VENV_DIR" ]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"
pip install --upgrade pip --quiet
info "Virtual environment ready."

# ── 4. Python dependencies ───────────────────────────────────────────────────
divider
info "Installing Python dependencies..."
pip install -r requirements.txt
info "Python dependencies installed."

# ── 5. BlackHole ─────────────────────────────────────────────────────────────
divider
info "Checking for BlackHole 2ch audio driver..."
if ls /Library/Audio/Plug-Ins/HAL/ 2>/dev/null | grep -qi "blackhole"; then
  info "BlackHole is already installed."
else
  info "Installing BlackHole 2ch via Homebrew..."
  brew install blackhole-2ch
  echo ""
  warn "BlackHole installed! You now need to create a Multi-Output Device so you"
  warn "can hear Discord AND have Passive Perception capture it simultaneously."
  echo ""
  echo "  Steps (do this once):"
  echo "  1. Open  Audio MIDI Setup  (Applications > Utilities > Audio MIDI Setup)"
  echo "  2. Click the '+' button at bottom-left → 'Create Multi-Output Device'"
  echo "  3. Check both your headphones/speakers AND 'BlackHole 2ch'"
  echo "  4. Right-click the new Multi-Output Device → 'Use This Device For Sound Output'"
  echo "  5. In Discord: Settings → Voice & Video → Output Device → Multi-Output Device"
  echo ""
  read -p "Press Enter once you've completed the Multi-Output Device setup..."
fi

# ── 6. MLX LLM model ────────────────────────────────────────────────────────
divider
info "The LLM model will be downloaded automatically on first use."
info "Model: mlx-community/Llama-3.2-3B-Instruct-4bit (~2 GB)"
info "This requires Apple Silicon (M1/M2/M3/M4/M5)."

# ── 7. HuggingFace token ─────────────────────────────────────────────────────
divider
info "HuggingFace token setup for speaker diarization..."
echo ""
if [ -f ".env" ] && grep -q "HUGGINGFACE_TOKEN" .env; then
  info "HuggingFace token already configured in .env"
else
  echo "  pyannote-audio requires a HuggingFace token to download the diarization model."
  echo "  1. Create a token at: https://huggingface.co/settings/tokens (Read access)"
  echo "  2. Accept the model license at: https://huggingface.co/pyannote/speaker-diarization-3.1"
  echo ""
  read -p "  Paste your HuggingFace token here: " HF_TOKEN
  if [ -z "$HF_TOKEN" ]; then
    warn "No token provided. Speaker diarization will be disabled until you add"
    warn "HUGGINGFACE_TOKEN=your_token_here to a .env file in this directory."
    echo "HUGGINGFACE_TOKEN=" > .env
  else
    echo "HUGGINGFACE_TOKEN=$HF_TOKEN" > .env
    info "Token saved to .env"
  fi
fi

# ── Done ──────────────────────────────────────────────────────────────────────
divider
echo ""
echo -e "  ${GREEN}Setup complete!${NC}"
echo ""
echo "  To start Passive Perception:"
echo "    source .venv/bin/activate"
echo "    python run.py"
echo ""
echo "  This will open http://localhost:8000 in your browser."
echo ""
