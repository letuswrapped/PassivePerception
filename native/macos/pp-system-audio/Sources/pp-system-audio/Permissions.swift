import Foundation
import CoreAudio

// Probe the Audio Capture permission by attempting to create a system-wide
// process tap. If the OS allows it, we got permission (and we destroy the
// tap immediately so we don't leak any audio). If creation fails because of
// TCC, we get a permission-class OSStatus and return EXIT_PERMISSION_DENIED.
//
// IMPORTANT: this call WILL trigger the TCC prompt on a not-yet-determined
// state. There is no public API to query Process Tap permission silently.
// Callers that need a side-effect-free check should use `--check-os`
// instead and treat permission as "unknown until first session start."
@available(macOS 14.2, *)
func probeAudioCapturePermission() -> Int32 {
    let description = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
    description.muteBehavior = .unmuted
    description.isPrivate = true
    description.isExclusive = false
    description.name = "Passive Perception (permission probe)"

    var tapID: AudioObjectID = kAudioObjectUnknown
    let status = AudioHardwareCreateProcessTap(description, &tapID)

    if status == noErr {
        // Granted — clean up the probe tap immediately.
        _ = AudioHardwareDestroyProcessTap(tapID)
        return EXIT_OK
    }

    // Apple has not documented a stable OSStatus for "TCC denied" on process
    // taps. The values observed in practice are kAudioHardwareUnspecifiedError
    // (560292419) and kAudioHardwareIllegalOperationError (1852797029). Either
    // can also indicate genuine API failure on a healthy system. We treat any
    // failure as "permission probe inconclusive — denied for our purposes."
    // Python then falls back to BlackHole if installed.
    FileHandle.standardError.write(
        "[pp-system-audio] permission probe failed: OSStatus=\(status)\n".data(using: .utf8)!
    )
    return EXIT_PERMISSION_DENIED
}
