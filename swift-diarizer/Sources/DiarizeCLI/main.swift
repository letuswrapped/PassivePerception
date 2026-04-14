///
/// DiarizeCLI — Thin wrapper around FluidAudio's offline diarization.
///
/// Usage:
///   DiarizeCLI <audio.wav> <output.json>
///
/// Output (JSON):
///   {"segments": [{"start": 0.0, "end": 5.2, "speaker": "SPEAKER_00"}, ...]}
///
/// Runs entirely on Apple Neural Engine via CoreML. No tokens required.
/// Models auto-download on first run (~200 MB).
///

import Foundation
import FluidAudio

// ── Helpers ─────────────────────────────────────────────────────────────────

func log(_ msg: String) {
    let stderr = FileHandle.standardError
    stderr.write("[diarization] \(msg)\n".data(using: .utf8)!)
}

func writeJSON(_ dict: [String: Any], to path: String) {
    if let data = try? JSONSerialization.data(withJSONObject: dict, options: .prettyPrinted) {
        try? data.write(to: URL(fileURLWithPath: path))
    }
}

// ── Argument parsing ────────────────────────────────────────────────────────

let args = CommandLine.arguments
guard args.count >= 3 else {
    log("Usage: DiarizeCLI <audio.wav> <output.json>")
    exit(1)
}

let audioPath = args[1]
let outputPath = args[2]

// ── Main ────────────────────────────────────────────────────────────────────

guard FileManager.default.fileExists(atPath: audioPath) else {
    log("Audio file not found: \(audioPath)")
    writeJSON(["error": "Audio file not found", "segments": [Any]()], to: outputPath)
    exit(1)
}

let fileSize = (try? FileManager.default.attributesOfItem(atPath: audioPath)[.size] as? Int) ?? 0
let sizeMB = Double(fileSize) / 1024.0 / 1024.0
log("Processing \(URL(fileURLWithPath: audioPath).lastPathComponent) (\(String(format: "%.1f", sizeMB)) MB)...")

do {
    let config = OfflineDiarizerConfig()
    let manager = OfflineDiarizerManager(config: config)

    log("Preparing models (first run downloads ~200 MB)...")
    try await manager.prepareModels()
    log("Models ready")

    log("Running diarization on Apple Neural Engine...")
    let audioURL = URL(fileURLWithPath: audioPath)
    let result = try await manager.process(audioURL)

    // Map speaker IDs to 0-indexed SPEAKER_NN format
    var speakerMap: [String: Int] = [:]
    var nextIdx = 0
    var segments: [[String: Any]] = []

    for segment in result.segments {
        let spkId = segment.speakerId
        if speakerMap[spkId] == nil {
            speakerMap[spkId] = nextIdx
            nextIdx += 1
        }
        let idx = speakerMap[spkId]!
        segments.append([
            "start": round(segment.startTimeSeconds * 1000) / 1000,
            "end": round(segment.endTimeSeconds * 1000) / 1000,
            "speaker": String(format: "SPEAKER_%02d", idx),
        ])
    }

    writeJSON(["segments": segments], to: outputPath)

    let speakers = Set(segments.compactMap { $0["speaker"] as? String })
    log("Found \(speakers.count) speaker(s): \(speakers.sorted())")
    log("Wrote \(segments.count) segments to \(outputPath)")

} catch {
    log("Diarization failed: \(error.localizedDescription)")
    writeJSON(["error": error.localizedDescription, "segments": [Any]()], to: outputPath)
    exit(1)
}
