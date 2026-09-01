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
  claim_id: string;
  type: string;
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
  linked_action_id?: string;
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

export interface TranscriptEvent {
  id: number;
  speaker: string;
  role: string;
  text: string;
  timestamp: string;
}
