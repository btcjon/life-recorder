import Combine
import CoreLocation
import CoreMotion
import Foundation
import SwiftUI
import UIKit

enum LocationContextPolicy {
    static let freshness: TimeInterval = 5 * 60
    static let retention: TimeInterval = 24 * 60 * 60
    static let movingInterval: TimeInterval = 60
    static let stationaryInterval: TimeInterval = 15 * 60

    static func isFresh(capturedAt: Date, now: Date) -> Bool {
        let age = now.timeIntervalSince(capturedAt)
        return age >= 0 && age <= freshness
    }

    static func maySend(lastSentAt: Date?, now: Date, activity: String, foregroundOneShot: Bool) -> Bool {
        if foregroundOneShot { return true }
        guard let lastSentAt else { return true }
        let interval = activity == "stationary" ? stationaryInterval : movingInterval
        return now.timeIntervalSince(lastSentAt) >= interval
    }

    static func activityLabel(_ activity: String) -> String {
        switch activity {
        case "stationary": return "Stationary"
        case "walking": return "Walking"
        case "vehicle": return "In a vehicle"
        default: return "Activity unknown"
        }
    }

    struct Revocation: Equatable {
        let stopLocation = true
        let stopMotion = true
        let clearUnsentLocations = true
        let forgetLastObservation = true
        let deleteMacHistory = false
    }

    static let revocation = Revocation()

    static func associatedObservationID(id: String?, capturedAt: Date?, clipStart: Date, now: Date) -> String? {
        guard let id, let capturedAt, capturedAt <= clipStart,
              now.timeIntervalSince(capturedAt) >= 0,
              now.timeIntervalSince(capturedAt) <= retention else { return nil }
        return id
    }

    static func viewerURL(_ value: String) -> URL? {
        guard let parts = URLComponents(string: value.trimmingCharacters(in: .whitespacesAndNewlines)),
              parts.scheme?.lowercased() == "https", let host = parts.host, !host.isEmpty,
              parts.user == nil, parts.password == nil, parts.query == nil,
              parts.fragment == nil else { return nil }
        return parts.url
    }
}

struct LocationContextObservation {
    let id: String
    let capturedAt: Date
    let latitude: Double
    let longitude: Double
    let accuracy: Double
    let source: String
    let activity: String?
    let confidence: String?

    var payload: [String: Any] {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        var observation: [String: Any] = ["id": id, "captured_at": formatter.string(from: capturedAt),
            "status": "observed", "latitude": latitude, "longitude": longitude,
            "accuracy_m": accuracy, "source": source]
        if let activity, let confidence {
            observation["activity"] = ["state": activity, "confidence": confidence]
        }
        return ["observation": observation]
    }
}

/// Coordinates stay in memory; the private, expiring transport owns unsent delivery.
final class LocationContext: NSObject, ObservableObject, CLLocationManagerDelegate {
    static let shared = LocationContext()
    @Published private(set) var enabled: Bool
    @Published private(set) var backgroundTrial: Bool
    @Published private(set) var motionEnabled: Bool
    @Published private(set) var status = "Location context is off"
    @Published private(set) var lastObservation: LocationContextObservation?
    @Published private(set) var activityState = "unknown"
    @Published private(set) var knownPlaces: [String] = []
    @Published private(set) var placeLabel: String?
    @Published private(set) var clock = Date()
    @Published private(set) var historyDeletionRequested = false
    private let manager = CLLocationManager()
    private let motion = CMMotionActivityManager()
    private var confidence = "low"
    private var activityCapturedAt: Date?
    private var lastSentAt: Date?
    private var foregroundOneShotPending = false
    private var updating = false
    private var timer: Timer?
    private var observers: [NSObjectProtocol] = []
    private var placeObservationID: String?
    private let defaults = UserDefaults.standard

    private override init() {
        enabled = UserDefaults.standard.bool(forKey: "locationContextEnabled")
        backgroundTrial = UserDefaults.standard.bool(forKey: "locationBackgroundTrial")
        motionEnabled = UserDefaults.standard.bool(forKey: "locationMotionEnabled")
        super.init()
        manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyHundredMeters
        manager.distanceFilter = 100
        manager.pausesLocationUpdatesAutomatically = true
        manager.activityType = .other
        observers.append(NotificationCenter.default.addObserver(forName: UIApplication.didBecomeActiveNotification,
            object: nil, queue: .main) { [weak self] _ in self?.activate() })
        observers.append(NotificationCenter.default.addObserver(forName: UIApplication.willResignActiveNotification,
            object: nil, queue: .main) { [weak self] _ in self?.leavingForeground() })
    }

    var viewerURL: URL {
        LocationContextPolicy.viewerURL(defaults.string(forKey: "locationViewerURL") ?? "")
            ?? URL(string: "https://lr.genr8ive.ai")!
    }

    var freshPlaceLabel: String {
        guard let observation = lastObservation,
              LocationContextPolicy.isFresh(capturedAt: observation.capturedAt, now: clock),
              placeObservationID == observation.id, let placeLabel else { return "Unknown" }
        return placeLabel
    }

    var observationSummary: String {
        guard let observation = lastObservation else { return "No location observed" }
        let age = max(0, clock.timeIntervalSince(observation.capturedAt))
        let minutes = Int(age / 60)
        let ageText = minutes == 0 ? "under a minute ago" : "\(minutes) minute\(minutes == 1 ? "" : "s") ago"
        let fresh = LocationContextPolicy.isFresh(capturedAt: observation.capturedAt, now: clock)
        return "Observed \(ageText) · ±\(Int(observation.accuracy.rounded())) m · \(fresh ? "fresh" : "stale; place unknown")"
    }

    func setViewerURL(_ value: String) -> Bool {
        guard let url = LocationContextPolicy.viewerURL(value) else { return false }
        defaults.set(url.absoluteString, forKey: "locationViewerURL")
        objectWillChange.send()
        return true
    }

    func activate() {
        clock = Date()
        expireCoordinates()
        if timer == nil {
            timer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
                self?.clock = Date()
                self?.expireCoordinates()
            }
        }
        guard enabled else { return }
        // Restoring a saved setting never creates a new permission prompt.
        switch manager.authorizationStatus {
        case .authorizedAlways, .authorizedWhenInUse:
            startCapture()
        case .denied, .restricted:
            revoke()
            status = "Location permission is off"
        case .notDetermined:
            revoke()
            status = "Switch location on to allow access"
        @unknown default:
            revoke()
        }
    }

    func setEnabled(_ value: Bool) {
        if !value { revoke(); return }
        enabled = true
        defaults.set(true, forKey: "locationContextEnabled")
        switch manager.authorizationStatus {
        case .notDetermined:
            status = "Waiting for your location choice"
            manager.requestWhenInUseAuthorization()
        case .authorizedAlways, .authorizedWhenInUse:
            startCapture()
        case .denied, .restricted:
            revoke()
            status = "Allow location in iPhone Settings to use context"
        @unknown default:
            revoke()
        }
    }

    func setBackgroundTrial(_ value: Bool) {
        backgroundTrial = value && enabled
        defaults.set(backgroundTrial, forKey: "locationBackgroundTrial")
        manager.stopUpdatingLocation()
        updating = false
        manager.allowsBackgroundLocationUpdates = backgroundTrial
        manager.showsBackgroundLocationIndicator = backgroundTrial
        if enabled && UIApplication.shared.applicationState == .active { startCapture() }
        if !backgroundTrial && UIApplication.shared.applicationState != .active { stopMotion() }
    }

    func setMotionEnabled(_ value: Bool) {
        motionEnabled = value && enabled
        defaults.set(motionEnabled, forKey: "locationMotionEnabled")
        if motionEnabled { startMotion(allowPrompt: true) } else { stopMotion() }
    }

    func requestOneShot() {
        guard enabled, UIApplication.shared.applicationState == .active,
              manager.authorizationStatus == .authorizedWhenInUse || manager.authorizationStatus == .authorizedAlways else { return }
        foregroundOneShotPending = true
        status = "Getting location once"
        manager.requestLocation()
    }

    private func startCapture() {
        guard UIApplication.shared.applicationState == .active else { return }
        ContextTransport.shared.activate()
        if backgroundTrial && !updating {
            manager.allowsBackgroundLocationUpdates = true
            manager.showsBackgroundLocationIndicator = true
            manager.startUpdatingLocation()
            updating = true
            status = "Background location trial running"
        } else if !backgroundTrial {
            requestOneShot()
        }
        if motionEnabled { startMotion() }
    }

    private func leavingForeground() {
        foregroundOneShotPending = false
        if !backgroundTrial {
            manager.stopUpdatingLocation()
            updating = false
            stopMotion()
        }
    }

    private func startMotion(allowPrompt: Bool = false) {
        guard enabled, motionEnabled, CMMotionActivityManager.isActivityAvailable(),
              UIApplication.shared.applicationState == .active || backgroundTrial else { return }
        if CMMotionActivityManager.authorizationStatus() == .denied || CMMotionActivityManager.authorizationStatus() == .restricted {
            motionEnabled = false
            defaults.set(false, forKey: "locationMotionEnabled")
            stopMotion()
            return
        }
        if CMMotionActivityManager.authorizationStatus() == .notDetermined && !allowPrompt {
            motionEnabled = false
            defaults.set(false, forKey: "locationMotionEnabled")
            return
        }
        motion.startActivityUpdates(to: .main) { [weak self] activity in
            guard let self, self.enabled, self.motionEnabled else { return }
            if CMMotionActivityManager.authorizationStatus() == .denied || CMMotionActivityManager.authorizationStatus() == .restricted {
                self.setMotionEnabled(false)
                return
            }
            guard let activity else { return }
            self.activityState = activity.automotive ? "vehicle" : activity.walking ? "walking" : activity.stationary ? "stationary" : "unknown"
            switch activity.confidence {
            case .high: self.confidence = "high"
            case .medium: self.confidence = "medium"
            default: self.confidence = "low"
            }
            self.activityCapturedAt = Date()
        }
    }

    private func stopMotion() {
        motion.stopActivityUpdates()
        activityState = "unknown"
        confidence = "low"
        activityCapturedAt = nil
    }

    private func revoke() {
        enabled = false
        backgroundTrial = false
        motionEnabled = false
        defaults.set(false, forKey: "locationContextEnabled")
        defaults.set(false, forKey: "locationBackgroundTrial")
        defaults.set(false, forKey: "locationMotionEnabled")
        if LocationContextPolicy.revocation.stopLocation { manager.stopUpdatingLocation() }
        manager.allowsBackgroundLocationUpdates = false
        manager.showsBackgroundLocationIndicator = false
        updating = false
        foregroundOneShotPending = false
        if LocationContextPolicy.revocation.stopMotion { stopMotion() }
        if LocationContextPolicy.revocation.clearUnsentLocations { ContextTransport.shared.clearLocationQueue() }
        if LocationContextPolicy.revocation.forgetLastObservation { lastObservation = nil }
        lastSentAt = nil
        placeLabel = nil
        placeObservationID = nil
        status = "Location context is off; unsent locations cleared"
    }

    private func expireCoordinates() {
        guard let observation = lastObservation else { return }
        if clock.timeIntervalSince(observation.capturedAt) > LocationContextPolicy.retention {
            lastObservation = nil
            placeLabel = nil
            placeObservationID = nil
        }
    }

    func latestObservationIDForClip(at startedAt: Date) -> String? {
        clock = Date()
        expireCoordinates()
        return LocationContextPolicy.associatedObservationID(id: lastObservation?.id, capturedAt: lastObservation?.capturedAt,
                                                            clipStart: startedAt, now: clock)
    }

    /// Optional verified/pinned status projection, supplied by the status client.
    /// Matching the observation ID prevents an old place label from naming a new fix.
    func updatePlaceStatus(_ projection: [String: Any]) {
        placeLabel = nil
        placeObservationID = nil
        if let places = projection["known_places"] as? [[String: Any]] {
            knownPlaces = Array(places.compactMap { ($0["name"] ?? $0["label"]) as? String }.filter { !$0.isEmpty && $0.count <= 100 }.prefix(100))
        }
        let lastKnown = (projection["last_known"] as? [String: Any]) ?? projection
        let place = lastKnown["place"] as? [String: Any]
        if let observed = (lastKnown["observation_id"] ?? lastKnown["id"]) as? String,
           observed == lastObservation?.id, lastKnown["status"] as? String == "known",
           let label = (place?["name"] ?? lastKnown["place_label"]) as? String,
           !label.isEmpty, label.count <= 100 {
            placeObservationID = observed
            placeLabel = label
        }
    }

    func updatePlaceStatus(_ data: Data) {
        guard data.count <= 16_384,
              let projection = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        updatePlaceStatus(projection)
    }

    func deleteHistory() {
        let id = UUID().uuidString.lowercased()
        do {
            let formatter = ISO8601DateFormatter()
            formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            let occurredAt = formatter.string(from: Date())
            let payload = try JSONSerialization.data(withJSONObject: ["id": id, "occurred_at": occurredAt])
            try ContextTransport.shared.enqueue(path: "/v1/location/delete-history", id: id,
                payload: payload, expiresAt: Date().addingTimeInterval(LocationContextPolicy.retention))
            lastObservation = nil
            placeLabel = nil
            placeObservationID = nil
            historyDeletionRequested = true
            status = "Location history deletion requested; waiting for Mac receipt"
        } catch {
            status = "Could not queue history deletion"
        }
    }

    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        guard enabled else { return }
        switch manager.authorizationStatus {
        case .authorizedWhenInUse, .authorizedAlways: startCapture()
        case .denied, .restricted: revoke(); status = "Location permission is off"
        default: break
        }
    }

    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard enabled, let location = locations.last,
              location.horizontalAccuracy.isFinite, location.horizontalAccuracy >= 0,
              CLLocationCoordinate2DIsValid(location.coordinate) else { return }
        let now = Date()
        guard LocationContextPolicy.isFresh(capturedAt: location.timestamp, now: now) else {
            status = "Location fix is stale; place unknown"
            return
        }
        let foreground = UIApplication.shared.applicationState == .active
        guard foreground || backgroundTrial else { return }
        // Core Motion's current state can remain stationary for a long time;
        // its state-change timestamp is not a stale GPS fix.
        let activityFresh = activityCapturedAt != nil
        let activity = motionEnabled && activityFresh ? activityState : "unknown"
        guard LocationContextPolicy.maySend(lastSentAt: lastSentAt, now: now, activity: activity,
                                           foregroundOneShot: foreground && foregroundOneShotPending) else { return }
        let observation = LocationContextObservation(id: UUID().uuidString.lowercased(), capturedAt: location.timestamp,
            latitude: location.coordinate.latitude, longitude: location.coordinate.longitude,
            accuracy: location.horizontalAccuracy, source: foreground ? "foreground" : "background",
            activity: motionEnabled ? activity : nil, confidence: motionEnabled ? (activityFresh ? confidence : "low") : nil)
        do {
            let payload = try JSONSerialization.data(withJSONObject: observation.payload)
            try ContextTransport.shared.enqueue(path: "/v1/location/observations", id: observation.id,
                payload: payload, expiresAt: observation.capturedAt.addingTimeInterval(LocationContextPolicy.retention))
            lastObservation = observation
            lastSentAt = now
            foregroundOneShotPending = false
            clock = now
            placeLabel = nil
            placeObservationID = nil
            status = backgroundTrial ? "Location trial running; observation queued" : "Location observed; queued for Mac"
        } catch {
            foregroundOneShotPending = false
            status = "Could not queue location observation"
        }
    }

    func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        if let error = error as? CLError, error.code == .denied {
            revoke()
            status = "Location permission is off"
            return
        }
        foregroundOneShotPending = false
        status = "Location unavailable; place unknown"
    }
}

struct LocationPanel: View {
    @ObservedObject private var context = LocationContext.shared
    @ObservedObject private var transport = ContextTransport.shared
    @State private var viewerAddress = UserDefaults.standard.string(forKey: "locationViewerURL") ?? "https://lr.genr8ive.ai"
    @State private var addressError = false
    @State private var confirmDeletion = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Place context").font(.headline)
            Toggle("Location context", isOn: Binding(get: { context.enabled }, set: context.setEnabled))
            Toggle("Background battery trial", isOn: Binding(get: { context.backgroundTrial }, set: context.setBackgroundTrial))
                .disabled(!context.enabled)
            Toggle("Motion context", isOn: Binding(get: { context.motionEnabled }, set: context.setMotionEnabled))
                .disabled(!context.enabled)
            Text("Background trial requests roughly 100 m accuracy and may affect battery. It starts while this app is open; iOS may pause it. Motion describes activity, including being in a vehicle; it does not identify a driver.")
                .font(.footnote).foregroundStyle(.secondary)
            Text(context.status).font(.caption)
            Text(context.observationSummary).font(.caption).foregroundStyle(.secondary)
            Text("Phone place: \(context.freshPlaceLabel)")
            if context.motionEnabled { Text(LocationContextPolicy.activityLabel(context.activityState)).font(.caption) }
            Button("Check location once") { context.requestOneShot() }.disabled(!context.enabled)
            Text("\(transport.pendingCount) context requests queued · \(transport.status)")
                .font(.caption).foregroundStyle(.secondary)
            if !context.knownPlaces.isEmpty {
                Text("Known places: \(context.knownPlaces.joined(separator: ", "))").font(.caption)
            }
            Link("Manage known places in viewer", destination: context.viewerURL)
            TextField("HTTPS viewer address", text: $viewerAddress)
                .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
            Button("Save viewer address") { addressError = !context.setViewerURL(viewerAddress) }
            if addressError { Text("Use an HTTPS address without credentials or token parameters.").font(.caption).foregroundStyle(.red) }
            Link("Open viewer", destination: context.viewerURL)
            Text("The viewer uses its own sign-in. Switching location off clears unsent locations; stored Mac history has a separate delete action.")
                .font(.footnote).foregroundStyle(.secondary)
            Button("Delete location history on Mac", role: .destructive) { confirmDeletion = true }
        }
        .alert("Delete location history?", isPresented: $confirmDeletion) {
            Button("Cancel", role: .cancel) { }
            Button("Request deletion", role: .destructive) { context.deleteHistory() }
        } message: { Text("This requests deletion of this phone's stored location observations on the Mac. It does not delete recordings.") }
    }
}
