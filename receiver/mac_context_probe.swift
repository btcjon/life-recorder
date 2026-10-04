import CoreAudio
import Foundation

// Public HAL process output-IO probe. IsRunningOutput means active output IO,
// not audible sound. This process does not open a tap, record, or set properties.

let maxProcesses = 64
let bundlePattern = try! NSRegularExpression(pattern: "^[A-Za-z0-9._-]{1,200}$")

func emit(_ payload: [String: Any]) -> Never {
    let data = try! JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
    exit(0)
}

func fail(_ reason: String) -> Never {
    emit(["version": 1, "ok": false, "reason": reason])
}

func address(_ selector: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(
        mSelector: selector,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
}

func processIDs() -> [AudioObjectID]? {
    var property = address(kAudioHardwarePropertyProcessObjectList)
    var size: UInt32 = 0
    let system = AudioObjectID(kAudioObjectSystemObject)
    guard AudioObjectGetPropertyDataSize(system, &property, 0, nil, &size) == noErr else {
        return nil
    }
    let count = Int(size) / MemoryLayout<AudioObjectID>.size
    guard count >= 0, count <= 512 else { return nil }
    var ids = [AudioObjectID](repeating: 0, count: count)
    var mutable = size
    guard AudioObjectGetPropertyData(system, &property, 0, nil, &mutable, &ids) == noErr else {
        return nil
    }
    return ids
}

func runningOutput(_ objectID: AudioObjectID) -> Bool? {
    var property = address(kAudioProcessPropertyIsRunningOutput)
    var value: UInt32 = 0
    var size = UInt32(MemoryLayout<UInt32>.size)
    guard AudioObjectGetPropertyData(objectID, &property, 0, nil, &size, &value) == noErr else {
        return nil
    }
    return value == 1
}

func bundleID(_ objectID: AudioObjectID) -> String? {
    var property = address(kAudioProcessPropertyBundleID)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(objectID, &property, 0, nil, &size) == noErr, size > 0 else {
        return nil
    }
    let raw = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: MemoryLayout<CFString?>.alignment)
    defer { raw.deallocate() }
    var mutable = size
    guard AudioObjectGetPropertyData(objectID, &property, 0, nil, &mutable, raw) == noErr else {
        return nil
    }
    guard let unmanaged = raw.load(as: Unmanaged<CFString>?.self) else { return nil }
    let text = unmanaged.takeRetainedValue() as String
    let range = NSRange(location: 0, length: (text as NSString).length)
    // Command-line/system output may legitimately have no bundle identity.
    // Preserve unattributed output activity without storing PID or process paths.
    guard bundlePattern.firstMatch(in: text, range: range) != nil else { return "unidentified_process" }
    return text
}

let ids = processIDs()
guard let ids else { fail("coreaudio_unavailable") }
var seen = Set<String>()
for objectID in ids {
    guard let active = runningOutput(objectID) else { fail("process_query_incomplete") }
    if !active { continue }
    guard let bundle = bundleID(objectID) else { fail("bundle_query_incomplete") }
    seen.insert(bundle)
    if seen.count > maxProcesses { fail("process_coverage_incomplete") }
}
let processes = seen.sorted().map { ["bundle_id": $0, "output_io": true] }
emit(["version": 1, "ok": true, "processes": processes])
