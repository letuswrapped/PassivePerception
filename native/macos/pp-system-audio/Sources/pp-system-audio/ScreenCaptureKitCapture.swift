import Foundation
import CoreMedia
import AVFoundation
import ScreenCaptureKit

// ── ScreenCaptureKit-based system audio capture ─────────────────────────────
//
// The Core Audio Process Tap + Aggregate Device pattern is broken on macOS
// 15+/26: AudioDeviceStart returns noErr but the device never enters the
// running state, so the IOProc never fires. ScreenCaptureKit is Apple's
// modern, reliable API for system audio — it's what QuickTime, OBS, Loom,
// and Apple's own samples use. Works on macOS 13+. Requires Screen Recording
// permission (NSScreenCaptureUsageDescription), NOT Audio Capture.
//
// Same stdout contract as the legacy tap path: a "PP1 rate=N ch=1 fmt=f32le"
// header followed by mono float32 PCM, so the Python reader (macos_system.py)
// doesn't need to know which backend produced the bytes.

@available(macOS 13.0, *)
func runSCKCapture(shouldStopRef: @escaping () -> Bool) {
    let semaphore = DispatchSemaphore(value: 0)
    var startupError: Error?
    let capture = SCKCapture()

    Task.detached {
        do {
            try await capture.start()
        } catch {
            startupError = error
        }
        semaphore.signal()
    }
    semaphore.wait()

    if let err = startupError {
        let nsErr = err as NSError
        let code: Int32 = nsErr.domain == "SCKPermissionDenied" ? EXIT_PERMISSION_DENIED : EXIT_RUNTIME
        elog("ScreenCaptureKit start failed: \(err.localizedDescription)")
        exit(code)
    }

    elog("ScreenCaptureKit capture started — audio should be flowing")

    var lastHeartbeat = Date()
    var lastBytes: UInt64 = 0
    // Faster heartbeat early on so the Python watchdog sees byte counts
    // before its 3s timeout, then back off to 5s.
    var heartbeatInterval: TimeInterval = 1.0
    var heartbeatsEmitted = 0
    while !shouldStopRef() {
        Thread.sleep(forTimeInterval: 0.1)
        if Date().timeIntervalSince(lastHeartbeat) >= heartbeatInterval {
            let bytes = capture.bytesWritten
            let buffers = capture.audioBuffersReceived
            let delta = bytes &- lastBytes
            elog("heartbeat: buffers=\(buffers) bytes=\(bytes) (+\(delta) in \(String(format: "%.1f", heartbeatInterval))s)")
            lastHeartbeat = Date()
            lastBytes = bytes
            heartbeatsEmitted += 1
            if heartbeatsEmitted >= 5 { heartbeatInterval = 5.0 }
        }
    }

    let stopSem = DispatchSemaphore(value: 0)
    Task.detached {
        await capture.stop()
        stopSem.signal()
    }
    stopSem.wait()
    exit(EXIT_OK)
}

@available(macOS 13.0, *)
final class SCKCapture: NSObject, SCStreamOutput, SCStreamDelegate, @unchecked Sendable {
    private var stream: SCStream?
    private let stdoutHandle = FileHandle.standardOutput
    private let audioQueue = DispatchQueue(label: "com.passiveperception.sck.audio", qos: .userInteractive)
    private let videoQueue = DispatchQueue(label: "com.passiveperception.sck.video", qos: .utility)
    private var headerEmitted = false
    private var sampleRate: Double = 48000
    fileprivate var bytesWritten: UInt64 = 0
    fileprivate var audioBuffersReceived: UInt64 = 0
    private var formatLogged = false
    private var firstBufferLogged = false
    private var extractionFailures = 0

    func start() async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        guard let display = content.displays.first else {
            throw NSError(
                domain: "SCKRuntime", code: 1,
                userInfo: [NSLocalizedDescriptionKey: "No displays available for SCContentFilter."]
            )
        }

        // Filter: the display is required as anchor, but we'll only consume
        // the audio output, not video. Exclude our own process so we don't
        // capture our own beeps / pywebview audio if any.
        let filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])

        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.excludesCurrentProcessAudio = true
        config.sampleRate = 48000
        config.channelCount = 2
        // Minimal video config — required by SCK even when we only want audio.
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1)  // 1 fps; near-zero cost
        config.queueDepth = 6

        let stream = SCStream(filter: filter, configuration: config, delegate: self)
        // Separate queues so audio delivery doesn't serialize behind video.
        try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: audioQueue)
        try stream.addStreamOutput(self, type: .screen, sampleHandlerQueue: videoQueue)
        self.stream = stream

        try await stream.startCapture()
    }

    func stop() async {
        guard let stream = stream else { return }
        do {
            try await stream.stopCapture()
        } catch {
            elog("stream.stopCapture error: \(error.localizedDescription)")
        }
        self.stream = nil
    }

    // ── SCStreamOutput ────────────────────────────────────────────────────

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio else { return }
        guard CMSampleBufferDataIsReady(sampleBuffer) else {
            elog("audio buffer not ready — skipped")
            return
        }

        audioBuffersReceived &+= 1
        if !firstBufferLogged {
            firstBufferLogged = true
            elog("first audio CMSampleBuffer received — SCK is producing audio")
        }

        // SCK delivers Float32 audio. Read the format to know channel count
        // and sample rate; downmix to mono and write to stdout.
        guard let formatDescription = CMSampleBufferGetFormatDescription(sampleBuffer) else {
            elog("CMSampleBufferGetFormatDescription returned nil")
            return
        }
        guard let asbdPtr = CMAudioFormatDescriptionGetStreamBasicDescription(formatDescription) else {
            elog("CMAudioFormatDescriptionGetStreamBasicDescription returned nil")
            return
        }
        let asbd = asbdPtr.pointee

        if !formatLogged {
            formatLogged = true
            let nonInterleaved = (asbd.mFormatFlags & kAudioFormatFlagIsNonInterleaved) != 0
            elog("audio format: rate=\(asbd.mSampleRate) ch=\(asbd.mChannelsPerFrame) bits=\(asbd.mBitsPerChannel) flags=\(String(format: "0x%x", asbd.mFormatFlags)) nonInterleaved=\(nonInterleaved)")
        }

        if !headerEmitted {
            sampleRate = asbd.mSampleRate > 0 ? asbd.mSampleRate : 48000
            let header = "PP1 rate=\(Int(sampleRate)) ch=1 fmt=f32le\n"
            if let data = header.data(using: .utf8) {
                stdoutHandle.write(data)
            }
            headerEmitted = true
            elog("PP1 header written to stdout")
        }

        // Extract the AudioBufferList from the CMSampleBuffer. The
        // *WithRetainedBlockBuffer variant needs the buffer list sized for the
        // actual channel count — for non-interleaved N-channel audio, that's
        // N AudioBuffer slots. The bare AudioBufferList struct only has one
        // inline slot, so for >1-channel non-interleaved we allocate.
        let channelCount = Int(asbd.mChannelsPerFrame)
        guard channelCount > 0 else {
            elog("channelCount=0 from ASBD — skipping buffer")
            return
        }
        let isInterleaved = (asbd.mFormatFlags & kAudioFormatFlagIsNonInterleaved) == 0
        let listBuffersNeeded = isInterleaved ? 1 : channelCount
        let listByteSize = MemoryLayout<AudioBufferList>.size +
            (max(0, listBuffersNeeded - 1) * MemoryLayout<AudioBuffer>.size)
        let listRawPtr = UnsafeMutableRawPointer.allocate(
            byteCount: listByteSize, alignment: MemoryLayout<AudioBufferList>.alignment,
        )
        defer { listRawPtr.deallocate() }
        let listPtr = listRawPtr.bindMemory(to: AudioBufferList.self, capacity: 1)
        listPtr.pointee = AudioBufferList()

        var blockBuffer: CMBlockBuffer?
        let status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: nil,
            bufferListOut: listPtr,
            bufferListSize: listByteSize,
            blockBufferAllocator: nil,
            blockBufferMemoryAllocator: nil,
            flags: kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment,
            blockBufferOut: &blockBuffer
        )
        guard status == noErr else {
            extractionFailures &+= 1
            if extractionFailures <= 5 {
                elog("CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer status=\(status) (failure #\(extractionFailures))")
            }
            return
        }

        var mono: [Float] = []
        let ablPtr = UnsafeMutableAudioBufferListPointer(listPtr)
        guard let firstBuf = ablPtr.first else {
            elog("AudioBufferList has no buffers — skipping")
            return
        }
        let firstFrameCount = Int(firstBuf.mDataByteSize) / MemoryLayout<Float>.size
        if firstFrameCount == 0 {
            // Empty buffers are normal when no audio is playing on the system
            // (SCK still delivers heartbeat frames). Skip silently.
            return
        }

        if isInterleaved {
            // One buffer, interleaved samples.
            guard let raw = firstBuf.mData else { return }
            let totalSamples = Int(firstBuf.mDataByteSize) / MemoryLayout<Float>.size
            let frameCount = totalSamples / channelCount
            let interleaved = raw.bindMemory(to: Float.self, capacity: totalSamples)
            mono = [Float](repeating: 0, count: frameCount)
            if channelCount == 1 {
                for i in 0..<frameCount { mono[i] = interleaved[i] }
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
        } else {
            // Non-interleaved: one buffer per channel. Average frame-wise.
            mono = [Float](repeating: 0, count: firstFrameCount)
            let inv = 1.0 / Float(channelCount)
            for ch in 0..<channelCount {
                let buf = ablPtr[ch]
                guard let raw = buf.mData else { continue }
                let count = Int(buf.mDataByteSize) / MemoryLayout<Float>.size
                let samples = raw.bindMemory(to: Float.self, capacity: count)
                let usable = min(count, firstFrameCount)
                for i in 0..<usable {
                    mono[i] += samples[i] * inv
                }
            }
        }

        if mono.isEmpty { return }
        mono.withUnsafeBytes { raw in
            guard let base = raw.baseAddress, raw.count > 0 else { return }
            let data = Data(bytesNoCopy: UnsafeMutableRawPointer(mutating: base),
                            count: raw.count,
                            deallocator: .none)
            stdoutHandle.write(data)
            bytesWritten &+= UInt64(raw.count)
        }
    }

    // ── SCStreamDelegate ──────────────────────────────────────────────────

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        elog("SCStream stopped with error: \(error.localizedDescription)")
    }
}
