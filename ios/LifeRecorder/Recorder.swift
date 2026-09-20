import AVFoundation
import Combine
import Foundation
import UIKit

@MainActor
final class Recorder: ObservableObject {
    @Published private(set) var enabled = UserDefaults.standard.bool(forKey: "recorderEnabled")
    @Published private(set) var recording = false
    @Published private(set) var status = "Recorder is off"
    @Published private(set) var incompleteClips = 0
    private var engine: AVAudioEngine?
    private var writer: ChunkWriter?
    private var starting = false
    private var stopping = false
    private var recovered = false
    private var interrupted = false
    private var observers: [NSObjectProtocol] = []
    private var scheduleTimer: Timer?
    private var routeWorkItem: DispatchWorkItem?
    private var inputFormat: AVAudioFormat?

    init() {
        if enabled { status = "Ready to resume" }
        let center = NotificationCenter.default
        observers.append(center.addObserver(forName: AVAudioSession.interruptionNotification,
            object: nil, queue: .main) { [weak self] note in
                Task { @MainActor in await self?.handleInterruption(note) }
            })
        observers.append(center.addObserver(forName: AVAudioSession.mediaServicesWereResetNotification,
            object: nil, queue: .main) { [weak self] _ in
                Task { @MainActor in
                    guard let self else { return }
                    await self.stopCapture()
                    await self.resumeIfEnabled(allowBackground: true)
                }
            })
        observers.append(center.addObserver(forName: AVAudioSession.routeChangeNotification,
            object: nil, queue: .main) { [weak self] note in
                Task { @MainActor in await self?.handleRouteChange() }
            })
        observers.append(center.addObserver(forName: .AVAudioEngineConfigurationChange,
            object: nil, queue: .main) { [weak self] _ in
                Task { @MainActor in await self?.handleRouteChange() }
            })
        armScheduleTimer()
    }

    func setEnabled(_ value: Bool) async {
        enabled = value
        UserDefaults.standard.set(value, forKey: "recorderEnabled")
        if value { await applySchedule() }
        else {
            scheduleTimer?.invalidate()
            await stopCapture()
            status = "Recorder is off"
        }
    }

    func applySchedule(now: Date = Date()) async {
        guard enabled else {
            scheduleTimer?.invalidate()
            return
        }
        if QuietHours.isActive(at: now) {
            await stopCapture()
            status = "Quiet hours until 5:00 AM Eastern"
        } else {
            await resumeIfEnabled(allowBackground: true)
        }
        armScheduleTimer(now: now)
    }

    func resumeIfEnabled(allowBackground: Bool = false) async {
        guard enabled, !recording, !starting, !stopping, !interrupted else { return }
        if QuietHours.isActive(at: Date()) {
            status = "Quiet hours until 5:00 AM Eastern"
            armScheduleTimer()
            return
        }
        // Background upload completion must never start a new microphone session.
        guard allowBackground || UIApplication.shared.applicationState == .active else { return }
        starting = true
        defer { starting = false }
        if !recovered {
            incompleteClips = await QueueStore.recoverInterruptedFiles()
            recovered = true
        }
        let permission = AVAudioApplication.shared.recordPermission
        if permission == .undetermined {
            guard UIApplication.shared.applicationState == .active else { return }
            let granted = await withCheckedContinuation { continuation in
                AVAudioApplication.requestRecordPermission { continuation.resume(returning: $0) }
            }
            guard granted else { status = "Allow microphone access in Settings to record"; return }
        } else if permission == .denied {
            status = "Allow microphone access in Settings to record"
            return
        }
        guard enabled else { return } // The switch may have changed during permission/recovery.
        do {
            try applyCaptureSession()
            let nextEngine = AVAudioEngine()
            let input = nextEngine.inputNode
            let format = input.outputFormat(forBus: 0)
            guard format.sampleRate > 0 && format.channelCount > 0 else {
                throw NSError(domain: "Recorder", code: 1,
                    userInfo: [NSLocalizedDescriptionKey: "The microphone is unavailable. Reopen the app to retry."])
            }
            let nextWriter = ChunkWriter(onChunk: {
                DispatchQueue.main.async { UploadManager.shared.pump() }
            }, onError: { [weak self] message in
                Task { @MainActor in
                    guard let self else { return }
                    await self.stopCapture()
                    self.status = message
                }
            })
            input.installTap(onBus: 0, bufferSize: 4096, format: format) { buffer, _ in nextWriter.consume(buffer) }
            nextEngine.prepare()
            try nextEngine.start()
            engine = nextEngine
            writer = nextWriter
            inputFormat = format
            recording = true
            status = "Recording, including when locked"
            armScheduleTimer()
        } catch {
            try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
            inputFormat = nil
            status = "Could not start: " + error.localizedDescription
        }
    }

    private func armScheduleTimer(now: Date = Date()) {
        scheduleTimer?.invalidate()
        let boundary = QuietHours.nextTransition(from: now)
        scheduleTimer = Timer.scheduledTimer(withTimeInterval: max(1, boundary.timeIntervalSince(now)), repeats: false) {
            [weak self] _ in Task { @MainActor in await self?.applySchedule() }
        }
    }

    private func stopCapture(deactivate: Bool = true) async {
        guard !stopping else { return }
        stopping = true
        routeWorkItem?.cancel()
        routeWorkItem = nil
        let backgroundTask = UIApplication.shared.beginBackgroundTask(withName: "Finish audio chunk")
        defer {
            stopping = false
            if backgroundTask != .invalid { UIApplication.shared.endBackgroundTask(backgroundTask) }
        }
        engine?.inputNode.removeTap(onBus: 0)
        if engine?.isRunning == true { engine?.stop() }
        engine = nil
        recording = false
        let previous = writer
        writer = nil
        inputFormat = nil
        await previous?.finish()
        if deactivate { try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation) }
        UploadManager.shared.pump()
    }

    private func handleInterruption(_ notification: Notification) async {
        guard let raw = notification.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
              let type = AVAudioSession.InterruptionType(rawValue: raw) else { return }
        if type == .began {
            interrupted = true
            await stopCapture(deactivate: false)
            if enabled { status = "Interrupted by another audio session" }
        } else {
            interrupted = false
            let rawOptions = notification.userInfo?[AVAudioSessionInterruptionOptionKey] as? UInt ?? 0
            let shouldResume = AVAudioSession.InterruptionOptions(rawValue: rawOptions).contains(.shouldResume)
            if AudioRouting.shouldResumeAfterInterruption(
                enabled: enabled,
                quietHoursActive: QuietHours.isActive(at: Date()),
                shouldResume: shouldResume) {
                await resumeIfEnabled(allowBackground: true)
            } else if enabled { status = "Recording paused. Reopen the app to resume." }
        }
    }

    private func handleRouteChange() async {
        guard enabled, !interrupted else { return }
        routeWorkItem?.cancel()
        let work = DispatchWorkItem { [weak self] in
            Task { @MainActor in await self?.reconcileRoute() }
        }
        routeWorkItem = work
        DispatchQueue.main.asyncAfter(deadline: .now() + AudioRouting.routeCoalesceInterval, execute: work)
    }

    private func reconcileRoute() async {
        guard enabled, !interrupted, !starting, !stopping else { return }
        if QuietHours.isActive(at: Date()) { return }
        do {
            try applyCaptureSession()
            let newFormat = engine?.inputNode.outputFormat(forBus: 0)
            let rebuild = AudioRouting.shouldRebuildCapture(
                engineRunning: engine?.isRunning == true && recording,
                currentFormat: inputFormat,
                newFormat: newFormat)
            guard rebuild else { return }
            await stopCapture(deactivate: false)
            await resumeIfEnabled(allowBackground: true)
        } catch {
            status = "Could not keep recording after an audio-route change"
        }
    }

    @discardableResult
    private func applyCaptureSession() throws -> AVAudioSession {
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(AudioRouting.category, mode: AudioRouting.mode, options: AudioRouting.options)
        try session.setActive(true)
        let current = session.currentRoute.inputs.first?.portType
        let available = session.availableInputs?.map(\.portType) ?? []
        if AudioRouting.shouldReassertBuiltInMic(currentInput: current, availableInputs: available),
           let mic = AudioRouting.preferredBuiltInMic(from: session.availableInputs ?? []) {
            try session.setPreferredInput(mic)
        }
        try session.overrideOutputAudioPort(
            AudioRouting.outputOverride(outputs: session.currentRoute.outputs.map(\.portType)))
        return session
    }
}
