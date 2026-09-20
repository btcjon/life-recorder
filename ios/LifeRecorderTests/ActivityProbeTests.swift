import AVFoundation
import XCTest
@testable import LifeRecorder

final class ActivityProbeTests: XCTestCase {
    private func buffer(rate: Double = 1000, frames: Int, value: Float) -> AVAudioPCMBuffer {
        let format = AVAudioFormat(standardFormatWithSampleRate: rate, channels: 1)!
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames))!
        buffer.frameLength = AVAudioFrameCount(frames)
        for index in 0..<frames { buffer.floatChannelData![0][index] = value }
        return buffer
    }

    func testQuietCompleteCoverageWouldHold() {
        let format = AVAudioFormat(standardFormatWithSampleRate: 1000, channels: 1)!
        let probe = WindowAccumulator()
        probe.begin(format: format, duration: 0.06)
        probe.consume(buffer(frames: 60, value: 0.0002))
        probe.beginExpected(duration: 0.06)
        probe.finish()
        let decision = ActivityProbe.decide(from: probe)
        XCTAssertEqual(decision.decision, .wouldHold)
        XCTAssertTrue(decision.coverageComplete)
        XCTAssertEqual(decision.windowCount, 3)
        XCTAssertLessThan(decision.maxRMSDBFS ?? 0, -60)
        XCTAssertLessThan(decision.peakDBFS ?? 0, -45)
        XCTAssertTrue(decision.headerValue.contains("d=would_hold"))
    }

    func testActivityWouldUpload() {
        let format = AVAudioFormat(standardFormatWithSampleRate: 1000, channels: 1)!
        let probe = WindowAccumulator()
        probe.begin(format: format, duration: 0.04)
        probe.consume(buffer(frames: 40, value: 0.2))
        probe.beginExpected(duration: 0.04)
        let decision = ActivityProbe.decide(from: probe)
        XCTAssertEqual(decision.decision, .wouldUpload)
        XCTAssertEqual(decision.reason, "activity")
    }

    func testIncompleteAndNonfiniteFailOpen() {
        let format = AVAudioFormat(standardFormatWithSampleRate: 1000, channels: 1)!
        let incomplete = WindowAccumulator()
        incomplete.begin(format: format, duration: 0.04)
        incomplete.consume(buffer(frames: 30, value: 0.0001))
        incomplete.beginExpected(duration: 0.04)
        XCTAssertEqual(ActivityProbe.decide(from: incomplete).decision, .unknown)

        incomplete.finish()
        XCTAssertEqual(ActivityProbe.decide(from: incomplete).decision, .wouldHold)
        XCTAssertTrue(ActivityProbe.decide(from: incomplete).coverageComplete)

        let nanProbe = WindowAccumulator()
        nanProbe.begin(format: format, duration: 0.02)
        nanProbe.consume(buffer(frames: 20, value: .nan))
        nanProbe.beginExpected(duration: 0.02)
        XCTAssertEqual(ActivityProbe.decide(from: nanProbe).reason, "nonfinite")
        XCTAssertEqual(ActivityProbe.recoveredUnknown().reason, "recovered")
    }

    func testLegacyManifestWithoutActivityStillDecodes() throws {
        let chunk = Chunk(id: UUID(), startedAt: Date(), duration: 1, sha256: String(repeating: "a", count: 64))
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        let data = try encoder.encode(chunk)
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        let decoded = try decoder.decode(Chunk.self, from: data)
        XCTAssertNil(decoded.activity)
        let formatter = ISO8601DateFormatter()
        let legacy = Data(#"{"id":"\#(chunk.id.uuidString)","startedAt":"\#(formatter.string(from: chunk.startedAt))","duration":1,"sha256":"\#(chunk.sha256)"}"#.utf8)
        XCTAssertNoThrow(try decoder.decode(Chunk.self, from: legacy))
    }

    func testInterleavedStereoFailsOpen() {
        let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48_000,
                                   channels: 2, interleaved: true)!
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 960)!
        buffer.frameLength = 960
        let probe = WindowAccumulator()
        probe.begin(format: format, duration: 0.02)
        probe.consume(buffer)
        probe.finish()
        let decision = ActivityProbe.decide(from: probe)
        XCTAssertEqual(decision.decision, .unknown)
        XCTAssertEqual(decision.reason, "unsupported")
    }
}
