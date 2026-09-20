import SwiftUI
import UIKit

final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(_ application: UIApplication,
                     handleEventsForBackgroundURLSession identifier: String,
                     completionHandler: @escaping () -> Void) {
        guard identifier == UploadManager.sessionID else { completionHandler(); return }
        UploadManager.shared.backgroundCompletion = completionHandler
        UploadManager.shared.activate()
    }
}

@main
struct LifeRecorderApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var recorder = Recorder()
    @StateObject private var uploads = UploadManager.shared
    @Environment(\.scenePhase) private var scenePhase
    @State private var showSettings = false
    @State private var pairingError: String?

    var body: some Scene {
        WindowGroup {
            ContentView(recorder: recorder, uploads: uploads, showSettings: $showSettings)
                .task { uploads.activate(); await recorder.applySchedule() }
                .onChange(of: scenePhase) { _, phase in
                    if phase == .active {
                        uploads.activate()
                        Task { await recorder.applySchedule() }
                    }
                }
                .onOpenURL { url in
                    guard url.scheme == "liferecorder", url.host == "pair",
                          let parts = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return }
                    let query = Dictionary(parts.queryItems?.map { ($0.name, $0.value ?? "") } ?? [],
                                           uniquingKeysWith: { _, last in last })
                    do {
                        try ReceiverSettings.save(url: query["url"] ?? "", token: query["token"] ?? "",
                                                  pin: query["pin"] ?? "")
                        uploads.configurationChanged()
                        showSettings = true
                    } catch { pairingError = error.localizedDescription }
                }
                .alert("Could not pair", isPresented: Binding(get: { pairingError != nil },
                    set: { if !$0 { pairingError = nil } })) {
                        Button("OK") { pairingError = nil }
                    } message: { Text(pairingError ?? "") }
        }
    }
}

struct ContentView: View {
    @ObservedObject var recorder: Recorder
    @ObservedObject var uploads: UploadManager
    @Binding var showSettings: Bool
    @State private var changing = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 28) {
                    VStack(alignment: .leading, spacing: 12) {
                        Text("Keep the context.").font(.system(size: 36, weight: .semibold, design: .rounded))
                        Text("Your phone records. Your Mac turns it into text.")
                            .font(.title3).foregroundStyle(.secondary)
                    }
                    VStack(spacing: 20) {
                        Image(systemName: recorder.recording ? "waveform" : "mic")
                            .font(.system(size: 52)).foregroundStyle(recorder.recording ? .red : .secondary)
                            .frame(height: 70)
                        Text(recorder.status).font(.headline).multilineTextAlignment(.center)
                        Button {
                            changing = true
                            Task { await recorder.setEnabled(!recorder.enabled); changing = false }
                        } label: {
                            Label(recorder.enabled ? "Switch recorder off" : "Switch recorder on",
                                  systemImage: recorder.enabled ? "stop.fill" : "mic.fill")
                                .font(.headline).frame(maxWidth: .infinity).padding(.vertical, 12)
                        }
                        .buttonStyle(.borderedProminent).tint(recorder.enabled ? .red : .indigo)
                        .disabled(changing).accessibilityIdentifier("recorderToggle")
                    }
                    .padding(24).frame(maxWidth: .infinity)
                    .background(.quaternary, in: RoundedRectangle(cornerRadius: 28))
                    VStack(alignment: .leading, spacing: 12) {
                        Label("\(uploads.pendingCount) clips waiting to upload", systemImage: "tray")
                        Text(uploads.status).foregroundStyle(.secondary)
                        if let date = uploads.lastUploadedAt {
                            Text("Last upload: \(date.formatted(date: .omitted, time: .shortened))")
                                .font(.footnote).foregroundStyle(.secondary)
                        }
                        if recorder.incompleteClips > 0 {
                            Text("\(recorder.incompleteClips) interrupted clips need recovery. They remain on this phone.")
                                .foregroundStyle(.orange)
                        }
                    }
                    Text("Records with the screen locked. Pauses 10:00 PM-5:00 AM Eastern. After restarting the phone or force-quitting, open this app once to resume. Switching off stays off.")
                        .font(.footnote).foregroundStyle(.secondary)
                }.padding(24)
            }
            .navigationTitle("Life Recorder").navigationBarTitleDisplayMode(.inline)
            .toolbar { Button("Pair Mac", systemImage: "laptopcomputer") { showSettings = true } }
            .sheet(isPresented: $showSettings) { PairingView(uploads: uploads) }
        }
    }
}

struct PairingView: View {
    @ObservedObject var uploads: UploadManager
    @Environment(\.dismiss) private var dismiss
    @State private var address = UserDefaults.standard.string(forKey: "receiverURL") ?? ""
    @State private var token = Credentials.token()
    @State private var pin = UserDefaults.standard.string(forKey: "certificateSHA256") ?? ""
    @State private var error: String?

    var body: some View {
        NavigationStack {
            Form {
                Section("Your Mac receiver") {
                    TextField("https://your-mac:8765", text: $address)
                        .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                    SecureField("Pairing token", text: $token)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                    TextField("Certificate fingerprint", text: $pin, axis: .vertical)
                        .font(.caption.monospaced()).textInputAutocapitalization(.never).autocorrectionDisabled()
                }
                Section {
                    Text("Uploads use Wi-Fi or cellular. When your Mac is unreachable, audio stays here until it can be uploaded.")
                    Text("Use the pairing link generated on your Mac to fill these fields. For cellular access, use a private VPN or another reachable HTTPS address.")
                }
                if let error { Section { Text(error).foregroundStyle(.red) } }
            }
            .navigationTitle("Pair your Mac")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        do {
                            try ReceiverSettings.save(url: address, token: token, pin: pin)
                            uploads.configurationChanged()
                            dismiss()
                        } catch { self.error = error.localizedDescription }
                    }
                }
            }
        }
    }
}
