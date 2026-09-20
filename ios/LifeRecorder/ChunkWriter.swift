import AVFoundation
import Foundation

/// The capture tap copies buffers here; this serial queue preserves their order across file rotations.
final class ChunkWriter: @unchecked Sendable {
    private let queue = DispatchQueue(label: "life.recorder.audio-writer", qos: .utility)
    private var file: AVAudioFile?
    private var journal: RecordingJournal?
    private var chunkFrames: Int64 = 0
    private var totalFrames: Int64 = 0
    private let startedAt = Date()
    private var sampleRate: Double = 48000
    private var failed = false
    private let onChunk: () -> Void
    private let onError: (String) -> Void

    init(onChunk: @escaping () -> Void, onError: @escaping (String) -> Void) {
        self.onChunk = onChunk
        self.onError = onError
    }

    func consume(_ input: AVAudioPCMBuffer) {
        // AVAudioEngine reuses the tap's memory after the callback returns.
        guard let copy = AVAudioPCMBuffer(pcmFormat: input.format, frameCapacity: input.frameLength) else { return }
        copy.frameLength = input.frameLength
        let src = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input.audioBufferList))
        let dst = UnsafeMutableAudioBufferListPointer(copy.mutableAudioBufferList)
        for index in 0..<src.count {
            guard let source = src[index].mData, let destination = dst[index].mData else { return }
            memcpy(destination, source, Int(src[index].mDataByteSize))
        }
        queue.async { [self] in
            guard !failed else { return }
            do {
                if file == nil { try begin(format: copy.format) }
                try file?.write(from: copy)
                chunkFrames += Int64(copy.frameLength)
                totalFrames += Int64(copy.frameLength)
                if Double(chunkFrames) / sampleRate >= 60 { try finishChunk() }
            } catch {
                failed = true
                onError(error.localizedDescription)
            }
        }
    }

    private func begin(format: AVAudioFormat) throws {
        let capacity = try QueueStore.directory.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey])
        if let free = capacity.volumeAvailableCapacityForImportantUsage, free < 200 * 1024 * 1024 {
            throw NSError(domain: "Recorder", code: 1,
                userInfo: [NSLocalizedDescriptionKey: "Storage is nearly full. Pending audio is preserved; free space to resume."])
        }
        sampleRate = format.sampleRate
        let next = RecordingJournal(id: UUID(), startedAt: startedAt.addingTimeInterval(Double(totalFrames) / sampleRate))
        let name = next.id.uuidString.lowercased()
        let journalURL = QueueStore.directory.appendingPathComponent(name + ".recording.json")
        try JSONEncoder().encode(next).write(to: journalURL, options: .atomic)
        let outputURL = QueueStore.directory.appendingPathComponent(name + ".m4a")
        let voiceBitRate = format.sampleRate >= 32000 ? 64000 : 32000
        file = try AVAudioFile(forWriting: outputURL, settings: [
            AVFormatIDKey: kAudioFormatMPEG4AAC,
            AVSampleRateKey: format.sampleRate,
            AVNumberOfChannelsKey: Int(format.channelCount),
            // 32 kbps produced audible metallic/warbled speech on real captures.
            // 64 kbps mono remains compact while preserving voice identity cues.
            AVEncoderBitRateKey: voiceBitRate * Int(format.channelCount)
        ], commonFormat: format.commonFormat, interleaved: format.isInterleaved)
        try FileManager.default.setAttributes([.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication],
                                              ofItemAtPath: outputURL.path)
        journal = next
        chunkFrames = 0
    }

    private func finishChunk() throws {
        file = nil // Closing finalizes the M4A container before computing the checksum.
        guard let journal, chunkFrames > 0 else { return }
        _ = try QueueStore.seal(journal, duration: Double(chunkFrames) / sampleRate)
        self.journal = nil
        chunkFrames = 0
        onChunk()
    }

    func finish() async {
        await withCheckedContinuation { continuation in
            queue.async { [self] in
                do { try finishChunk() } catch { onError(error.localizedDescription) }
                continuation.resume()
            }
        }
    }
}
