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

# ── 5. Swift toolchain + native helper ───────────────────────────────────────
divider
info "Checking Swift toolchain (for the native system-audio helper)..."
if ! xcode-select -p &>/dev/null; then
  warn "Xcode Command Line Tools are not installed. Install them with:"
  warn "  xcode-select --install"
  error "Re-run this script after the install finishes."
fi
if ! command -v swift &>/dev/null; then
  error "Swift compiler not found even though Xcode CLT is installed. Re-install with xcode-select --install."
fi
info "Swift toolchain found: $(swift --version | head -1)"

info "Building native system-audio helper (pp-system-audio)..."
(
  cd native/macos/pp-system-audio
  swift build -c release --arch arm64
)
info "Native helper built at native/macos/pp-system-audio/.build/release/pp-system-audio"
info "System audio capture: macOS will ask permission the first time you record. No driver install needed."

# ── Done ──────────────────────────────────────────────────────────────────────
divider
echo ""
echo -e "  ${GREEN}Setup complete!${NC}"
echo ""
echo "  Transcription and note generation use Deepgram and Gemini (cloud APIs)."
echo "  Add your API keys in Settings → API Keys the first time you launch."
echo ""
echo "  To start Passive Perception:"
echo "    source .venv/bin/activate"
echo "    python run.py"
echo ""
