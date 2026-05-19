import Darwin
import Foundation

// ── Exit codes (kept in sync with src/audio/backends/macos_system.py) ─────────
let EXIT_OK: Int32 = 0
let EXIT_PERMISSION_DENIED: Int32 = 1
let EXIT_UNSUPPORTED_OS: Int32 = 2
let EXIT_AGGREGATE_FAILED: Int32 = 3
let EXIT_BAD_ARGS: Int32 = 4
let EXIT_RUNTIME: Int32 = 5

func elog(_ message: String) {
    FileHandle.standardError.write("[pp-system-audio] \(message)\n".data(using: .utf8)!)
}

func usage() {
    elog("usage: pp-system-audio --mode sck | --mode system | --check-permission | --check-os")
}

// stdout pipes can break if the parent (Python) goes away. Without this,
// SIGPIPE silently kills the process; we want a graceful tear-down on EPIPE.
signal(SIGPIPE, SIG_IGN)

// Reap on SIGTERM / SIGINT. The capture path checks `shouldStop` between IO
// callbacks and exits cleanly via main.run loop.
var shouldStop = false
let stopHandler: @convention(c) (Int32) -> Void = { _ in
    shouldStop = true
}
signal(SIGTERM, stopHandler)
signal(SIGINT, stopHandler)

// Detach into our own process group so the Python parent can reap the whole
// tree via os.killpg(pid, SIGTERM) on emergency exit.
_ = setpgid(0, 0)

let args = CommandLine.arguments
guard args.count >= 2 else {
    usage()
    exit(EXIT_BAD_ARGS)
}

switch args[1] {
case "--check-os":
    // No side effects, no TCC interaction.
    if #available(macOS 14.2, *) {
        exit(EXIT_OK)
    } else {
        exit(EXIT_UNSUPPORTED_OS)
    }

case "--check-permission":
    // Probes by attempting a tap. WILL trigger the TCC prompt on a
    // not-yet-determined state. Callers (run.py at app launch) should use
    // --check-os if they want a side-effect-free check.
    if #available(macOS 14.2, *) {
        exit(probeAudioCapturePermission())
    } else {
        exit(EXIT_UNSUPPORTED_OS)
    }

case "--mode":
    guard args.count >= 3 else {
        usage()
        exit(EXIT_BAD_ARGS)
    }
    switch args[2] {
    case "sck":
        // ScreenCaptureKit path — modern, reliable, macOS 13+.
        if #available(macOS 13.0, *) {
            runSCKCapture(shouldStopRef: { shouldStop })
        } else {
            elog("ScreenCaptureKit requires macOS 13.0 or later.")
            exit(EXIT_UNSUPPORTED_OS)
        }
    case "system":
        // Legacy Core Audio Process Tap path — kept for older macOS where the
        // SCK path may not be available, and as an A/B test channel.
        if #available(macOS 14.2, *) {
            runSystemCapture(shouldStopRef: { shouldStop })
        } else {
            elog("Core Audio Process Taps require macOS 14.2 or later.")
            exit(EXIT_UNSUPPORTED_OS)
        }
    default:
        usage()
        exit(EXIT_BAD_ARGS)
    }

default:
    usage()
    exit(EXIT_BAD_ARGS)
}
