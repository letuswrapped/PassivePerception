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
    // happen on Core Audio's HAL thread; the main thread polls and emits a
    // diagnostic heartbeat every 5s so silent IOProc failures are visible.
    var lastHeartbeat = Date()
    var lastBytes: UInt64 = 0
    var lastFires: UInt64 = 0
    while !shouldStopRef() {
        Thread.sleep(forTimeInterval: 0.1)
        if Date().timeIntervalSince(lastHeartbeat) >= 5.0 {
            let fires = tap.ioProcFireCount
            let bytes = tap.bytesWritten
            let dFires = fires &- lastFires
            let dBytes = bytes &- lastBytes
            if dFires == 0 {
                elog("heartbeat: IOProc has NOT fired in the last 5s (total fires=\(fires), bytes=\(bytes)) — audio source may be silent or tap may be stalled")
            } else {
                elog("heartbeat: IOProc fired \(dFires) times in last 5s, +\(dBytes) bytes (total fires=\(fires), bytes=\(bytes))")
            }
            lastHeartbeat = Date()
            lastFires = fires
            lastBytes = bytes
        }
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

    // Diagnostics — incremented from the HAL thread, read from the main
    // thread for periodic heartbeats. Eventual consistency is fine; no lock.
    fileprivate var ioProcFireCount: UInt64 = 0
    fileprivate var bytesWritten: UInt64 = 0
    fileprivate var firstFireLogged: Bool = false

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

        // Aggregate devices wrapping a tap need a REAL output device as the
        // clock master, otherwise the IOProc never fires on macOS 15+ (the
        // device starts cleanly, but no buffers ever arrive). On 14.2 an empty
        // main sub-device worked because the tap self-clocked. We resolve the
        // system's default output device UID and pin the aggregate to it.
        let mainDeviceUID = (try? Self.copyDefaultOutputDeviceUID()) ?? ""
        if mainDeviceUID.isEmpty {
            elog("warning: no default output device UID available — aggregate may not clock")
        } else {
            elog("aggregate main sub-device: \(mainDeviceUID)")
        }

        let aggregateUID = "com.passiveperception.system-audio.\(UUID().uuidString)"
        let dict: [String: Any] = [
            kAudioAggregateDeviceNameKey as String: "Passive Perception System Audio",
            kAudioAggregateDeviceUIDKey as String: aggregateUID,
            kAudioAggregateDeviceMainSubDeviceKey as String: mainDeviceUID,
            // Must be private. Public aggregates trigger
            // kAudioHardwareUnknownPropertyError on stream-format read.
            kAudioAggregateDeviceIsPrivateKey as String: true,
            kAudioAggregateDeviceIsStackedKey as String: false,
            // TapAutoStart MUST be true, otherwise the tap doesn't engage
            // until something explicitly starts it, and the aggregate's
            // stream format isn't queryable.
            kAudioAggregateDeviceTapAutoStartKey as String: true,
            // Sub-device list provides the actual clock source. Naming a
            // device via kAudioAggregateDeviceMainSubDeviceKey alone is not
            // enough on macOS 15+ — the device also has to appear in the
            // sub-device list, otherwise AudioDeviceStart succeeds but the
            // device never enters the running state (IsRunning=0).
            kAudioAggregateDeviceSubDeviceListKey as String: [
                [
                    kAudioSubDeviceUIDKey as String: mainDeviceUID,
                ],
            ],
            kAudioAggregateDeviceTapListKey as String: [
                [
                    // Inner dict keys cast to String for consistent CFDictionary
                    // bridging — without the casts the nested dict's keys can
                    // round-trip as CFString and may not be recognized by the
                    // tap-list reader on some macOS versions.
                    kAudioSubTapUIDKey as String: tapUID,
                    kAudioSubTapDriftCompensationKey as String: false,
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
        elog("aggregate created: id=\(aggregateDeviceID) uid=\(aggregateUID)")
    }

    // Resolve the UID of the current default output device. Used as the
    // aggregate's main sub-device so the IOProc actually fires.
    private static func copyDefaultOutputDeviceUID() throws -> String {
        var addr = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultOutputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var deviceID: AudioObjectID = kAudioObjectUnknown
        var size = UInt32(MemoryLayout<AudioObjectID>.size)
        let status = AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &deviceID,
        )
        guard status == noErr, deviceID != kAudioObjectUnknown else { return "" }

        var uidAddr = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyDeviceUID,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var cfUID: CFString = "" as CFString
        var uidSize = UInt32(MemoryLayout<CFString>.size)
        let uidStatus = withUnsafeMutablePointer(to: &cfUID) { ptr in
            AudioObjectGetPropertyData(deviceID, &uidAddr, 0, nil, &uidSize, ptr)
        }
        guard uidStatus == noErr else { return "" }
        return cfUID as String
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
        elog("IOProc registered on aggregate device \(aggregateDeviceID)")

        let startStatus = AudioDeviceStart(aggregateDeviceID, procID)
        guard startStatus == noErr else {
            throw TapError(
                code: EXIT_RUNTIME,
                message: "AudioDeviceStart failed (OSStatus=\(startStatus))."
            )
        }
        elog("AudioDeviceStart returned noErr — device should now be producing samples")

        // Query kAudioDevicePropertyDeviceIsRunning in BOTH scopes after a
        // short delay (Start can be asynchronous on macOS 15+).
        Thread.sleep(forTimeInterval: 0.5)
        for (scopeName, scope) in [
            ("Global", kAudioObjectPropertyScopeGlobal),
            ("Input",  kAudioObjectPropertyScopeInput),
            ("Output", kAudioObjectPropertyScopeOutput),
        ] {
            var isRunning: UInt32 = 0
            var runningSize = UInt32(MemoryLayout<UInt32>.size)
            var runningAddr = AudioObjectPropertyAddress(
                mSelector: kAudioDevicePropertyDeviceIsRunning,
                mScope: scope,
                mElement: kAudioObjectPropertyElementMain
            )
            let runStatus = AudioObjectGetPropertyData(
                aggregateDeviceID, &runningAddr, 0, nil, &runningSize, &isRunning,
            )
            elog("IsRunning[\(scopeName)]=\(isRunning) status=\(runStatus)")
        }

        // Also probe the tap itself for IsRunning and the aggregate's
        // sub-device list (post-creation, to confirm the tap is registered).
        var tapRunning: UInt32 = 0
        var tapRunningSize = UInt32(MemoryLayout<UInt32>.size)
        var tapRunningAddr = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyDeviceIsRunning,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        let tapRunStatus = AudioObjectGetPropertyData(
            tapID, &tapRunningAddr, 0, nil, &tapRunningSize, &tapRunning,
        )
        elog("TapIsRunning=\(tapRunning) status=\(tapRunStatus)")
    }

    // ── IO proc body — runs on Core Audio's HAL thread ─────────────────────
    //
    // The aggregate device delivers interleaved float32 in one buffer. We
    // average channels to mono and write directly to stdout. FileHandle.write
    // is blocking, but the kernel pipe buffer (~64 KB) is far larger than our
    // per-callback payload (~2 KB @ 48 kHz mono), so it won't back up unless
    // the Python parent stalls — in which case SIGPIPE will tear us down.
    fileprivate func handleBuffer(_ bufferList: UnsafePointer<AudioBufferList>) {
        ioProcFireCount &+= 1
        if !firstFireLogged {
            firstFireLogged = true
            elog("IOProc fired for the first time — audio is flowing")
        }
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
            bytesWritten &+= UInt64(raw.count)
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
