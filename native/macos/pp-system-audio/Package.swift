// swift-tools-version:5.9
// Passive Perception — system audio capture helper.
// Streams mono float32 PCM from macOS Core Audio Process Taps to stdout.

import PackageDescription

let package = Package(
    name: "pp-system-audio",
    platforms: [
        // Process Taps require macOS 14.2. SwiftPM's coarse enum exposes .v14
        // (14.0); we enforce 14.2 at runtime with `if #available`.
        .macOS(.v14),
    ],
    products: [
        .executable(name: "pp-system-audio", targets: ["pp-system-audio"]),
    ],
    targets: [
        .executableTarget(
            name: "pp-system-audio",
            path: "Sources/pp-system-audio",
            exclude: ["Info.plist"],
            linkerSettings: [
                // Embed the Info.plist into the binary's __TEXT,__info_plist
                // section. macOS TCC requires NSAudioCaptureUsageDescription
                // be present here for the Audio Capture permission prompt to
                // fire and for system audio to actually flow through the tap.
                // Without this, AudioHardwareCreateProcessTap succeeds but
                // the IOProc receives only silence.
                .unsafeFlags([
                    "-Xlinker", "-sectcreate",
                    "-Xlinker", "__TEXT",
                    "-Xlinker", "__info_plist",
                    "-Xlinker", "Sources/pp-system-audio/Info.plist",
                ]),
            ]
        ),
    ]
)
