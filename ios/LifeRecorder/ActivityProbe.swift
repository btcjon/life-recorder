import AVFoundation
import Foundation

struct ActivityDecision: Equatable {
    static let version = 1
    static let windowSeconds = 0.020
    static let holdRMSDBFS = -60.0
    static let holdPeakDBFS = -45.0

    enum Decision: String {
        case wouldHold = "would_hold"
        case wouldUpload = "would_upload"
        case unknown = "unknown"
    }

    let version: Int
    let decision: Decision
    let coverageComplete: Bool
    let windowCount: Int
    let expectedWindows: Int
    let maxRMSDBFS: Double?
    let peakDBFS: Double?
    let reason: String

    var headerValue: String {
        var parts = [
            "v=\(version)",
            "d=\(decision.rawValue)",
            "c=\(coverageComplete ? 1 : 0)",
            "w=\(windowCount)",
            "e=\(expectedWindows)",
            "r=\(reason)",
        ]
        if let maxRMSDBFS { parts.insert("rms=\(Self.format(maxRMSDBFS))", at: 5) }
        if let peakDBFS { parts.insert("pk=\(Self.format(peakDBFS))", at: peakDBFSInsertIndex(parts)) }
        return parts.joined(separator: ";")
    }

    private func peakDBFSInsertIndex(_ parts: [String]) -> Int {
        parts.firstIndex(where: { $0.hasPrefix("rms=") }).map { $0 + 1 } ?? 5
    }

    static func format(_ value: Double) -> String {
        String(format: "%.2f", value)
    }
}

enum ActivityProbe {
    static func decide(from accumulator: WindowAccumulator) -> ActivityDecision {
        guard accumulator.supported, accumulator.nonfinite == 0 else {
            return ActivityDecision(
                version: ActivityDecision.version,
                decision: .unknown,
                coverageComplete: false,
                windowCount: accumulator.completeWindows,
                expectedWindows: accumulator.expectedWindows,
                maxRMSDBFS: nil,
                peakDBFS: nil,
                reason: accumulator.supported ? "nonfinite" : "unsupported"
            )
        }
        let coverage = accumulator.expectedWindows > 0
            && accumulator.completeWindows == accumulator.expectedWindows
            && accumulator.remainderFrames == 0
        guard coverage, let rms = accumulator.maxRMSDBFS, let peak = accumulator.peakDBFS else {
            return ActivityDecision(
                version: ActivityDecision.version,
                decision: .unknown,
                coverageComplete: coverage,
                windowCount: accumulator.completeWindows,
                expectedWindows: accumulator.expectedWindows,
                maxRMSDBFS: accumulator.maxRMSDBFS,
                peakDBFS: accumulator.peakDBFS,
                reason: coverage ? "missing_levels" : "incomplete"
            )
        }
        let hold = rms < ActivityDecision.holdRMSDBFS && peak < ActivityDecision.holdPeakDBFS
        return ActivityDecision(
            version: ActivityDecision.version,
            decision: hold ? .wouldHold : .wouldUpload,
            coverageComplete: true,
            windowCount: accumulator.completeWindows,
            expectedWindows: accumulator.expectedWindows,
            maxRMSDBFS: rms,
            peakDBFS: peak,
            reason: hold ? "quiet" : "activity"
        )
    }

    static func recoveredUnknown() -> ActivityDecision {
        ActivityDecision(
            version: ActivityDecision.version,
            decision: .unknown,
            coverageComplete: false,
            windowCount: 0,
            expectedWindows: 0,
            maxRMSDBFS: nil,
            peakDBFS: nil,
            reason: "recovered"
        )
    }
}

final class WindowAccumulator {
    private(set) var supported = true
    private(set) var nonfinite = 0
    private(set) var completeWindows = 0
    private(set) var remainderFrames = 0
    private(set) var expectedWindows = 0
    private(set) var maxRMS = 0.0
    private(set) var peak = 0.0
    private var sampleRate = 0.0
    private var windowFrames = 0
    private var channelCount = 0
    private var sums: [Double] = []
    private var counts: [Int] = []

    var maxRMSDBFS: Double? { completeWindows > 0 ? Self.dbfs(maxRMS) : nil }
    var peakDBFS: Double? { completeWindows > 0 ? Self.dbfs(peak) : nil }

    func begin(format: AVAudioFormat, duration: Double) {
        reset()
        sampleRate = format.sampleRate
        channelCount = Int(format.channelCount)
        // floatChannelData below is planar. Fail open for interleaved layouts
        // instead of risking a false quiet classification from incorrect indexing.
        supported = sampleRate > 0 && channelCount > 0
            && format.commonFormat == .pcmFormatFloat32 && !format.isInterleaved
        windowFrames = max(1, Int((ActivityDecision.windowSeconds * sampleRate).rounded()))
        beginExpected(duration: duration)
        sums = Array(repeating: 0, count: max(channelCount, 0))
        counts = Array(repeating: 0, count: max(channelCount, 0))
    }

    func beginExpected(duration: Double) {
        expectedWindows = Int(ceil(max(0, duration) / ActivityDecision.windowSeconds))
    }

    func consume(_ buffer: AVAudioPCMBuffer) {
        guard supported else { return }
        guard let channels = buffer.floatChannelData else {
            supported = false
            return
        }
        let frames = Int(buffer.frameLength)
        let channelLimit = min(channelCount, Int(buffer.format.channelCount))
        for frame in 0..<frames {
            for channel in 0..<channelLimit {
                let sample = Double(channels[channel][frame])
                if !sample.isFinite {
                    nonfinite += 1
                    continue
                }
                peak = max(peak, abs(sample))
                sums[channel] += sample * sample
                counts[channel] += 1
            }
            remainderFrames += 1
            if remainderFrames >= windowFrames {
                closeWindow()
            }
        }
    }

    func finish() {
        // A chunk rarely ends exactly on a 20 ms boundary. Count its final
        // partial window so arbitrary AVAudioEngine buffer sizes retain full coverage.
        if remainderFrames > 0 { closeWindow() }
    }

    private func closeWindow() {
        var windowRMS = 0.0
        for channel in 0..<channelCount where counts[channel] > 0 {
            windowRMS = max(windowRMS, sqrt(sums[channel] / Double(counts[channel])))
            sums[channel] = 0
            counts[channel] = 0
        }
        maxRMS = max(maxRMS, windowRMS)
        completeWindows += 1
        remainderFrames = 0
    }

    func reset() {
        supported = true
        nonfinite = 0
        completeWindows = 0
        remainderFrames = 0
        expectedWindows = 0
        maxRMS = 0
        peak = 0
        sums = []
        counts = []
    }

    static func dbfs(_ amplitude: Double) -> Double {
        if !(amplitude.isFinite) { return .nan }
        if amplitude <= 0 { return -160 }
        return 20.0 * log10(amplitude)
    }
}
