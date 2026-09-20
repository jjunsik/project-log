export const statuses = ['CONFIRMED', 'INFERRED', 'HYPOTHESIS', 'UNKNOWN'] as const;
export type EvidenceStatus = typeof statuses[number];
export interface Claim {
  id: string; question_id: string; text: string | null; evidence_status: EvidenceStatus;
  record_kind: string; evidence_ids: string[]; unknown_reason: string | null;
}
export interface Draft { title: string; claims: Claim[]; links: {event_id: string; story_key: string}[] }
export interface Golden {
  draft: Draft; important_question_ids: string[]; forbidden_claims: string[]; annotation_notes: string;
}
export interface PilotCase {
  id: string; project_key: string; title: string; purpose: string;
  questions: {id: string; label: string}[];
  evidence: {id: string; repository_key: string; source_type: string; locator: string;
    content: string; occurred_at: string; provenance_note: string}[];
  golden_candidate: Golden;
}
export interface Revision {
  id: string; draft: Draft; mode: string; recorded_at: string; latency_ms: number;
  base_revision_id: string | null; correction: string | null;
}
export interface Candidate { id: string; golden: Golden; golden_hash: string }
export interface Judgment {
  claim_id: string; supported: boolean; content_correct: boolean;
  severe_fabrication: boolean; note: string;
}
export interface CaseState {
  case: PilotCase; revisions: Revision[]; goldens: Candidate[];
  approvals: {id: string; candidate_id: string}[];
  reviews: {id: string; revision_id: string; action: string; note: string; review_status: string}[];
  evaluations: {id: string; mode: string; metrics: Record<string, unknown>}[];
  timing: { active_ms: number; wait_ms: number; overlap_ms: number; paused_ms: number;
    uncertain_sessions: string[] };
  incomplete_runs: string[]; fixture_changed: boolean; case_hash: string;
}
