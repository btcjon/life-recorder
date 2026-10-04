import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { ContextEvidence } from "./contracts";

export function ObservedContext({ report, error }: { report: ContextEvidence | null; error: string }) {
  return <Card><CardHeader><CardTitle>Observed context</CardTitle></CardHeader><CardContent className="space-y-4 break-words [overflow-wrap:anywhere]">
    <p>{report?.statement || "Overlapping context. This is not a recording source."}</p>
    {error ? <p role="alert">{error}</p> : !report ? <p>Loading observed context…</p> : <>
      <p>{report.browser_adapter?.available ? "Browser media metadata is available." : `Browser metadata is unavailable. ${report.browser_adapter?.note || "YouTube and X playback metadata has not been configured or verified."}`}</p>
      <p>Observer freshness: {report.freshness?.label || "unknown"}. {report.freshness?.detail}</p>
      <h3 className="font-medium">Open capture gaps</h3>
      {!(report.open_capture_gaps || []).length && <p>No open capture gap in this read.</p>}
      {(report.open_capture_gaps || []).map((gap, i) => <section key={`${gap.started_at}:${i}`} className="rounded-md border p-3">
        <p>Open recording gap from {gap.started_at}. {Math.round(gap.duration_seconds || 0)} seconds since this observation; not a final duration. No successful restart has been observed.</p>
        <p>{gap.uncertain ? "The boundary is uncertain." : "The stop is the reported inactive observation."}</p>
        {gap.uncertainties?.includes("no_fresh_observer") && <p>No fresh observer, so this gap stays open and uncertain.</p>}
        {gap.reason && <p>Reported reason, not a cause: {gap.reason}</p>}
      </section>)}
      <h3 className="font-medium">Recovered capture gaps</h3>
      {(report.capture_gaps || []).filter(g => !g.open).map((gap, i) => <p key={`${gap.started_at}:${i}`}>{gap.started_at} → {gap.ended_at} · {Math.round(gap.duration_seconds || 0)} seconds {gap.uncertain && "· uncertain boundary"} {gap.reason && `· reported reason: ${gap.reason}`}</p>)}
      <h3 className="font-medium">Intervals</h3>
      {!report.intervals?.length && <p>No context intervals in this read.</p>}
      {(report.intervals || []).map((item, i) => <section key={`${item.started_at}:${i}`} className="space-y-1 rounded-md border p-3">
        <p className="flex flex-wrap gap-2"><Badge variant="outline">{item.signal}</Badge><Badge variant="secondary">{item.state}</Badge><span>{item.app || "no app"}</span></p>
        <p>{item.started_at} → {item.ended_at || item.effective_until || "open"}</p>
        <p>{[item.fresh ? "fresh" : item.stale ? "stale" : "", !item.boundary_exact ? "boundary not exact" : "", item.uncertain ? "uncertain" : "", item.open_recording_gap ? "open recording gap" : ""].filter(Boolean).join(" · ")}</p>
        {item.precise_transcript_association === false && <p>Clock skew excludes a precise transcript association.</p>}
        {item.title && <p>{item.title}</p>}{item.call_state && <p>Call {item.call_state}</p>}
        {item.reason && <p>Reported reason, not a cause: {item.reason}</p>}
      </section>)}
      {report.truncated && <p>This context read is limited to recent evidence.</p>}
    </>}
  </CardContent></Card>;
}
