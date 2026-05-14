# pp-system-audio

Native macOS system-audio capture helper for Passive Perception. Wraps
Core Audio Process Taps (macOS 14.2+) and streams mono float32 PCM to stdout
for the Python parent to consume.

## Build

```bash
cd native/macos/pp-system-audio
swift build -c release
# Output: .build/release/pp-system-audio
```

The `build_macos.sh` script in the repo root compiles this automatically and
copies the binary into `Contents/MacOS/` alongside the launcher.

## CLI

```
pp-system-audio --mode system          # capture, write PCM to stdout
pp-system-audio --check-permission     # probe TCC (may prompt user)
pp-system-audio --check-os             # silent OS version check
```

## stdout format

One ASCII header line, then continuous mono float32 little-endian PCM:

```
PP1 rate=<int> ch=1 fmt=f32le\n
<float32 samples>...
```

The header field order is fixed; Python parses it with a strict regex
(`src/audio/backends/macos_system.py::_HEADER_RE`). If you change the
format here, bump the magic (`PP1` → `PP2`) and update the parser.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Clean stop on SIGTERM (or `--check-*` succeeded) |
| 1 | Audio Capture permission denied |
| 2 | macOS version < 14.2 |
| 3 | Aggregate device creation failed |
| 4 | Bad argv |
| 5 | Runtime Core Audio error after start |

## Signals

- `SIGTERM` / `SIGINT`: main loop notices, calls `stop()`, exits 0.
- `SIGPIPE`: ignored. Writes failing with EPIPE propagate as runtime errors.
- The helper detaches into its own process group via `setpgid(0, 0)` so the
  Python parent can reap the tree with `os.killpg(pid, SIGTERM)`.

## Standalone smoke test

```bash
swift run pp-system-audio --mode system | head -c 192000 > /tmp/out.pcm
# Play music in another app for ~1 second while this runs.
ffplay -f f32le -ar 48000 -ch_layout mono /tmp/out.pcm
```

If the first run triggers a TCC prompt, click Allow. The prompt is attributed
to the parent process (Terminal, iTerm, or the IDE running `swift run`);
inside the Passive Perception bundle the prompt is attributed to the .app.

## Permission model

The helper does NOT call any explicit `requestAccess` API — there isn't a
public one for Process Taps. TCC fires the first time `AudioHardwareCreateProcessTap`
is invoked from a non-allowed process. The app's Info.plist must include
`NSAudioCaptureUsageDescription` (handled by `build_macos.sh`).
