export type SpeakerTurn = {
  id: string;
  started?: number;
  ended?: number;
  text?: string;
  person_id?: string;
  name?: string;
  suggested_name?: string;
  suggested_person_id?: string;
  speaker_key?: string;
  run_id?: string;
  preserved?: boolean;
  label_source?: string;
  clean_seconds?: number;
};

export type Chunk = {
  id: string;
  transcript?: string;
  speakers?: SpeakerTurn[];
  speaker_turn_count?: number;
  status?: string;
  started_local?: string;
  started?: string;
  duration?: number;
  audio_playable?: boolean;
  audio_pinned?: boolean;
  voice_pending?: boolean;
  identity_revision?: number;
  diarization?: {
    outcome?: string;
    speech_seconds?: number;
    coverage?: number;
    turn_count?: number;
    embedding_count?: number;
    cluster_count?: number;
  };
  diarization_status?: string;
};

export type BlockSpeaker = {
  confirmed?: boolean;
  name?: string;
};

export type SuggestionPart = {
  kind?: string;
  started_local?: string;
  ended_local?: string;
};

export type DisplayBlock = {
  kind?: string;
  id?: string;
  chunk_id?: string;
  chunk_ids?: string[];
  source?: string;
  title?: string;
  started_local?: string;
  ended_local?: string;
  speakers?: BlockSpeaker[];
  summary_state?: string;
  summary?: string;
  summary_coverage?: string;
  preview?: string;
  suggestion?: { parts?: SuggestionPart[] };
  start_chunk_id?: string;
  end_chunk_id?: string;
  revision?: number;
};

export type SpeechEvent = {
  id: string;
  audio_playable?: boolean;
  enhanced_playable?: boolean;
  started_local?: string;
  playable_duration?: number;
  duration?: number;
  chunks?: Array<{ id?: string }>;
};

export type Person = {
  id: string;
  name: string;
  sample_count?: number;
  sample_seconds?: number;
  clip_count?: number;
  enrollment_ready?: boolean;
  enrollment_reasons?: string[];
};

export type IntervalMarker = {
  source?: string;
  label?: string;
  started_at?: string;
  ended_at?: string;
};

export type CaptureSession = {
  title?: string;
  started?: string;
};

export type DayPayload = {
  chunks?: Chunk[];
  display_blocks?: DisplayBlock[];
  events?: SpeechEvent[];
  intervals?: IntervalMarker[];
  sessions?: CaptureSession[];
  people?: Person[];
  pending?: number;
  errors?: number;
  activity_shadow?: { hold_vad_positive?: number };
  identity_revision?: number;
};

export type DaysPayload = {
  days?: string[];
};

export type ReviewPayload = {
  speakers?: SpeakerTurn[];
  voice_pending?: boolean;
  identity_revision?: number;
  diarization?: Chunk["diarization"];
  diarization_status?: string;
  transcript?: string;
  people?: Person[];
};

export type Suggestion = {
  person_id: string;
  name?: string | null;
  score?: number;
  margin?: number;
};

export type ReviewCandidate = {
  turn_id: string;
  group_id?: string;
  group_turn_ids?: string[];
  chunk_id: string;
  speaker_key?: string;
  clip_started?: string;
  started?: number;
  ended?: number;
  audio_usable?: boolean;
  quality?: number | null;
  clean_seconds?: number;
  label_source?: string;
  stored_person_id?: string | null;
  stored_name?: string | null;
  suggestions?: Suggestion[];
  suggestion_score?: number | null;
  suggestion_margin?: number | null;
  reasons?: string[];
  confirmed?: boolean;
};

export type ReviewPage = {
  items?: ReviewCandidate[];
  next_cursor?: string | null;
  identity_revision?: number;
};

export type ReviewDiagnostics = {
  groups?: number;
  rank1_correct_groups?: number;
  rejection_reasons?: { accepted?: number };
};

export type VoiceSample = {
  id: string;
  turn_id?: string;
  chunk_id?: string;
  clip_started?: string;
  duration?: number;
  confirmed_at?: number;
  status?: string;
  legacy?: boolean | number;
};

export type IdentityProfile = Person & {
  samples?: VoiceSample[];
};

export type IdentityProfiles = {
  revision: number;
  people?: IdentityProfile[];
};

export type ClusterMember = {
  turn_id: string;
  chunk_id: string;
  started?: number;
  ended?: number;
  person_id?: string | null;
  label_source?: string | null;
};

export type SpeakerCluster = {
  id: string;
  status?: string;
  members?: ClusterMember[];
  eligible_vectors?: number;
};

export type ClusterProposal = {
  id?: string;
  track_ids: string[];
  score?: number;
  margin?: number;
};

export type ClusterReport = {
  revision?: number;
  clusters?: SpeakerCluster[];
  gate?: { enabled?: boolean; reason?: string };
  proposals?: ClusterProposal[];
  proposal_status?: string;
};

export type IdentityMutationResult = {
  change_id?: string;
  revision?: number;
  kind?: string;
  enrollment?: { enrolled?: boolean; reason?: string };
};

export type LabelTurnsBody = {
  turn_ids: string[];
  person_id: string | null;
  use_sample: boolean;
  revision: number;
};

export type HealthReport = {
  processing?: { pending_count?: number; pending?: number; attention_count?: number; needs_attention?: number };
  queue?: { pending_count?: number; pending?: number; attention_count?: number; needs_attention?: number };
};

export type SelectionRef = {
  kind: "chunk" | "event";
  id: string;
};
