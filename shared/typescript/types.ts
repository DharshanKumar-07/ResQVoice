export enum HypothesisStatus {
  UNCONFIRMED = 'UNCONFIRMED',
  CORROBORATED = 'CORROBORATED',
  DISPUTED = 'DISPUTED',
  CONFIRMED = 'CONFIRMED',
  REJECTED = 'REJECTED'
}

export enum ActionStatus {
  TODO = 'TODO',
  IN_PROGRESS = 'IN_PROGRESS',
  BLOCKED = 'BLOCKED',
  COMPLETED = 'COMPLETED',
  CANCELLED = 'CANCELLED',
  OVERDUE = 'OVERDUE'
}

export interface Fact {
  id: string;
  description: string;
  source: string;
  speaker: string;
  timestamp: string; // ISO 8601
  status: string;
}

export interface Hypothesis {
  id: string;
  description: string;
  origin: string;
  supporting_evidence: string[];
  contradicting_evidence: string[];
  confidence: number;
  status: HypothesisStatus;
}

export interface Claim {
  id: string;
  text: string;
  speaker: string;
  role: string;
  timestamp: string;
  status: string;
  supporting: string[];
  contradicting: string[];
}

export enum ClaimStatus {
  UNVERIFIED = 'UNVERIFIED',
  UNCONFIRMED = 'UNCONFIRMED',
  CORROBORATED = 'CORROBORATED',
  DISPUTED = 'DISPUTED',
  CONFIRMED = 'CONFIRMED',
  RESOLVED = 'RESOLVED',
  REJECTED = 'REJECTED'
}

export enum EvidenceType {
  SUPPORTING = 'supporting',
  CONTRADICTING = 'contradicting'
}

export enum EvidenceTargetType {
  CLAIM = 'claim',
  HYPOTHESIS = 'hypothesis'
}

export interface Evidence {
  id: string;
  target_id: string;
  target_type?: EvidenceTargetType;
  type: EvidenceType;
  description: string;
  source: string;
}

export interface Conflict {
  id: string;
  topic: string;
  claim_a_id: string;
  claim_b_id: string;
  status: string;
  recommended_verification: string;
}

export interface Unknown {
  id: string;
  description: string;
  status: string;
  source_id?: string;
}

export interface Action {
  id: string;
  task: string;
  owner: string;
  status: ActionStatus;
  priority: string;
  created_at: string;
  deadline?: string;
}

export interface Decision {
  id: string;
  recommendation: string;
  evidence: string[];
  sop_reference: string;
  approved_by?: string;
  approval_time?: string;
  execution_status: string;
  result?: string;
}

export interface TimelineEvent {
  id: string;
  timestamp: string;
  description: string;
  event_type: string;
}

export interface Participant {
  agora_uid: string;
  user_id: string;
  display_name: string;
  role: string;
  participant_type: 'human' | 'ai_agent' | 'system';
  channel: string;
}

export interface Intervention {
  id: string;
  trigger_type: string;
  severity: string;
  confidence: number;
  message: string;
  related_claim_ids: string[];
  created_at: string;
  expires_at?: string | null;
  spoken_at?: string | null;
  status: 'PENDING' | 'DEFERRED' | 'SPOKEN' | 'DISMISSED' | 'EXPIRED';
  agent_id?: string | null;
}

export interface SOPMatch {
  title: string;
  reference: string;
  score: number;
  required_steps: string[];
}

export interface PotentialSOPConflict {
  proposed_action: string;
  sop_reference: string;
  missing_steps: string[];
  out_of_order: boolean;
  recommended_sequence: string[];
}

export interface SafetyRecommendation {
  decision: Decision;
  sop_match: SOPMatch;
  confidence: number;
  conflict?: PotentialSOPConflict;
}
