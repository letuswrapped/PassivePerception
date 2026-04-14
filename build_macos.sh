#!/usr/bin/env bash
set -e

# ── Passive Perception — macOS App Builder ──────────────────────────────────
# Creates PassivePerception.app and packages it into a .dmg for distribution.
# Usage: ./build_macos.sh

APP_NAME="Passive Perception"
BUNDLE_ID="com.passiveperception.app"
VERSION="1.0.0"
BUILD_DIR="build"
APP_DIR="$BUILD_DIR/${APP_NAME}.app"
DMG_NAME="PassivePerception-${VERSION}.dmg"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info() { echo -e "${GREEN}[build]${NC} $1"; }
warn() { echo -e "${YELLOW}[build]${NC} $1"; }

echo ""
echo "  Building ${APP_NAME}.app"
echo ""

# ── Clean previous build ────────────────────────────────────────────────────
rm -rf "$BUILD_DIR"
mkdir -p "$APP_DIR/Contents/MacOS"
mkdir -p "$APP_DIR/Contents/Resources/app"

# ── Copy application source ────────────────────────────────────────────────
info "Copying application files..."
cp -R src "$APP_DIR/Contents/Resources/app/"
cp run.py "$APP_DIR/Contents/Resources/app/"
cp config.yaml "$APP_DIR/Contents/Resources/app/"
cp requirements.txt "$APP_DIR/Contents/Resources/app/"
cp .env.example "$APP_DIR/Contents/Resources/app/"
cp setup.sh "$APP_DIR/Contents/Resources/app/"

# ── Copy icon ───────────────────────────────────────────────────────────────
if [ -f "src/ui/static/icon.icns" ]; then
  cp src/ui/static/icon.icns "$APP_DIR/Contents/Resources/AppIcon.icns"
  info "Icon copied."
else
  warn "icon.icns not found — app will use default icon."
fi

# ── Info.plist ──────────────────────────────────────────────────────────────
info "Writing Info.plist..."
cat > "$APP_DIR/Contents/Info.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>${APP_NAME}</string>
    <key>CFBundleDisplayName</key>
    <string>${APP_NAME}</string>
    <key>CFBundleIdentifier</key>
    <string>${BUNDLE_ID}</string>
    <key>CFBundleVersion</key>
    <string>${VERSION}</string>
    <key>CFBundleShortVersionString</key>
    <string>${VERSION}</string>
    <key>CFBundleExecutable</key>
    <string>launcher</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSMinimumSystemVersion</key>
    <string>13.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>LSUIElement</key>
    <false/>
    <key>NSMicrophoneUsageDescription</key>
    <string>Passive Perception needs microphone access to capture audio from your D&amp;D sessions for transcription.</string>
</dict>
</plist>
PLIST

# ── Launcher script ────────────────────────────────────────────────────────
info "Writing launcher..."
cat > "$APP_DIR/Contents/MacOS/launcher" << 'LAUNCHER'
#!/usr/bin/env bash

# ── Passive Perception Launcher ─────────────────────────────────────────────
# Handles first-run setup and launches the app with native macOS dialogs.

APP_DIR="$(cd "$(dirname "$0")/../Resources/app" && pwd)"
SUPPORT_DIR="$HOME/Library/Application Support/Passive Perception"
VENV_DIR="$SUPPORT_DIR/venv"
LOG_FILE="$SUPPORT_DIR/launcher.log"
PYTHON_VERSION="3.11.9"

mkdir -p "$SUPPORT_DIR"

# ── Logging ──────────────────────────────────────────────────────────────────
log() { echo "[$(date '+%H:%M:%S')] $1" >> "$LOG_FILE"; }

# ── Native macOS dialogs ─────────────────────────────────────────────────────
notify() {
  osascript -e "display notification \"$1\" with title \"Passive Perception\""
}

alert() {
  osascript -e "display dialog \"$1\" with title \"Passive Perception\" buttons {\"OK\"} default button \"OK\" with icon caution"
}

ask_yes_no() {
  osascript -e "display dialog \"$1\" with title \"Passive Perception\" buttons {\"Cancel\", \"Continue\"} default button \"Continue\"" 2>/dev/null
  return $?
}

ask_text() {
  osascript -e "display dialog \"$1\" with title \"Passive Perception\" default answer \"\" buttons {\"Cancel\", \"OK\"} default button \"OK\"" 2>/dev/null | sed 's/.*text returned://'
}

progress_dialog() {
  # Show a progress window using osascript — runs in background
  osascript << EOF &
    tell application "System Events"
      display dialog "$1

This may take a few minutes. Please wait..." with title "Passive Perception — Setup" buttons {} giving up after 600 with icon note
    end tell
EOF
  echo $!
}

kill_progress() {
  kill "$1" 2>/dev/null
  # Dismiss any lingering dialog
  osascript -e 'tell application "System Events" to keystroke return' 2>/dev/null
}

# ── Check architecture (use sysctl to detect true hardware, not Rosetta) ─────
if ! sysctl -n machdep.cpu.brand_string 2>/dev/null | grep -qi "apple"; then
  alert "Passive Perception requires Apple Silicon (M1/M2/M3/M4/M5). This Mac is not supported."
  exit 1
fi

# ── First-run setup ──────────────────────────────────────────────────────────
if [ ! -d "$VENV_DIR" ]; then
  log "First run — starting setup"

  ask_yes_no "Welcome to Passive Perception!

This is your first launch. The app needs to install:
• Python 3.11 (via Homebrew + pyenv)
• ML models for speech recognition
• BlackHole audio driver

This will take 5–10 minutes and requires an internet connection." || exit 0

  # ── Homebrew ─────────────────────────────────────────────────────────────
  if ! command -v /opt/homebrew/bin/brew &>/dev/null; then
    log "Installing Homebrew..."
    notify "Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" >> "$LOG_FILE" 2>&1
    eval "$(/opt/homebrew/bin/brew shellenv)"
  fi
  eval "$(/opt/homebrew/bin/brew shellenv)"
  log "Homebrew ready"

  # ── pyenv + Python 3.11 ─────────────────────────────────────────────────
  if ! command -v pyenv &>/dev/null; then
    log "Installing pyenv..."
    notify "Installing pyenv..."
    brew install pyenv >> "$LOG_FILE" 2>&1
  fi
  export PYENV_ROOT="$HOME/.pyenv"
  export PATH="$PYENV_ROOT/bin:$PATH"
  eval "$(pyenv init -)"

  if ! pyenv versions 2>/dev/null | grep -q "$PYTHON_VERSION"; then
    log "Installing Python $PYTHON_VERSION..."
    notify "Installing Python $PYTHON_VERSION — this takes a few minutes..."
    PROGRESS_PID=$(progress_dialog "Installing Python $PYTHON_VERSION...")
    pyenv install "$PYTHON_VERSION" >> "$LOG_FILE" 2>&1
    kill_progress "$PROGRESS_PID"
  fi
  PYTHON_BIN="$(pyenv prefix "$PYTHON_VERSION")/bin/python3"
  log "Python ready: $($PYTHON_BIN --version)"

  # ── Virtual environment + dependencies ───────────────────────────────────
  log "Creating virtual environment..."
  notify "Installing Python packages — this takes a few minutes..."
  PROGRESS_PID=$(progress_dialog "Installing Python packages...\n\nDownloading ML models for speech recognition and note generation.")
  "$PYTHON_BIN" -m venv "$VENV_DIR" >> "$LOG_FILE" 2>&1
  source "$VENV_DIR/bin/activate"
  pip install --upgrade pip --quiet >> "$LOG_FILE" 2>&1
  pip install -r "$APP_DIR/requirements.txt" >> "$LOG_FILE" 2>&1
  kill_progress "$PROGRESS_PID"
  log "Dependencies installed"

  # ── BlackHole ────────────────────────────────────────────────────────────
  if ! ls /Library/Audio/Plug-Ins/HAL/ 2>/dev/null | grep -qi "blackhole"; then
    log "Installing BlackHole..."
    notify "Installing BlackHole audio driver..."
    brew install blackhole-2ch >> "$LOG_FILE" 2>&1

    osascript << 'BHDIALOG'
      display dialog "BlackHole audio driver has been installed!

To capture Discord audio, you need to create a Multi-Output Device:

1. Open Audio MIDI Setup (Applications → Utilities)
2. Click '+' at bottom-left → Create Multi-Output Device
3. Check both your headphones AND BlackHole 2ch
4. Right-click it → Use This Device For Sound Output
5. In Discord: Output Device → Multi-Output Device" with title "Passive Perception — Audio Setup" buttons {"Open Audio MIDI Setup", "I'll Do This Later"} default button "I'll Do This Later"
BHDIALOG
    if [ $? -eq 0 ]; then
      BUTTON=$(osascript -e 'button returned of (display dialog "BlackHole audio driver has been installed!" with title "test" buttons {"Open Audio MIDI Setup", "Later"} default button "Later")' 2>/dev/null)
      if [ "$BUTTON" = "Open Audio MIDI Setup" ]; then
        open "/Applications/Utilities/Audio MIDI Setup.app"
      fi
    fi
  fi

  # ── stt CLI (speaker diarization via FluidAudio/CoreML) ───────────────────
  if ! command -v stt &>/dev/null; then
    log "Installing stt CLI (speaker diarization)..."
    notify "Installing speaker diarization engine..."
    brew tap jvsteiner/tap >> "$LOG_FILE" 2>&1
    brew install stt >> "$LOG_FILE" 2>&1
  fi

  notify "Setup complete! Launching Passive Perception..."
  log "Setup complete"
fi

# ── Launch the app ───────────────────────────────────────────────────────────
log "Launching app..."

# Activate Python
export PYENV_ROOT="$HOME/.pyenv"
export PATH="$PYENV_ROOT/bin:$PATH"
eval "$(pyenv init -)" 2>/dev/null

source "$VENV_DIR/bin/activate"

# Create sessions dir in support directory
mkdir -p "$SUPPORT_DIR/sessions"
if [ ! -e "$APP_DIR/sessions" ]; then
  ln -sf "$SUPPORT_DIR/sessions" "$APP_DIR/sessions"
fi

cd "$APP_DIR"

# Run the app (opens native window, blocks until quit)
python run.py >> "$LOG_FILE" 2>&1

LAUNCHER

chmod +x "$APP_DIR/Contents/MacOS/launcher"

# ── Build DMG ───────────────────────────────────────────────────────────────
info "Creating DMG..."

DMG_STAGING="$BUILD_DIR/dmg_staging"
mkdir -p "$DMG_STAGING"
cp -R "$APP_DIR" "$DMG_STAGING/"

# Create Applications symlink for drag-to-install
ln -s /Applications "$DMG_STAGING/Applications"

# Create the DMG
hdiutil create -volname "Passive Perception" \
  -srcfolder "$DMG_STAGING" \
  -ov -format UDZO \
  "$BUILD_DIR/$DMG_NAME" \
  -quiet

info "Done!"
echo ""
echo "  Output:"
echo "    App: $APP_DIR"
echo "    DMG: $BUILD_DIR/$DMG_NAME"
echo ""
echo "  Upload $BUILD_DIR/$DMG_NAME as a GitHub Release for distribution."
echo ""
