import AVFoundation
import XCTest
@testable import LifeRecorder

final class RecorderTests: XCTestCase {
    @MainActor
    func testSwitchPreferenceSurvivesNewController() async {
        let old = UserDefaults.standard.object(forKey: "recorderEnabled")
        defer { UserDefaults.standard.set(old, forKey: "recorderEnabled") }
        UserDefaults.standard.set(false, forKey: "recorderEnabled")
        let off = Recorder()
        await off.resumeIfEnabled()
        XCTAssertFalse(off.enabled)
        XCTAssertFalse(off.recording)
        UserDefaults.standard.set(true, forKey: "recorderEnabled")
        let on = Recorder()
        XCTAssertTrue(on.enabled)
        XCTAssertFalse(on.recording, "Constructing a controller must not start the microphone in a background launch")
        await on.setEnabled(false)
        XCTAssertFalse(Recorder().enabled)
    }

    func testCaptureSessionUsesA2DPMixingAndNotSpeakerOverride() {
        XCTAssertEqual(AudioRouting.category, .playAndRecord)
        XCTAssertEqual(AudioRouting.mode, .default)
        XCTAssertTrue(AudioRouting.options.contains(.mixWithOthers))
        XCTAssertTrue(AudioRouting.options.contains(.allowBluetoothA2DP))
        XCTAssertTrue(AudioRouting.options.contains(.defaultToSpeaker))
        XCTAssertFalse(AudioRouting.options.contains(.allowBluetooth))
    }

    func testSpeakerIsForcedOnlyWhenNoHeadphonesAreConnected() {
        XCTAssertTrue(AudioRouting.shouldForceSpeaker(outputs: [.builtInReceiver]))
        XCTAssertTrue(AudioRouting.shouldForceSpeaker(outputs: [.builtInSpeaker]))
        XCTAssertFalse(AudioRouting.shouldForceSpeaker(outputs: [.bluetoothA2DP]))
        XCTAssertFalse(AudioRouting.shouldForceSpeaker(outputs: [.headphones]))
        XCTAssertEqual(AudioRouting.outputOverride(outputs: [.builtInReceiver]), .speaker)
        XCTAssertEqual(AudioRouting.outputOverride(outputs: [.bluetoothA2DP]), .none)
    }

    func testBuiltInMicIsPreferredWhenAnotherInputIsActive() {
        XCTAssertTrue(AudioRouting.shouldReassertBuiltInMic(
            currentInput: .bluetoothHFP, availableInputs: [.builtInMic, .bluetoothHFP]))
        XCTAssertFalse(AudioRouting.shouldReassertBuiltInMic(
            currentInput: .builtInMic, availableInputs: [.builtInMic, .bluetoothHFP]))
        XCTAssertFalse(AudioRouting.shouldReassertBuiltInMic(
            currentInput: .bluetoothHFP, availableInputs: [.bluetoothHFP]))
    }

    func testOutputOnlyRouteChangeDoesNotRebuildCapture() {
        let format = AVAudioFormat(standardFormatWithSampleRate: 48000, channels: 1)
        XCTAssertFalse(AudioRouting.shouldRebuildCapture(
            engineRunning: true, currentFormat: format, newFormat: format))
        XCTAssertTrue(AudioRouting.shouldRebuildCapture(
            engineRunning: false, currentFormat: format, newFormat: format))
        let other = AVAudioFormat(standardFormatWithSampleRate: 44100, channels: 1)
        XCTAssertTrue(AudioRouting.shouldRebuildCapture(
            engineRunning: true, currentFormat: format, newFormat: other))
    }

    func testInterruptionResumeHonorsQuietHoursAndEnabledState() {
        XCTAssertTrue(AudioRouting.shouldResumeAfterInterruption(
            enabled: true, quietHoursActive: false, shouldResume: true))
        XCTAssertFalse(AudioRouting.shouldResumeAfterInterruption(
            enabled: true, quietHoursActive: true, shouldResume: true))
        XCTAssertFalse(AudioRouting.shouldResumeAfterInterruption(
            enabled: false, quietHoursActive: false, shouldResume: true))
        XCTAssertFalse(AudioRouting.shouldResumeAfterInterruption(
            enabled: true, quietHoursActive: false, shouldResume: false))
    }

    func testContinuousBuffersRotateIntoDecodableAACWithoutLosingFrames() async throws {
        let before = Set(QueueStore.pending().map(\.id))
        let lock = NSLock()
        var errors: [String] = []
        let writer = ChunkWriter(onChunk: {}, onError: { message in
            lock.lock(); errors.append(message); lock.unlock()
        })
        let format = AVAudioFormat(standardFormatWithSampleRate: 16000, channels: 1)!
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 1600)!
        buffer.frameLength = 1600
        for i in 0..<1600 { buffer.floatChannelData![0][i] = Float(sin(Double(i) * 2 * .pi * 440 / 16000)) * 0.1 }
        for _ in 0..<1220 { writer.consume(buffer) } // 122 s, three files, no microphone required.
        await writer.finish()
        XCTAssertTrue(errors.isEmpty, errors.joined(separator: "; "))
        let chunks = QueueStore.pending().filter { !before.contains($0.id) }
        defer { for chunk in chunks { try? QueueStore.removeAcknowledged(chunk) } }
        XCTAssertEqual(chunks.count, 3)
        XCTAssertEqual(chunks.reduce(0) { $0 + $1.duration }, 122, accuracy: 0.001)
        var decodedFrames: Int64 = 0
        for chunk in chunks {
            XCTAssertEqual(try QueueStore.checksum(chunk.audioURL), chunk.sha256)
            let file = try AVAudioFile(forReading: chunk.audioURL)
            decodedFrames += file.length
        }
        XCTAssertEqual(Double(decodedFrames) / 16000, 122, accuracy: 0.1)
        XCTAssertEqual(chunks[1].startedAt.timeIntervalSince(chunks[0].startedAt), 60, accuracy: 0.001)
        XCTAssertEqual(chunks[2].startedAt.timeIntervalSince(chunks[1].startedAt), 60, accuracy: 0.001)
    }

    func testUnfinishedButDecodableFileIsRecoveredAfterRestart() async throws {
        let journal = RecordingJournal(id: UUID(), startedAt: Date())
        let name = journal.id.uuidString.lowercased()
        let path = QueueStore.directory.appendingPathComponent(name + ".m4a")
        let journalPath = QueueStore.directory.appendingPathComponent(name + ".recording.json")
        let format = AVAudioFormat(standardFormatWithSampleRate: 16000, channels: 1)!
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 16000)!
        buffer.frameLength = 16000
        for i in 0..<16000 { buffer.floatChannelData![0][i] = 0 }
        try JSONEncoder().encode(journal).write(to: journalPath, options: .atomic)
        var file: AVAudioFile? = try AVAudioFile(forWriting: path, settings: [
            AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 16000,
            AVNumberOfChannelsKey: 1, AVEncoderBitRateKey: 32000])
        try file?.write(from: buffer)
        file = nil
        XCTAssertFalse(QueueStore.pending().contains { $0.id == journal.id })
        _ = await QueueStore.recoverInterruptedFiles()
        let chunk = try XCTUnwrap(QueueStore.pending().first { $0.id == journal.id })
        XCTAssertEqual(chunk.duration, 1, accuracy: 0.1)
        XCTAssertFalse(FileManager.default.fileExists(atPath: journalPath.path))
        try QueueStore.removeAcknowledged(chunk)
    }
}
