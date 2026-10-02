import AVFoundation
import XCTest
@testable import LifeRecorder

final class NativeUploadTests: XCTestCase {
    @MainActor
    func testPinnedHTTPSUploadRemovesOnlyAcknowledgedClip() async throws {
        let environment = ProcessInfo.processInfo.environment
        #if !targetEnvironment(simulator)
        throw XCTSkip("The isolated HTTPS integration test runs only in Simulator")
        #endif
        guard let address = environment["LIFE_TEST_URL"], let token = environment["LIFE_TEST_TOKEN"],
              let pin = environment["LIFE_TEST_PIN"] else {
            throw XCTSkip("Set LIFE_TEST_URL, LIFE_TEST_TOKEN and LIFE_TEST_PIN for the isolated HTTPS test receiver")
        }
        // Never use a user-configured receiver for this integration test.
        guard environment["LIFE_TEST_ISOLATED"] == "1",
              let url = URLComponents(string: address), url.scheme == "https",
              url.host == "127.0.0.1", let port = url.port, port > 1024, port <= 65535,
              port != 8766, url.user == nil, url.password == nil,
              url.query == nil, url.fragment == nil, url.path.isEmpty else {
            throw XCTSkip("Expected explicitly isolated loopback HTTPS receiver on a safe temporary port")
        }
        let priorURL = UserDefaults.standard.string(forKey: "receiverURL")
        let priorPin = UserDefaults.standard.string(forKey: "certificateSHA256")
        let priorToken = Credentials.token()
        defer {
            UserDefaults.standard.set(priorURL, forKey: "receiverURL")
            UserDefaults.standard.set(priorPin, forKey: "certificateSHA256")
            try? Credentials.setToken(priorToken)
        }
        try ReceiverSettings.save(url: address, token: token, pin: pin)
        let journal = RecordingJournal(id: UUID(), startedAt: Date())
        let path = QueueStore.directory.appendingPathComponent(journal.id.uuidString.lowercased() + ".m4a")
        let format = AVAudioFormat(standardFormatWithSampleRate: 16000, channels: 1)!
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 16000)!
        buffer.frameLength = 16000
        for i in 0..<16000 { buffer.floatChannelData![0][i] = Float(sin(Double(i) * 2 * .pi * 440 / 16000)) * 0.1 }
        var file: AVAudioFile? = try AVAudioFile(forWriting: path, settings: [
            AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 16000,
            AVNumberOfChannelsKey: 1, AVEncoderBitRateKey: 32000])
        try file?.write(from: buffer)
        file = nil
        let chunk = try QueueStore.seal(journal, duration: 1)
        let manager = UploadManager.shared
        manager.configurationChanged()
        manager.activate()
        let deadline = Date().addingTimeInterval(45)
        while Date() < deadline && FileManager.default.fileExists(atPath: chunk.manifestURL.path) {
            try await Task.sleep(for: .milliseconds(250))
        }
        XCTAssertFalse(FileManager.default.fileExists(atPath: chunk.manifestURL.path), manager.status)
        XCTAssertFalse(FileManager.default.fileExists(atPath: chunk.audioURL.path), "Delete local audio only after a durable receipt")
    }

    func testReceiverSettingsStayHTTPSAndRejectBadPin() {
        XCTAssertThrowsError(try ReceiverSettings.save(url: "http://your-mac.example.ts.net:8766",
                                                       token: "token", pin: String(repeating: "a", count: 64)))
        XCTAssertThrowsError(try ReceiverSettings.save(url: "https://your-mac.example.ts.net:8766",
                                                       token: "token", pin: "not-a-fingerprint"))
    }

    func testATSExceptionIsExactTailscaleHostWithoutArbitraryLoads() throws {
        let url = Bundle(for: UploadManager.self).url(forResource: "Info", withExtension: "plist")
            ?? Bundle.main.url(forResource: "Info", withExtension: "plist")
        let data = try Data(contentsOf: try XCTUnwrap(url))
        let plist = try XCTUnwrap(PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any])
        let ats = try XCTUnwrap(plist["NSAppTransportSecurity"] as? [String: Any])
        XCTAssertNil(ats["NSAllowsArbitraryLoads"])
        let domains = try XCTUnwrap(ats["NSExceptionDomains"] as? [String: Any])
        // Installations replace the documented example with their exact host.
        // Preserve the security invariant without requiring that example value.
        XCTAssertEqual(domains.count, 1)
        let host = try XCTUnwrap(domains.keys.first)
        XCTAssertTrue(host.hasSuffix(".ts.net"))
        XCTAssertGreaterThan(host.split(separator: ".").count, 2)
        XCTAssertFalse(host.hasPrefix("."))
        XCTAssertFalse(host.contains("*"))
        XCTAssertFalse(host.contains("/"))
        let exception = try XCTUnwrap(domains[host] as? [String: Any])
        XCTAssertEqual(exception["NSIncludesSubdomains"] as? Bool, false)
        XCTAssertEqual(exception["NSExceptionMinimumTLSVersion"] as? String, "TLSv1.2")
        XCTAssertEqual(exception["NSExceptionAllowsInsecureHTTPLoads"] as? Bool, true)
    }
}
