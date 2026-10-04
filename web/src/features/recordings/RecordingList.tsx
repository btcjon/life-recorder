import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useRef, useState } from "react";
import type { ApiClient } from "../../api/client";
import { ApiError, eventEditFailureCopy } from "../../api/client";
import { isUnknownSaveOutcome } from "../identitySafety";
import type { Chunk, DayPayload } from "../../api/types";
import {
  audioStateLabel,
  eventTitle,
  hasPlayableRows,
  preview,
  recordedLength,
  summaryStatus,
  suggestionText,
  type ListRow,
} from "../../lib/recordings";

type EditorState = {
  blockId: string;
  manual: boolean;
  editId: string;
  revision: number;
  title: string;
  start: string;
  end: string;
  error: string;
  saving: boolean;
  conflict: boolean;
};

export function RecordingList({
  rows,
  payload,
  client,
  selectedId,
  selectedKind,
  expanded,
  onToggle,
  onSelectChunk,
  onSelectEvent,
  onSaved,
  readOnly = false,
}: {
  rows: ListRow[];
  payload: DayPayload;
  client: ApiClient;
  selectedId: string | null;
  selectedKind: "chunk" | "event";
  expanded: ReadonlySet<string>;
  onToggle: (id: string) => void;
  onSelectChunk: (id: string) => void;
  onSelectEvent: (id: string) => void;
  onSaved: () => Promise<boolean> | boolean | void;
  readOnly?: boolean;
}) {
  const [editor, setEditor] = useState<EditorState | null>(null);
  const writeBusy = useRef(false);
  const options = (payload.chunks || []).filter((chunk) => chunk.started_local || chunk.started);

  async function save(current: EditorState) {
    if (readOnly || writeBusy.current || current.conflict || current.saving) return;
    if (!current.start || !current.end || !current.title.trim()) {
      setEditor({ ...current, error: "Enter a title and both recording boundaries." });
      return;
    }
    writeBusy.current = true;
    setEditor({ ...current, saving: true, error: "" });
    const body: Record<string, unknown> = {
      title: current.title,
      start_chunk_id: current.start,
      end_chunk_id: current.end,
      expected_revision: current.revision,
    };
    if (current.manual && current.editId) body.id = current.editId;
    try {
      await client.saveEventEdit(body);
      setEditor(null);
      onSaved();
    } catch (error) {
      const uncertain = isUnknownSaveOutcome(error);
      setEditor({ ...current, saving: false, conflict: uncertain || error instanceof ApiError && error.status === 409,
        error: uncertain ? "The save outcome is uncertain. Reload current boundaries before another save; nothing was retried." : eventEditFailureCopy(error) });
    } finally { writeBusy.current = false; }
  }

  return (
    <ScrollArea className="h-full">
      <div className="grid gap-2 p-3" aria-label="Recordings">
        {rows.map((row) => {
          if (row.type === "interval") {
            return (
              <div key={row.key} className="rounded-lg border px-3 py-2 text-sm">
                <Badge variant={row.manual ? "secondary" : "outline"}>{row.label}</Badge>
                <p className="mt-1 text-xs text-muted-foreground">{row.when}</p>
              </div>
            );
          }
          if (row.type === "session") {
            return <p key={row.key} className="px-1 text-xs text-muted-foreground">{row.text}</p>;
          }
          if (row.type === "speech") {
            const active = selectedKind === "event" && selectedId === row.event.id;
            return (
              <Button key={row.key} id={"speech-" + row.event.id} type="button" variant={active ? "secondary" : "outline"} className="touch-target h-auto justify-start whitespace-normal text-left" aria-current={active ? "true" : undefined} onClick={() => onSelectEvent(row.event.id)}>
                <span>
                  <span className="block font-medium">Speech event · {row.event.started_local || row.event.id}</span>
                  <span className="block text-xs text-muted-foreground">{row.event.enhanced_playable ? "Original and enhanced" : row.event.audio_playable ? "Original only" : "Audio unavailable"}</span>
                </span>
              </Button>
            );
          }
          if (row.type === "event") {
            const open = expanded.has(row.key);
            const seconds = row.members.reduce((sum, chunk) => sum + (Number(chunk.duration) || 0), 0);
            const speakers = (row.block.speakers || []).filter((person) => person.confirmed && person.name);
            const editing = editor?.blockId === row.key;
            return (
              <Card key={row.key} className="gap-2 p-3">
                <button type="button" className="touch-target w-full text-left" aria-expanded={open} onClick={() => onToggle(row.key)}>
                  <div className="font-medium">{eventTitle(row.block)}</div>
                  <div className="text-xs text-muted-foreground">{(row.block.source === "manual" ? "Edited" : "Suggested") + " · " + row.members.length + " clips · " + recordedLength(seconds)}</div>
                  {speakers.length ? (
                    <div className="mt-2 flex flex-wrap gap-1">{speakers.map((person) => <Badge key={person.name} variant="secondary">{person.name}</Badge>)}</div>
                  ) : <div className="mt-1 text-xs text-muted-foreground">No named speakers yet.</div>}
                  {row.block.summary_state === "ready" && row.block.summary ? (
                    <>
                      {row.block.summary_coverage === "sampled" ? <div className="mt-1 text-xs text-muted-foreground">Partial summary.</div> : null}
                      <p className="mt-1 line-clamp-2 text-sm">{row.block.summary}</p>
                    </>
                  ) : (
                    <>
                      <p className="mt-1 truncate text-xs text-muted-foreground">Excerpt · {preview(row.block.preview || row.members.find((chunk) => chunk.transcript)?.transcript)}</p>
                      <p className="text-xs text-muted-foreground">{summaryStatus(row.block)}</p>
                    </>
                  )}
                  {suggestionText(row.block) ? <p className="mt-1 text-xs text-muted-foreground">Suggested: {suggestionText(row.block)}</p> : null}
                </button>
                <Button type="button" size="sm" variant="outline" className="touch-target" disabled={readOnly || editor?.saving || editing && editor?.conflict} aria-label={row.block.source === "manual" ? "Edit event title and boundaries" : "Edit this suggested event"} onClick={() => setEditor(editing ? null : {
                  blockId: row.key,
                  manual: row.block.source === "manual",
                  editId: row.block.source === "manual" ? row.key : "",
                  revision: row.block.source === "manual" ? Number(row.block.revision || 0) : 0,
                  title: row.block.source === "manual" ? row.block.title || "" : "",
                  start: row.block.start_chunk_id || "",
                  end: row.block.end_chunk_id || "",
                  error: "",
                  saving: false,
                  conflict: false,
                })}>
                  {editing ? "Close" : "Edit"}
                </Button>
                {editing && editor ? (
                  <form className="grid gap-2" onSubmit={(event) => { event.preventDefault(); void save(editor); }}>
                    <label className="grid gap-1 text-xs text-muted-foreground">Title
                      <Input aria-label="Event title" maxLength={80} value={editor.title} onChange={(event) => setEditor({ ...editor, title: event.target.value })} />
                    </label>
                    <label className="grid gap-1 text-xs text-muted-foreground">Starts with
                      <select aria-label="Starts with" className="h-8 rounded-lg border bg-transparent px-2" value={editor.start} onChange={(event) => setEditor({ ...editor, start: event.target.value })}>
                        {options.map((chunk) => <option key={chunk.id} value={chunk.id}>{chunk.started_local || chunk.started || "Recording"}</option>)}
                      </select>
                    </label>
                    <label className="grid gap-1 text-xs text-muted-foreground">Ends with
                      <select aria-label="Ends with" className="h-8 rounded-lg border bg-transparent px-2" value={editor.end} onChange={(event) => setEditor({ ...editor, end: event.target.value })}>
                        {options.map((chunk) => <option key={chunk.id} value={chunk.id}>{chunk.started_local || chunk.started || "Recording"}</option>)}
                      </select>
                    </label>
                    <Button type="submit" size="sm" disabled={readOnly || editor.saving || editor.conflict}>{editor.saving ? "Saving" : "Save"}</Button>
                    {editor.error ? <p role="alert" className="text-xs text-destructive">{editor.error}</p> : null}
                    {editor.conflict && <Button type="button" variant="outline" disabled={readOnly || editor.saving} onClick={async () => { const ok = await onSaved(); if (ok === true) setEditor(null); }}>Reload current boundaries</Button>}
                  </form>
                ) : null}
                {open ? row.members.map((chunk) => <ChunkButton key={chunk.id} chunk={chunk} active={selectedKind === "chunk" && selectedId === chunk.id} onSelect={onSelectChunk} />) : null}
              </Card>
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
    </ScrollArea>
  );
}

function ChunkButton({ chunk, active, onSelect }: { chunk: Chunk; active: boolean; onSelect: (id: string) => void }) {
  const note = audioStateLabel(chunk);
  return (
    <Button id={"recording-" + chunk.id} type="button" variant={active ? "secondary" : "outline"} className="touch-target h-auto justify-start whitespace-normal text-left" aria-current={active ? "true" : undefined} onClick={() => onSelect(chunk.id)}>
      <span className="min-w-0">
        <span className="block font-medium">{(chunk.started_local || chunk.started || "Recording") + " · " + Number(chunk.duration || 0).toFixed(0) + "s"}</span>
        <span className="block truncate text-xs text-muted-foreground">{preview(chunk.transcript)}</span>
        {note ? <span className="block text-xs text-muted-foreground">{note}</span> : null}
      </span>
    </Button>
  );
}
