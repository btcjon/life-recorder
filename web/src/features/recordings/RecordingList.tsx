import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty";
import type { RefObject } from "react";
import type { Chunk } from "../../api/types";
import {
  audioStateLabel,
  clockLabel,
  eventRange,
  eventTitle,
  groupSourceKind,
  groupSourceLabel,
  hasPlayableRows,
  preview,
  recordedLength,
  type ListRow,
} from "../../lib/recordings";

export function RecordingList({
  rows,
  selectedId,
  selectedKind,
  listRef,
  onSelectChunk,
  onSelectGroup,
}: {
  rows: ListRow[];
  selectedId: string | null;
  selectedKind: "chunk" | "event" | "group";
  listRef?: RefObject<HTMLDivElement | null>;
  onSelectChunk: (id: string) => void;
  onSelectGroup: (id: string) => void;
}) {
  return (
    <div ref={listRef} className="h-full min-w-0 overflow-y-auto" data-recording-list>
      <div className="grid min-w-0 gap-2 p-3" aria-label="Recordings">
        {rows.map((row) => {
          if (row.type === "heading") {
            return <h2 key={row.key} className="px-1 pt-2 text-xs font-medium text-muted-foreground">{row.label}</h2>;
          }
          if (row.type === "group") {
            const active = selectedKind === "group" && selectedId === row.key;
            const seconds = row.members.reduce((sum, chunk) => sum + (Number(chunk.duration) || 0), 0);
            const label = groupSourceLabel(row.block);
            const matched = row.members.find((chunk) => chunk.id === row.matchId);
            return (
              <div key={row.key} className="min-w-0 rounded-lg border p-2">
                <Button id={"recording-" + row.key} type="button" variant={active ? "secondary" : "ghost"} className="recording-row touch-target h-auto w-full justify-start whitespace-normal text-left" aria-current={active ? "true" : undefined} onClick={() => onSelectGroup(row.key)}>
                  <span className="min-w-0">
                    <span className="block font-medium">{eventTitle(row.block)}</span>
                    <span className="block text-xs text-muted-foreground">{label} · {eventRange(row.block)} · {row.members.length} recordings · {recordedLength(seconds)}</span>
                    <span className="block truncate text-xs text-muted-foreground">{preview(matched?.transcript || row.block.preview || row.members.find((chunk) => chunk.transcript)?.transcript)}</span>
                  </span>
                </Button>
                <details className="mt-1 px-1">
                  <summary className="touch-target cursor-pointer text-xs text-muted-foreground">Source recordings</summary>
                  <ul className="grid gap-1" aria-label={"Source recordings for " + eventTitle(row.block)}>
                  {row.members.map((chunk) => (
                    <li key={chunk.id}>
                      <button id={"recording-" + chunk.id} type="button" className="touch-target w-full truncate text-left text-xs text-muted-foreground underline" aria-label={"Open source recording " + (chunk.started_local || chunk.started || chunk.id)} onClick={() => onSelectChunk(chunk.id)}>
                        {chunk.started_local || chunk.started || chunk.id}
                      </button>
                      {audioStateLabel(chunk) ? <span className="block text-xs text-muted-foreground">{audioStateLabel(chunk)}</span> : null}
                    </li>
                  ))}
                  </ul>
                </details>
                <Badge variant={groupSourceKind(row.block) === "manual" ? "secondary" : "outline"}>{label}</Badge>
                {groupSourceKind(row.block) === "time" ? <Badge variant="outline">No verified topic</Badge> : null}
              </div>
            );
          }
          return <ChunkButton key={row.key} chunk={row.chunk} active={selectedKind === "chunk" && selectedId === row.chunk.id} onSelect={onSelectChunk} />;
        })}
        {!hasPlayableRows(rows) ? (
          <Empty>
            <EmptyHeader>
              <EmptyTitle>No recordings</EmptyTitle>
              <EmptyDescription>No recordings match this day or search.</EmptyDescription>
            </EmptyHeader>
          </Empty>
        ) : null}
      </div>
    </div>
  );
}

function ChunkButton({ chunk, active, onSelect }: { chunk: Chunk; active: boolean; onSelect: (id: string) => void }) {
  const note = audioStateLabel(chunk);
  return (
    <Button id={"recording-" + chunk.id} type="button" variant={active ? "secondary" : "outline"} className="recording-row touch-target h-auto min-w-0 justify-start whitespace-normal text-left" aria-current={active ? "true" : undefined} onClick={() => onSelect(chunk.id)}>
      <span className="min-w-0">
        <span className="block font-medium">{clockLabel(chunk.started_local || chunk.started) + " · " + recordedLength(Number(chunk.duration || 0))}</span>
        <span className="block truncate text-xs text-muted-foreground">{preview(chunk.transcript)}</span>
        {note ? <span className="block text-xs text-muted-foreground">{note}</span> : null}
      </span>
    </Button>
  );
}
