import AVFoundation

/// Capture must stay on the built-in microphone so Bluetooth headphones can use A2DP playback.
enum AudioRouting {
    static let category: AVAudioSession.Category = .playAndRecord
    static let mode: AVAudioSession.Mode = .default
    static let options: AVAudioSession.CategoryOptions = [.mixWithOthers, .allowBluetoothA2DP, .defaultToSpeaker]
    static let routeCoalesceInterval: TimeInterval = 0.25

    static func preferredBuiltInMic(from inputs: [AVAudioSessionPortDescription]) -> AVAudioSessionPortDescription? {
        inputs.first { $0.portType == .builtInMic }
    }

    static func shouldReassertBuiltInMic(currentInput: AVAudioSession.Port?, availableInputs: [AVAudioSession.Port]) -> Bool {
        availableInputs.contains(.builtInMic) && currentInput != .builtInMic
    }

    static func formatsCompatible(_ lhs: AVAudioFormat?, _ rhs: AVAudioFormat?) -> Bool {
        guard let lhs, let rhs else { return false }
        return lhs.sampleRate == rhs.sampleRate && lhs.channelCount == rhs.channelCount
            && lhs.commonFormat == rhs.commonFormat
    }

    /// Rebuild only when capture is down or the microphone format changed.
    /// Output-only changes, including AirPods A2DP connect/disconnect, must leave the engine running.
    static func shouldRebuildCapture(engineRunning: Bool, currentFormat: AVAudioFormat?, newFormat: AVAudioFormat?) -> Bool {
        if !engineRunning { return true }
        return !formatsCompatible(currentFormat, newFormat)
    }

    static func shouldResumeAfterInterruption(enabled: Bool, quietHoursActive: Bool, shouldResume: Bool) -> Bool {
        enabled && shouldResume && !quietHoursActive
    }

    private static let externalOutputs: Set<AVAudioSession.Port> = [
        .headphones, .bluetoothA2DP, .bluetoothLE, .bluetoothHFP, .carAudio, .airPlay, .HDMI
    ]

    /// Phone speaker when nothing else is plugged in; leave AirPods/headphones alone.
    static func shouldForceSpeaker(outputs: [AVAudioSession.Port]) -> Bool {
        outputs.allSatisfy { !externalOutputs.contains($0) }
    }

    static func outputOverride(outputs: [AVAudioSession.Port]) -> AVAudioSession.PortOverride {
        shouldForceSpeaker(outputs: outputs) ? .speaker : .none
    }
}
