import Foundation
import CoreAudio

// ── Capture entry point ─────────────────────────────────────────────────────
//
// 1. Create a system-wide process tap (CATapDescription, default = stereo
//    mixdown of all running processes).
// 2. Wrap it in an aggregate device so we can install an IOProc on it —
//    AudioHardwareCreateProcessTap alone does not deliver samples to a
//    callback; you need an aggregate-device shell.
// 3. Read the aggregate's native format (sample rate, channel count).
// 4. Emit a one-line ASCII header on stdout so the Python reader knows the
//    format, then start the device.
// 5. In the IOProc: average channels to mono and write float32 to stdout.
// 6. On SIGTERM (shouldStopRef returns true): stop, destroy, exit cleanly.

@available(macOS 14.2, *)
func runSystemCapture(shouldStopRef: @escaping () -> Bool) {
    let tap = SystemAudioTap()
    do {
        try tap.start()
    } catch let error as TapError {
        FileHandle.standardError.write(
            "[pp-system-audio] \(error.message)\n".data(using: .utf8)!
        )
        exit(error.code)
    } catch {
        FileHandle.standardError.write(
            "[pp-system-audio] unexpected error: \(error)\n".data(using: .utf8)!
        )
        exit(EXIT_RUNTIME)
    }

    // Idle the main thread until a stop signal is received. IO callbacks
    // happen on Core Audio's HAL thread; the main thread just polls.
    while !shouldStopRef() {
        Thread.sleep(forTimeInterval: 0.1)
    }

    tap.stop()
    exit(EXIT_OK)
}

// ── Errors ──────────────────────────────────────────────────────────────────

struct TapError: Error {
    let code: Int32
    let message: String
}

// ── Tap lifecycle ───────────────────────────────────────────────────────────

@available(macOS 14.2, *)
final class SystemAudioTap {
    private var tapID: AudioObjectID = kAudioObjectUnknown
    private var aggregateDeviceID: AudioObjectID = kAudioObjectUnknown
    private var ioProcID: AudioDeviceIOProcID?
    private var sampleRate: Double = 48000
    private var channels: UInt32 = 2
    private let stdoutHandle = FileHandle.standardOutput

    func start() throws {
        try createTap()
        try createAggregateDevice()
        try readNativeFormat()
        try emitHeader()
        try installIOProcAndStart()
    }

    func stop() {
        if let proc = ioProcID, aggregateDeviceID != kAudioObjectUnknown {
            AudioDeviceStop(aggregateDeviceID, proc)
            AudioDeviceDestroyIOProcID(aggregateDeviceID, proc)
            ioProcID = nil
        }
        if aggregateDeviceID != kAudioObjectUnknown {
            AudioHardwareDestroyAggregateDevice(aggregateDeviceID)
            aggregateDeviceID = kAudioObjectUnknown
        }
        if tapID != kAudioObjectUnknown {
            AudioHardwareDestroyProcessTap(tapID)
            tapID = kAudioObjectUnknown
        }
    }

    // ── Steps ───────────────────────────────────────────────────────────────

    private func createTap() throws {
        // `stereoGlobalTapButExcludeProcesses: []` taps the entire system mix
        // with no exclusions. The `stereoMixdownOfProcesses:` initializer takes
        // an inclusion list and means "tap ONLY these PIDs" — passing `[]`
        // there silently captures nothing, which was the original bug.
        let description = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
        description.muteBehavior = .unmuted
        description.isPrivate = true
        description.isExclusive = false
        description.name = "Passive Perception System Audio"

        var newTap: AudioObjectID = kAudioObjectUnknown
        let status = AudioHardwareCreateProcessTap(description, &newTap)
        guard status == noErr else {
            throw TapError(
                code: EXIT_PERMISSION_DENIED,
                message: "AudioHardwareCreateProcessTap failed (OSStatus=\(status)). " +
                         "Check System Settings → Privacy & Security → Audio Capture."
            )
        }
        tapID = newTap
    }

    private func createAggregateDevice() throws {
        // Fetch the tap UID — required as the sub-tap identifier on the
        // aggregate device.
        var uidProp = AudioObjectPropertyAddress(
            mSelector: kAudioTapPropertyUID,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var tapUID: CFString = "" as CFString
        var size = UInt32(MemoryLayout<CFString>.size)
        let uidStatus = withUnsafeMutablePointer(to: &tapUID) { ptr in
            AudioObjectGetPropertyData(tapID, &uidProp, 0, nil, &size, ptr)
        }
        guard uidStatus == noErr else {
            throw TapError(
                code: EXIT_RUNTIME,
                message: "Failed to read tap UID (OSStatus=\(uidStatus))."
            )
        }

        let aggregateUID = "com.passiveperception.system-audio.\(UUID().uuidString)"
        let dict: [String: Any] = [
            kAudioAggregateDeviceNameKey as String: "Passive Perception System Audio",
            kAudioAggregateDeviceUIDKey as String: aggregateUID,
            kAudioAggregateDeviceMainSubDeviceKey as String: "",
            kAudioAggregateDeviceIsPrivateKey as String: true,
            kAudioAggregateDeviceIsStackedKey as String: false,
            kAudioAggregateDeviceTapAutoStartKey as String: true,
            kAudioAggregateDeviceTapListKey as String: [
                [
                    kAudioSubTapUIDKey: tapUID,
                    kAudioSubTapDriftCompensationKey: false,
                ],
            ],
        ]

        var newAgg: AudioObjectID = kAudioObjectUnknown
        let aggStatus = AudioHardwareCreateAggregateDevice(dict as CFDictionary, &newAgg)
        guard aggStatus == noErr else {
            throw TapError(
                code: EXIT_AGGREGATE_FAILED,
                message: "AudioHardwareCreateAggregateDevice failed (OSStatus=\(aggStatus))."
            )
        }
        aggregateDeviceID = newAgg
    }

    private func readNativeFormat() throws {
        var formatProp = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyStreamFormat,
            mScope: kAudioObjectPropertyScopeInput,
            mElement: kAudioObjectPropertyElementMain
        )
        var asbd = AudioStreamBasicDescription()
        var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        let status = AudioObjectGetPropertyData(aggregateDeviceID, &formatProp, 0, nil, &size, &asbd)
        guard status == noErr else {
            throw TapError(
                code: EXIT_RUNTIME,
                message: "Failed to read aggregate device stream format (OSStatus=\(status))."
            )
        }
        sampleRate = asbd.mSampleRate > 0 ? asbd.mSampleRate : 48000
        channels = asbd.mChannelsPerFrame > 0 ? asbd.mChannelsPerFrame : 2
    }

    private func emitHeader() throws {
        // Python parses this with a regex. Keep the format STABLE — see
        // `_HEADER_RE` in src/audio/backends/macos_system.py.
        let header = "PP1 rate=\(Int(sampleRate)) ch=1 fmt=f32le\n"
        guard let data = header.data(using: .utf8) else {
            throw TapError(code: EXIT_RUNTIME, message: "Header encode failed.")
        }
        stdoutHandle.write(data)
    }

    private func installIOProcAndStart() throws {
        let context = Unmanaged.passUnretained(self).toOpaque()
        var newProcID: AudioDeviceIOProcID?
        let procStatus = AudioDeviceCreateIOProcID(
            aggregateDeviceID,
            ioProcCallback,
            context,
            &newProcID
        )
        guard procStatus == noErr, let procID = newProcID else {
            throw TapError(
                code: EXIT_RUNTIME,
                message: "AudioDeviceCreateIOProcID failed (OSStatus=\(procStatus))."
            )
        }
        ioProcID = procID

        let startStatus = AudioDeviceStart(aggregateDeviceID, procID)
        guard startStatus == noErr else {
            throw TapError(
                code: EXIT_RUNTIME,
                message: "AudioDeviceStart failed (OSStatus=\(startStatus))."
            )
        }
    }

    // ── IO proc body — runs on Core Audio's HAL thread ─────────────────────
    //
    // The aggregate device delivers interleaved float32 in one buffer. We
    // average channels to mono and write directly to stdout. FileHandle.write
    // is blocking, but the kernel pipe buffer (~64 KB) is far larger than our
    // per-callback payload (~2 KB @ 48 kHz mono), so it won't back up unless
    // the Python parent stalls — in which case SIGPIPE will tear us down.
    fileprivate func handleBuffer(_ bufferList: UnsafePointer<AudioBufferList>) {
        let ablPtr = UnsafeMutableAudioBufferListPointer(
            UnsafeMutablePointer(mutating: bufferList)
        )
        guard let firstBuffer = ablPtr.first else { return }
        guard firstBuffer.mDataByteSize > 0, let raw = firstBuffer.mData else { return }

        let channelCount = Int(firstBuffer.mNumberChannels)
        guard channelCount > 0 else { return }

        let totalSamples = Int(firstBuffer.mDataByteSize) / MemoryLayout<Float>.size
        let frameCount = totalSamples / channelCount
        if frameCount == 0 { return }

        let interleaved = raw.bindMemory(to: Float.self, capacity: totalSamples)
        var mono = [Float](repeating: 0, count: frameCount)
        if channelCount == 1 {
            for i in 0..<frameCount {
                mono[i] = interleaved[i]
            }
        } else {
            let inv = 1.0 / Float(channelCount)
            for i in 0..<frameCount {
                var sum: Float = 0
                let base = i * channelCount
                for c in 0..<channelCount {
                    sum += interleaved[base + c]
                }
                mono[i] = sum * inv
            }
        }

        mono.withUnsafeBytes { raw in
            guard let base = raw.baseAddress, raw.count > 0 else { return }
            let data = Data(bytesNoCopy: UnsafeMutableRawPointer(mutating: base),
                            count: raw.count,
                            deallocator: .none)
            stdoutHandle.write(data)
        }
    }
}

// C-callable IOProc bridge.
@available(macOS 14.2, *)
private let ioProcCallback: AudioDeviceIOProc = { (
    _ inDevice: AudioObjectID,
    _ inNow: UnsafePointer<AudioTimeStamp>,
    _ inInputData: UnsafePointer<AudioBufferList>,
    _ inInputTime: UnsafePointer<AudioTimeStamp>,
    _ outOutputData: UnsafeMutablePointer<AudioBufferList>,
    _ inOutputTime: UnsafePointer<AudioTimeStamp>,
    _ inClientData: UnsafeMutableRawPointer?
) -> OSStatus in
    guard let context = inClientData else { return noErr }
    let tap = Unmanaged<SystemAudioTap>.fromOpaque(context).takeUnretainedValue()
    tap.handleBuffer(inInputData)
    return noErr
}
