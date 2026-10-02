// Offline JSON-lines bridge. No asset requests or alternative models.
import Foundation
import NaturalLanguage

func emit(_ value: [String: Any]) {
    let data = try! JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    print(String(data: data, encoding: .utf8)!)
    fflush(stdout)
}

let revision = NLEmbedding.currentSentenceEmbeddingRevision(for: .english)
let supported = NLEmbedding.supportedSentenceEmbeddingRevisions(for: .english)
guard let model = NLEmbedding.sentenceEmbedding(for: .english, revision: revision) else {
    emit(["status": "unavailable", "model": "Apple NaturalLanguage NLEmbedding English sentence",
          "revision": revision, "supported_revisions": Array(supported),
          "os": ProcessInfo.processInfo.operatingSystemVersionString])
    exit(2)
}
emit(["status": "ready", "model": "Apple NaturalLanguage NLEmbedding English sentence",
      "revision": model.revision, "dimension": model.dimension,
      "supported_revisions": Array(supported),
      "os": ProcessInfo.processInfo.operatingSystemVersionString])
while let line = readLine() {
    do {
        let request = try JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any]
        guard let text = request?["text"] as? String else {
            emit(["status": "error", "error": "text_required"])
            continue
        }
        let started = ProcessInfo.processInfo.systemUptime
        guard let vector = model.vector(for: text) else {
            emit(["status": "error", "error": "vector_unavailable"])
            continue
        }
        emit(["status": "ok", "vector": vector,
              "embedding_ms": (ProcessInfo.processInfo.systemUptime - started) * 1000])
    } catch {
        emit(["status": "error", "error": "invalid_json"])
    }
}
