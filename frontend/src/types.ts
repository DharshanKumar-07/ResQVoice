export type HypothesisStatus = 
  | 'UNCONFIRMED'
  | 'CORROBORATED'
  | 'DISPUTED'
  | 'CONFIRMED'
  | 'REJECTED';

export type ActionStatus = 
  | 'TODO'
  | 'IN_PROGRESS'
  | 'BLOCKED'
  | 'COMPLETED'
  | 'CANCELLED'
  | 'OVERDUE';

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

export interface Evidence {
  id: string;
  target_id: string;
  target_type?: 'claim' | 'hypothesis';
  type: 'supporting' | 'contradicting';
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

export interface TranscriptEvent {
  id: number;
  speaker: string;
  role: string;
  text: string;
  timestamp: string;
}
