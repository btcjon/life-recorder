import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useCallback, useEffect, useRef, useState } from "react";
import type { ContextClient } from "../context/contracts";
import type { HealthReport } from '../../api/types';

export type HealthSnapshot = HealthReport & {
  queue?: HealthReport['processing'];
  storage?: { low_space?: boolean; original_audio_bytes?: number; audio_usage_complete?: boolean; available_bytes?: number };
  stages?: Record<string, { enabled?: boolean; counts?: Record<string, number>; status?: { state?: string; error_code?: string } }>;
  runtime?: { source_revision?: string; viewer_ui?: { state?: string; delivery?: string; asset_count?: number } };
  agent_index?: { last_reconciled_at?: number };
};
export function healthDetails(report: HealthSnapshot) {
  const queue = report.processing || report.queue || {}, storage = report.storage || {};
  const details = [`Pending: ${Number(queue.pending_count ?? queue.pending ?? 0)}`, `Needs attention: ${Number(queue.attention_count ?? queue.needs_attention ?? 0)}`];
  const processing = report.processing;
  details.unshift(processing?.last_completed_at ? `Last success: ${new Date(processing.last_completed_at * 1000).toLocaleString()}` : 'Last success unavailable');
  details.push(`Engine: ${processing?.engine_ready === true ? 'ready' : processing?.engine_ready === false ? 'unavailable' : 'not checked'}`);
  if (processing?.stalled) details.push('Processing stalled');
  if (processing?.active_operation) details.push(`Active operation: ${processing.active_operation}`);
  if (processing?.backlog_count != null) details.push(`Backlog: ${processing.backlog_count} recordings · ${Math.round((processing.backlog_duration_seconds || 0) / 60)} minutes`);
  if (queue.delayed || Number(queue.oldest_pending_age_seconds || 0) > 600) details.push("Processing delayed — inspect pending recordings");
  if (storage.low_space) details.push("Low disk space — free storage on your Mac");
  if (storage.original_audio_bytes != null) details.push(`Original audio: ${(storage.original_audio_bytes / 1048576).toFixed(1)} MB${storage.audio_usage_complete ? "" : " (partial count)"}`);
  if (storage.available_bytes != null) details.push(`Free space: ${(storage.available_bytes / 1073741824).toFixed(1)} GB`);
  if (queue.last_completed_at) details.push(`Last processed: ${new Date(queue.last_completed_at * 1000).toLocaleString()}`);
  if (report.agent_index?.last_reconciled_at) details.push(`Search index checked: ${new Date(report.agent_index.last_reconciled_at * 1000).toLocaleString()}`);
  details.push(report.runtime?.source_revision ? `Source: ${report.runtime.source_revision.slice(0, 12)} (verified launch release)` : "Source revision unavailable — deploy a verified source release");
  if (report.runtime?.viewer_ui) details.push(`Viewer bundle: ${report.runtime.viewer_ui.state || "unknown"} · ${report.runtime.viewer_ui.asset_count || 0} assets`);
  return details;
}
type SharedHealth = { report: HealthSnapshot | null; error: string; loading: boolean; refresh: () => void };
export function HealthView({ client, shared }: { client: ContextClient; shared?: SharedHealth }) {
  return shared ? <HealthDisplay {...shared} /> : <StandaloneHealth client={client} />;
}
function StandaloneHealth({ client }: { client: ContextClient }) {
  const [report, setReport] = useState<HealthSnapshot | null>(null), [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const generation = useRef(0), abortRef = useRef<AbortController | null>(null);
  const load = useCallback(async () => {
    const id = ++generation.current; abortRef.current?.abort();
    const abort = new AbortController(); abortRef.current = abort; setLoading(true);
    try { const next = await client.getJson<HealthSnapshot>("/v1/health", abort.signal); if (id === generation.current && !abort.signal.aborted) { setReport(next); setError(""); } }
    catch { if (id === generation.current && !abort.signal.aborted) setError("Processing status unavailable. Check that your Mac receiver is reachable. Previous status, if shown, may be stale."); }
    finally { if (id === generation.current) setLoading(false); }
  }, [client]);
  useEffect(() => { const requests = generation; void load(); return () => { requests.current++; abortRef.current?.abort(); }; }, [load]);
  return <HealthDisplay report={report} error={error} loading={loading} refresh={() => void load()} />;
}
function HealthDisplay({report, error, loading, refresh}: SharedHealth) {
  return <div id="health-view" className="h-full overflow-y-auto p-3 md:p-5"><div className="mx-auto max-w-4xl space-y-5">
    <div className="flex flex-wrap items-center justify-between gap-3"><h2 className="text-xl font-semibold">Receiver health</h2><Button disabled={loading} variant="outline" onClick={refresh}>{loading ? "Checking…" : "Refresh health"}</Button></div>
    <p className="text-sm text-muted-foreground">Uploaded, pending, retrying, quiet and unavailable are different states. This screen reads status; it does not retry processing or recompute voice diagnostics.</p>
    {error && <p role="alert" className="text-destructive">{error}</p>}
    {!report && !error && <p>Loading processing status…</p>}
    {report && <><Card><CardHeader><CardTitle>Processing and storage</CardTitle></CardHeader><CardContent><ul className="space-y-2 break-words [overflow-wrap:anywhere]">{healthDetails(report).map(line => <li key={line}>{line}</li>)}</ul></CardContent></Card>
      <Card><CardHeader><CardTitle>Pipeline stages</CardTitle></CardHeader><CardContent className="space-y-3">{["asr", "diarization", "vad", "enhancement", "summaries", "topics"].map(name => {
        const stage = report.stages?.[name]; if (!stage) return null;
        const counts = Object.entries(stage.counts || {}).map(([state, count]) => `${count} ${state}`).join(", ");
        const reason = stage.status?.error_code;
        return <section key={name} className="rounded-md border p-3"><h3 className="font-medium">{name.toUpperCase()}</h3><p>{stage.enabled === false ? "off" : stage.status?.state || counts || "ready"}</p>{stage.status?.state && counts && <p>{counts}</p>}{reason && reason !== "disabled" && <p>{reason} — configure and verify the Grok route where required.</p>}</section>;
      })}</CardContent></Card></>}
  </div></div>;
}
