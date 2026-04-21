#!/usr/bin/env bash
set -e

# ── Passive Perception — macOS App Builder ──────────────────────────────────
# Creates PassivePerception.app and packages it into a .dmg for distribution.
# Usage: ./build_macos.sh

APP_NAME="Passive Perception"
BUNDLE_ID="com.passiveperception.app"
VERSION="2.0.1"
BUILD_DIR="build"
APP_DIR="$BUILD_DIR/${APP_NAME}.app"
DMG_NAME="PassivePerception-${VERSION}.dmg"
ENTITLEMENTS="$BUILD_DIR/entitlements.plist"

# Signing / notarization config — override via environment if you prefer.
# Leave APPLE_DEVELOPER_ID empty to skip signing entirely (produces an
# unsigned build for local testing).
APPLE_DEVELOPER_ID="${APPLE_DEVELOPER_ID:-Developer ID Application: Colby Schenck (D9L2AS7SDJ)}"
APPLE_NOTARY_PROFILE="${APPLE_NOTARY_PROFILE:-PassivePerception}"

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
VERSION_STAMP="$SUPPORT_DIR/.version"
PYTHON_VERSION="3.11.9"

# Read the current app version from our own Info.plist so upgrades can
# detect a version change and reinstall Python deps. Fail soft to "unknown"
# so a missing/unreadable plist doesn't wedge the launcher.
APP_VERSION="$(/usr/libexec/PlistBuddy -c 'Print CFBundleShortVersionString' \
  "$(dirname "$0")/../Info.plist" 2>/dev/null || echo unknown)"

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
  PROGRESS_PID=$(progress_dialog "Installing Python packages...")
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

  notify "Setup complete! Launching Passive Perception..."
  log "Setup complete"
  echo "$APP_VERSION" > "$VERSION_STAMP"
fi

# ── Upgrade path ─────────────────────────────────────────────────────────────
# If the app version changed since last launch (or the stamp is missing from
# a pre-1.1.1 install), reinstall Python deps against the current
# requirements.txt. Same trap bit us on 1.0 → 1.1: venv existed so setup
# was skipped, but requirements had changed and the backend failed to import.
INSTALLED_VERSION="$(cat "$VERSION_STAMP" 2>/dev/null || echo none)"
if [ "$INSTALLED_VERSION" != "$APP_VERSION" ]; then
  log "Upgrade detected ($INSTALLED_VERSION → $APP_VERSION) — reinstalling Python deps"
  notify "Updating Passive Perception to $APP_VERSION — this takes a minute..."
  PROGRESS_PID=$(progress_dialog "Updating to version $APP_VERSION...\n\nInstalling new Python packages.")

  export PYENV_ROOT="$HOME/.pyenv"
  export PATH="$PYENV_ROOT/bin:$PATH"
  eval "$(pyenv init -)" 2>/dev/null

  source "$VENV_DIR/bin/activate"
  pip install --upgrade pip --quiet >> "$LOG_FILE" 2>&1
  pip install -r "$APP_DIR/requirements.txt" >> "$LOG_FILE" 2>&1
  deactivate 2>/dev/null || true

  kill_progress "$PROGRESS_PID"
  echo "$APP_VERSION" > "$VERSION_STAMP"
  log "Upgrade complete"
fi

# ── Launch the app ───────────────────────────────────────────────────────────
log "Launching app..."

# Activate Python
export PYENV_ROOT="$HOME/.pyenv"
export PATH="$PYENV_ROOT/bin:$PATH"
eval "$(pyenv init -)" 2>/dev/null

source "$VENV_DIR/bin/activate"

# Cloud backend — no local ML binaries (ffmpeg / ollama / whisper models) needed.
# Deepgram (transcription+diarization) and Gemini (notes) are accessed over
# HTTPS; the only runtime requirement beyond Python deps is BlackHole for
# audio capture, which is installed during first-run setup.

# Run from a writable CWD. The config declares `./sessions` and `./tmp` as
# relative paths, and the app bundle under /Applications is read-only on a
# notarized install (writing `tmp/` there fails with EPERM). `config.yaml`
# is loaded via `Path(__file__).parent.parent` so it's unaffected by CWD.
mkdir -p "$SUPPORT_DIR/sessions" "$SUPPORT_DIR/tmp"
cd "$SUPPORT_DIR"

# Run the app (opens native window, blocks until quit). Absolute path is
# required because CWD is $SUPPORT_DIR (writable) rather than $APP_DIR.
python "$APP_DIR/run.py" >> "$LOG_FILE" 2>&1

LAUNCHER

chmod +x "$APP_DIR/Contents/MacOS/launcher"

# ── Entitlements ───────────────────────────────────────────────────────────
# Hardened-runtime exceptions for Python (JIT, unsigned C extensions, pyenv
# env vars) plus microphone access. Required for notarization to accept the
# bundle while still allowing MLX/Whisper/Ollama to run inside the app.
info "Writing entitlements.plist..."
cat > "$ENTITLEMENTS" << 'ENTITLEMENTS_EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>com.apple.security.cs.allow-jit</key>
    <true/>
    <key>com.apple.security.cs.allow-unsigned-executable-memory</key>
    <true/>
    <key>com.apple.security.cs.disable-library-validation</key>
    <true/>
    <key>com.apple.security.cs.allow-dyld-environment-variables</key>
    <true/>
    <key>com.apple.security.device.audio-input</key>
    <true/>
</dict>
</plist>
ENTITLEMENTS_EOF

# ── Code signing ────────────────────────────────────────────────────────────
if [ -n "$APPLE_DEVELOPER_ID" ]; then
  info "Signing app bundle with: $APPLE_DEVELOPER_ID"

  # Sign the launcher first (inside-out: children before parent)
  codesign --force --timestamp --options runtime \
    --entitlements "$ENTITLEMENTS" \
    --sign "$APPLE_DEVELOPER_ID" \
    "$APP_DIR/Contents/MacOS/launcher"

  # Sign the .app bundle itself
  codesign --force --timestamp --options runtime --deep \
    --entitlements "$ENTITLEMENTS" \
    --sign "$APPLE_DEVELOPER_ID" \
    "$APP_DIR"

  # Verify the signature is valid and satisfies Gatekeeper
  info "Verifying signature..."
  codesign --verify --deep --strict --verbose=2 "$APP_DIR"

  # spctl may exit non-zero before notarization is stapled — that's fine here
  spctl --assess --type execute --verbose=2 "$APP_DIR" 2>&1 || \
    warn "spctl assessment will pass after notarization + stapling."
else
  warn "APPLE_DEVELOPER_ID not set — building unsigned app (local testing only)."
fi

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

# ── Sign + notarize + staple the DMG ───────────────────────────────────────
if [ -n "$APPLE_DEVELOPER_ID" ]; then
  info "Signing DMG..."
  codesign --force --timestamp --sign "$APPLE_DEVELOPER_ID" "$BUILD_DIR/$DMG_NAME"

  # Check that a notarization profile exists in the keychain before submitting
  if xcrun notarytool history --keychain-profile "$APPLE_NOTARY_PROFILE" &>/dev/null; then
    info "Submitting DMG to Apple for notarization (this can take 2–10 minutes)..."
    if xcrun notarytool submit "$BUILD_DIR/$DMG_NAME" \
         --keychain-profile "$APPLE_NOTARY_PROFILE" \
         --wait; then
      info "Stapling notarization ticket to DMG..."
      xcrun stapler staple "$BUILD_DIR/$DMG_NAME"
      xcrun stapler validate "$BUILD_DIR/$DMG_NAME"

      info "Final Gatekeeper check..."
      spctl --assess --type open --context context:primary-signature -v "$BUILD_DIR/$DMG_NAME" 2>&1 || true
    else
      warn "Notarization failed — DMG is signed but not notarized."
      warn "Check the log with: xcrun notarytool log <submission-id> --keychain-profile $APPLE_NOTARY_PROFILE"
    fi
  else
    warn "No notary profile '$APPLE_NOTARY_PROFILE' in keychain — skipping notarization."
    warn "Run: xcrun notarytool store-credentials $APPLE_NOTARY_PROFILE"
  fi
fi

info "Done!"
echo ""
echo "  Output:"
echo "    App: $APP_DIR"
echo "    DMG: $BUILD_DIR/$DMG_NAME"
echo ""
echo "  Upload $BUILD_DIR/$DMG_NAME as a GitHub Release for distribution."
echo ""
