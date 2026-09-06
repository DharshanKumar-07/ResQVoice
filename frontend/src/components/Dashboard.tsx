import { useEffect, useState } from 'react';
import axios from 'axios';
import { BACKEND_URL } from '../lib/backend';
import {
  Activity,
  ArrowRight,
  AlertOctagon,
  AlertTriangle,
  BadgeCheck,
  CheckCircle2,
  Database,
  Eye,
  CircleHelp,
  Clock3,
  FileText,
  History,
  Link2,
  ListTodo,
  MessageSquare,
  Pause,
  Play,
  Radio,
  RefreshCw,
  RotateCcw,
  SearchCheck,
  ShieldCheck,
  ShieldAlert,
  SkipForward,
  Sparkles,
  TestTube2,
  UserRound,
  Volume2,
  VolumeX,
  Wrench,
  XCircle,
} from 'lucide-react';
import type {
  Action,
  Claim,
  Conflict,
  Decision,
  Fact,
  Hypothesis,
  SafetyRecommendation,
  TranscriptEvent,
  Unknown,
} from '../types';
import { buildMockReplayState, MOCK_PAYMENT_OUTAGE, MOCK_REPLAY_STAGES } from '../data/mockIncident';

interface State {
  facts: Fact[];
  hypotheses: Hypothesis[];
  actions: Action[];
  decisions: Decision[];
  claims: Claim[];
  conflicts: Conflict[];
  unknowns: Unknown[];
  agent_runtime: AgentRuntime | null;
  agent_activity: AgentActivityItem[];
  transcripts: TranscriptEvent[];
}

interface IncidentReport {
  status: string;
  summary: string;
  markdown: string;
}

interface AgentRuntime {
  timestamp: string;
  objective: string;
  plan: Array<{ step: string; status: string }>;
  activity: string[];
  tool_call: { tool: string; mode: string; result: Record<string, unknown> };
  tool_calls?: Array<{ tool: string; mode: string; result: Record<string, unknown> }>;
  diagnosis?: string;
  remediation?: { action: string; status: string; mode: string };
  recovery_check?: { tool: string; mode: string; result: Record<string, unknown> } | null;
  recommended_owner?: { name: string; role: string } | null;
  hypothesis_updates?: Array<{
    hypothesis_id: string;
    previous_confidence: number;
    new_confidence: number;
    evidence_overlap: string[];
  }>;
  next_question: string;
}

interface AgentActivityItem {
  id: string;
  timestamp: string;
  phase: string;
  title: string;
  detail: string;
  status: 'active' | 'complete';
}

const EMPTY_STATE: State = {
  facts: [],
  hypotheses: [],
  actions: [],
  decisions: [],
  claims: [],
  conflicts: [],
  unknowns: [],
  agent_runtime: null,
  agent_activity: [],
  transcripts: [],
};

type SemanticStatus = 'confirmed' | 'corroborated' | 'disputed' | 'unknown' | 'critical';

function semanticStatus(status: string): SemanticStatus {
  const normalized = status.toUpperCase().replaceAll('_', '-');
  if (normalized.includes('CRITICAL') || normalized === 'OVERDUE') return 'critical';
  if (normalized === 'DISPUTED' || normalized === 'REJECTED' || normalized === 'BLOCKED') return 'disputed';
  if (normalized === 'CORROBORATED' || normalized === 'IN-PROGRESS') return 'corroborated';
  if (['CONFIRMED', 'DECLARED', 'COMPLETED', 'APPROVED'].includes(normalized)) return 'confirmed';
  return 'unknown';
}

function StatusBadge({ status, label }: { status: string; label?: string }) {
  const kind = semanticStatus(status);
  const Icon = kind === 'confirmed'
    ? CheckCircle2
    : kind === 'corroborated'
      ? Link2
      : kind === 'disputed'
        ? XCircle
        : kind === 'critical'
          ? AlertOctagon
          : CircleHelp;

  return (
    <span className={`status-badge status-${kind}`}>
      <Icon size={11} aria-hidden="true" />
      {label ?? status.replaceAll('_', ' ')}
    </span>
  );
}

function formatTime(timestamp?: string) {
  if (!timestamp) return '—';
  return new Date(timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function formatToolValue(value: unknown) {
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  return String(value);
}

export default function Dashboard() {
  const [state, setState] = useState<State | null>(null);
  const [loading, setLoading] = useState(true);
  const [useMockData, setUseMockData] = useState(false);
  const [demoReplayActive, setDemoReplayActive] = useState(false);
  const [demoReplayPlaying, setDemoReplayPlaying] = useState(false);
  const [demoReplayStep, setDemoReplayStep] = useState(0);
  const [demoReplayVoiceEnabled, setDemoReplayVoiceEnabled] = useState(true);
  const [demoReplayRun, setDemoReplayRun] = useState(0);
  const [isResetting, setIsResetting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [proposedAction, setProposedAction] = useState('Restart only pricing-servlet v2.18.4 and drain orphaned database sessions');
  const [actionEvidence, setActionEvidence] = useState(
    'Payment API error rate reached 38.2%\n' +
    'Pricing servlet logs show DB_POOL_ACQUIRE_TIMEOUT\n' +
    'Core payment worker health checks remain green',
  );
  const [completedSteps, setCompletedSteps] = useState(
    'Confirm the incident symptoms and identify the affected production service and deployment.\n' +
    'Capture the current release version, error rate, latency, and customer-impact baseline.\n' +
    'Identify and validate the last known-good release and confirm rollback compatibility.',
  );
  const [approverName, setApproverName] = useState('Priya Nair — Incident Commander');
  const [verificationMetric, setVerificationMetric] = useState('Error rate below 1%, p95 latency below 500 ms, synthetic checkout passing');
  const [verificationFactId, setVerificationFactId] = useState('');
  const [safetyResult, setSafetyResult] = useState<SafetyRecommendation | null>(null);
  const [busyDecisionId, setBusyDecisionId] = useState<string | null>(null);
  const [agentCommand, setAgentCommand] = useState<string | null>(null);
  const [incidentReport, setIncidentReport] = useState<IncidentReport | null>(null);
  const [visibleAgentStage, setVisibleAgentStage] = useState(4);

  const startGuidedReplay = () => {
    setUseMockData(true);
    setDemoReplayActive(true);
    setDemoReplayStep(0);
    setDemoReplayPlaying(true);
    setDemoReplayRun(run => run + 1);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  };

  const toggleMockData = () => {
    const enabled = !useMockData;
    setUseMockData(enabled);
    if (!enabled) {
      setDemoReplayActive(false);
      setDemoReplayPlaying(false);
      window.speechSynthesis?.cancel();
    }
  };

  const toggleReplayVoice = () => {
    setDemoReplayVoiceEnabled(enabled => {
      if (enabled) window.speechSynthesis?.cancel();
      return !enabled;
    });
  };

  const fetchState = async () => {
    try {
      const [stateResponse, transcriptResponse] = await Promise.all([
        axios.get(`${BACKEND_URL}/api/state`),
        axios.get<TranscriptEvent[]>(`${BACKEND_URL}/api/transcripts`, {
          params: { limit: 50 },
        }),
      ]);
      setState(previous => {
        const transcriptsById = new Map(
          (previous?.transcripts ?? []).map(item => [item.id, item]),
        );
        transcriptResponse.data.forEach(item => transcriptsById.set(item.id, item));
        return {
          ...EMPTY_STATE,
          ...stateResponse.data,
          transcripts: Array.from(transcriptsById.values())
            .sort((a, b) => a.id - b.id)
            .slice(-50),
        };
      });
      setError(null);
    } catch (fetchError) {
      console.error('Failed to fetch incident state:', fetchError);
      setError(`Unable to connect to the command engine at ${BACKEND_URL}.`);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    // Initial synchronization is the effect's external-system responsibility.
    // oxlint-disable-next-line react/set-state-in-effect
    void fetchState();
    const interval = window.setInterval(fetchState, 2_000);
    return () => window.clearInterval(interval);
  }, []);

  useEffect(() => {
    const eventSource = new EventSource(`${BACKEND_URL}/api/transcripts/stream`);
    eventSource.onmessage = event => {
      try {
        const transcript = JSON.parse(event.data) as TranscriptEvent;
        setState(previous => {
          const current = previous ?? EMPTY_STATE;
          const transcriptsById = new Map(current.transcripts.map(item => [item.id, item]));
          transcriptsById.set(transcript.id, transcript);
          return {
            ...current,
            transcripts: Array.from(transcriptsById.values())
              .sort((a, b) => a.id - b.id)
              .slice(-50),
          };
        });
      } catch (streamError) {
        console.warn('Ignoring malformed transcript stream event:', streamError);
      }
    };
    return () => eventSource.close();
  }, []);

  useEffect(() => {
    if (!demoReplayActive || !demoReplayPlaying || demoReplayStep >= MOCK_REPLAY_STAGES.length - 1) return;
    const timer = window.setTimeout(() => {
      setDemoReplayStep(step => Math.min(step + 1, MOCK_REPLAY_STAGES.length - 1));
    }, 7_500);
    return () => window.clearTimeout(timer);
  }, [demoReplayActive, demoReplayPlaying, demoReplayStep]);

  useEffect(() => {
    if (!demoReplayActive || !demoReplayPlaying || !demoReplayVoiceEnabled || !('speechSynthesis' in window)) return;

    const synth = window.speechSynthesis;
    const utterance = new SpeechSynthesisUtterance(MOCK_REPLAY_STAGES[demoReplayStep].speech);
    const voices = synth.getVoices();
    utterance.voice = voices.find(voice =>
      /^en/i.test(voice.lang) && /Aria|Jenny|Samantha|Zira|Female|Google UK English Female/i.test(voice.name),
    ) ?? voices.find(voice => /^en/i.test(voice.lang)) ?? null;
    utterance.lang = 'en-US';
    utterance.rate = 1.04;
    utterance.pitch = 1.02;
    utterance.volume = 1;

    synth.cancel();
    const startTimer = window.setTimeout(() => synth.speak(utterance), 220);
    return () => {
      window.clearTimeout(startTimer);
      synth.cancel();
    };
  }, [demoReplayActive, demoReplayPlaying, demoReplayRun, demoReplayStep, demoReplayVoiceEnabled]);

  const handleResetWorkspace = async () => {
    const confirmed = window.confirm(
      'Reset Workspace will permanently clear all live transcripts and incident state. Continue?',
    );
    if (!confirmed) return;

    setIsResetting(true);
    try {
      await axios.post(`${BACKEND_URL}/api/workspace/reset`);
      setUseMockData(false);
      setDemoReplayActive(false);
      setDemoReplayPlaying(false);
      setState(EMPTY_STATE);
      await fetchState();
    } catch (resetError) {
      console.error('Workspace reset failed:', resetError);
      setError('Workspace reset failed. The backend store may not be empty.');
    } finally {
      setIsResetting(false);
    }
  };

  const lines = (value: string) => value
    .split('\n')
    .map(item => item.trim())
    .filter(Boolean);

  const handleRecommend = async () => {
    setBusyDecisionId('recommend');
    try {
      const response = await axios.post<SafetyRecommendation>(
        `${BACKEND_URL}/api/safety/recommend`,
        {
          proposed_action: proposedAction,
          evidence: lines(actionEvidence),
          completed_steps: lines(completedSteps),
        },
      );
      setSafetyResult(response.data);
      await fetchState();
    } catch (safetyError) {
      console.error('SOP recommendation failed:', safetyError);
      setError('The safety recommendation could not be created.');
    } finally {
      setBusyDecisionId(null);
    }
  };

  const runAgentCommand = async (command: 'summary' | 'review_gaps' | 'review_actions') => {
    setAgentCommand(command);
    try {
      await axios.post(`${BACKEND_URL}/api/agent/command`, { command });
      setError(null);
      await fetchState();
    } catch (commandError) {
      console.error('Agent command failed:', commandError);
      setError('Join the Voice Room first, or wait for the previous agent message cooldown to finish.');
    } finally {
      setAgentCommand(null);
    }
  };

  const generateIncidentReport = async () => {
    setAgentCommand('report');
    try {
      const response = await axios.get<IncidentReport>(`${BACKEND_URL}/api/agent/report`);
      setIncidentReport(response.data);
      setError(null);
      await fetchState();
    } catch (reportError) {
      console.error('Incident report generation failed:', reportError);
      setError('The incident handoff report could not be generated.');
    } finally {
      setAgentCommand(null);
    }
  };

  const runAutonomousCycle = async () => {
    setAgentCommand('cycle');
    try {
      await axios.post(`${BACKEND_URL}/api/agent/cycle`, { reason: 'command_center_demo' });
      setError(null);
      await fetchState();
    } catch (cycleError) {
      console.error('Autonomous agent cycle failed:', cycleError);
      setError('The autonomous agent cycle could not be completed.');
    } finally {
      setAgentCommand(null);
    }
  };

  const verifyIncidentRecovery = async () => {
    setAgentCommand('verify-recovery');
    try {
      const response = await axios.post<{ ready_to_resolve: boolean; message: string; blockers: string[] }>(
        `${BACKEND_URL}/api/agent/verify-recovery`,
      );
      setError(response.data.ready_to_resolve
        ? null
        : `${response.data.message} ${response.data.blockers.join(' ')}`);
      await fetchState();
    } catch (verificationError) {
      console.error('Recovery verification failed:', verificationError);
      setError('The agent could not verify recovery.');
    } finally {
      setAgentCommand(null);
    }
  };

  const advanceDecision = async (decision: Decision, operation: 'approve' | 'execute' | 'verify') => {
    setBusyDecisionId(decision.id);
    try {
      const base = `${BACKEND_URL}/api/safety/decisions/${decision.id}`;
      if (operation === 'approve') {
        await axios.post(`${base}/approve`, { approved_by: approverName });
      } else if (operation === 'execute') {
        await axios.post(`${base}/execute`);
      } else {
        await axios.post(`${base}/verify`, {
          fact_id: verificationFactId || safeState.facts[0]?.id,
          observed_metric: verificationMetric,
          source: 'Safety verification UI',
          verified_by: approverName,
        });
      }
      setSafetyResult(null);
      await fetchState();
    } catch (workflowError) {
      console.error(`Decision ${operation} failed:`, workflowError);
      setError(`Decision ${operation} failed. Check its approval and verification state.`);
    } finally {
      setBusyDecisionId(null);
    }
  };

  const liveState = state ?? EMPTY_STATE;
  const safeState: State = useMockData
    ? { ...EMPTY_STATE, ...(demoReplayActive ? buildMockReplayState(demoReplayStep) : MOCK_PAYMENT_OUTAGE) } as State
    : liveState;
  const replayStage = MOCK_REPLAY_STAGES[demoReplayStep];
  const replayComplete = demoReplayStep === MOCK_REPLAY_STAGES.length - 1;

  useEffect(() => {
    if (!safeState.agent_runtime?.timestamp) return;
    // Replay the observable loop when a new autonomous result reaches the UI.
    // oxlint-disable-next-line react/set-state-in-effect
    setVisibleAgentStage(0);
    let stage = 0;
    const interval = window.setInterval(() => {
      stage += 1;
      setVisibleAgentStage(Math.min(stage, 4));
      if (stage >= 4) window.clearInterval(interval);
    }, 850);
    return () => window.clearInterval(interval);
  }, [safeState.agent_runtime?.timestamp]);

  const roleSummaries = (() => {
    const roles = new Map<string, { count: number; speakers: Set<string> }>();
    safeState.transcripts.forEach(item => {
      const current = roles.get(item.role) ?? { count: 0, speakers: new Set<string>() };
      current.count += 1;
      current.speakers.add(item.speaker);
      roles.set(item.role, current);
    });
    return Array.from(roles.entries())
      .map(([role, details]) => ({ role, count: details.count, speakers: Array.from(details.speakers) }))
      .sort((a, b) => b.count - a.count);
  })();

  const openHypotheses = safeState.hypotheses.filter(hypothesis =>
    !['REJECTED', 'CONFIRMED'].includes(hypothesis.status),
  ).length;
  const inProgressActions = safeState.actions.filter(action =>
    action.status === 'IN_PROGRESS' || action.status === 'TODO',
  ).length;
  const unresolvedSignals = safeState.hypotheses.filter(hypothesis =>
    hypothesis.status === 'UNCONFIRMED' || hypothesis.status === 'DISPUTED',
  ).length + safeState.conflicts.filter(item => item.status !== 'RESOLVED').length
    + safeState.unknowns.filter(item => item.status === 'OPEN').length;
  const incidentRecovered = safeState.agent_runtime?.remediation?.status === 'SIMULATED_EXECUTED'
    && safeState.agent_runtime?.recovery_check?.result?.recovered === true;

  if (loading && !state) {
    return (
      <div className="empty-placeholder" style={{ minHeight: '60vh' }}>
        <div>
          <RefreshCw size={24} aria-hidden="true" />
          <p>Connecting to the ResQVoice command engine…</p>
        </div>
      </div>
    );
  }

  return (
    <div className="dashboard-container">
      <header className="page-header">
        <div>
          <h1 className="page-title">
            <Activity className="page-title-icon" size={23} aria-hidden="true" />
            Incident command center
          </h1>
          <p className="page-description">
            Live operational state extracted from the active voice channel
          </p>
        </div>
        <div className="page-actions" aria-label="Workspace actions">
          <button className="btn btn-secondary" onClick={() => void fetchState()} title="Refresh state immediately">
            <RefreshCw size={14} aria-hidden="true" />
            Sync
          </button>
          <button className="btn btn-primary" onClick={startGuidedReplay}>
            <Play size={14} aria-hidden="true" />
            {demoReplayActive ? 'Restart guided replay' : 'Guided demo replay'}
          </button>
          <button
            className={`btn ${useMockData ? 'mock-toggle-active' : 'btn-secondary'}`}
            onClick={toggleMockData}
            aria-pressed={useMockData}
          >
            <TestTube2 size={14} aria-hidden="true" />
            {useMockData ? 'Return to live data' : 'Use mock data'}
          </button>
          <button className="btn btn-danger" onClick={handleResetWorkspace} disabled={isResetting}>
            <RotateCcw size={14} aria-hidden="true" />
            {isResetting ? 'Resetting…' : 'Reset workspace'}
          </button>
        </div>
      </header>

      {error && (
        <div className="notice-banner notice-error" role="alert">
          <AlertTriangle size={17} aria-hidden="true" />
          <span>{error}</span>
        </div>
      )}

      {useMockData && (
        <div className="mock-banner" role="status">
          <TestTube2 size={17} aria-hidden="true" />
          <strong>MOCK DATA ACTIVE</strong>
          <span>Isolated payment-outage fixture. Live backend data remains unchanged and hidden.</span>
        </div>
      )}

      {demoReplayActive && (
        <section className="guided-replay" aria-live="polite" aria-label="Guided demo replay">
          <div className="guided-replay-glow" aria-hidden="true" />
          <div className="guided-replay-copy">
            <div className="guided-replay-kicker">
              <span className="guided-replay-live"><span /> GUIDED REPLAY</span>
              <span>Chapter {demoReplayStep + 1} of {MOCK_REPLAY_STAGES.length}</span>
              {demoReplayVoiceEnabled && <span className="guided-replay-voice-status"><Volume2 size={11} /> Agent voice live</span>}
            </div>
            <h2>{replayStage.title}</h2>
            <p>{replayStage.narration}</p>
            <div className="guided-replay-cue"><Sparkles size={14} aria-hidden="true" /><span>{replayStage.cue}</span></div>
          </div>
          <div className="guided-replay-controls">
            <button className="btn btn-secondary" onClick={toggleReplayVoice} aria-pressed={demoReplayVoiceEnabled}>
              {demoReplayVoiceEnabled ? <Volume2 size={14} /> : <VolumeX size={14} />}
              Voice {demoReplayVoiceEnabled ? 'on' : 'off'}
            </button>
            <button
              className="btn btn-primary"
              onClick={() => setDemoReplayPlaying(playing => !playing)}
              disabled={replayComplete}
            >
              {demoReplayPlaying && !replayComplete ? <Pause size={14} /> : <Play size={14} />}
              {replayComplete ? 'Replay complete' : demoReplayPlaying ? 'Pause' : 'Resume'}
            </button>
            <button className="btn btn-secondary" onClick={startGuidedReplay}><RotateCcw size={14} />Restart</button>
            <button
              className="btn btn-secondary"
              disabled={replayComplete}
              onClick={() => setDemoReplayStep(step => Math.min(step + 1, MOCK_REPLAY_STAGES.length - 1))}
            >
              Next <SkipForward size={14} />
            </button>
          </div>
          <div className="guided-replay-progress" aria-label={`Replay progress: ${demoReplayStep + 1} of ${MOCK_REPLAY_STAGES.length}`}>
            {MOCK_REPLAY_STAGES.map((stage, index) => (
              <button
                key={stage.label}
                className={index < demoReplayStep ? 'is-complete' : index === demoReplayStep ? 'is-active' : ''}
                onClick={() => {
                  setDemoReplayStep(index);
                  setDemoReplayRun(run => run + 1);
                }}
                title={`Go to ${stage.label}`}
              >
                <span>{index < demoReplayStep ? <CheckCircle2 size={13} /> : index + 1}</span>
                <small>{stage.label}</small>
              </button>
            ))}
          </div>
        </section>
      )}

      <section className="surface-panel health-panel" aria-labelledby="incident-health-title">
        <div className="panel-header">
          <h2 className="panel-title" id="incident-health-title">
            <ShieldCheck size={17} aria-hidden="true" />
            Incident health
          </h2>
          <StatusBadge status={incidentRecovered ? 'CONFIRMED' : unresolvedSignals > 0 ? 'UNRESOLVED-CRITICAL' : 'CONFIRMED'} label={incidentRecovered ? 'Recovery verified' : unresolvedSignals > 0 ? 'Attention required' : 'Monitoring'} />
        </div>
        <div className="health-grid">
          <div className="health-metric">
            <div className="metric-topline">
              <span className="metric-label">Confirmed facts</span>
              <span className="metric-icon status-confirmed"><CheckCircle2 size={15} aria-hidden="true" /></span>
            </div>
            <div className="metric-value">{safeState.facts.length}</div>
            <div className="metric-detail">Verified operational signals</div>
          </div>
          <div className="health-metric">
            <div className="metric-topline">
              <span className="metric-label">Open hypotheses</span>
              <span className="metric-icon status-unknown"><CircleHelp size={15} aria-hidden="true" /></span>
            </div>
            <div className="metric-value">{openHypotheses}</div>
            <div className="metric-detail">Root-cause paths under review</div>
          </div>
          <div className="health-metric">
            <div className="metric-topline">
              <span className="metric-label">Pending actions</span>
              <span className="metric-icon status-corroborated"><ListTodo size={15} aria-hidden="true" /></span>
            </div>
            <div className="metric-value">{inProgressActions}<span className="metric-detail"> / {safeState.actions.length}</span></div>
            <div className="metric-detail">Assigned or ready for execution</div>
          </div>
          <div className="health-metric">
            <div className="metric-topline">
              <span className="metric-label">Unresolved signals</span>
              <span className={`metric-icon ${unresolvedSignals ? 'status-critical' : 'status-confirmed'}`}>
                {unresolvedSignals ? <AlertOctagon size={15} aria-hidden="true" /> : <BadgeCheck size={15} aria-hidden="true" />}
              </span>
            </div>
            <div className="metric-value">{unresolvedSignals}</div>
            <div className="metric-detail">Disputes and active conflicts</div>
          </div>
        </div>
      </section>

      <section className="surface-panel agent-operations" aria-labelledby="agent-operations-title">
        <div className="panel-header">
          <h2 className="panel-title" id="agent-operations-title"><Radio size={17} aria-hidden="true" />Agent operations</h2>
          <span className="panel-count">Voice-triggered · no button required</span>
        </div>
        <div className="agent-operation-body">
          <div className="agent-operation-actions">
            <button className="btn btn-secondary" disabled={agentCommand !== null} onClick={() => void runAutonomousCycle()} title="Developer fallback for replaying a cycle without live speech">
              <RefreshCw size={14} aria-hidden="true" />Replay cycle
            </button>
            <button className="btn btn-primary" disabled={agentCommand !== null} onClick={() => void runAgentCommand('summary')}>
              <Radio size={14} aria-hidden="true" />Speak status summary
            </button>
            <button className="btn btn-secondary" disabled={agentCommand !== null} onClick={() => void runAgentCommand('review_gaps')}>
              <SearchCheck size={14} aria-hidden="true" />Review evidence gaps
            </button>
            <button className="btn btn-secondary" disabled={agentCommand !== null} onClick={() => void runAgentCommand('review_actions')}>
              <ListTodo size={14} aria-hidden="true" />Review action owners
            </button>
            <button className="btn btn-secondary" disabled={agentCommand !== null} onClick={() => void generateIncidentReport()}>
              <FileText size={14} aria-hidden="true" />Generate handoff report
            </button>
            <button className="btn btn-secondary" disabled={agentCommand !== null} onClick={() => void verifyIncidentRecovery()}>
              <ShieldCheck size={14} aria-hidden="true" />Verify recovery
            </button>
          </div>
          <p className="agent-operation-note">
            The agent automatically flags contradictions, explicit unknowns, stale critical claims, and unowned P1 actions. Spoken commands require an active Voice Room session.
          </p>
          {incidentReport && (
            <div className="report-preview">
              <div className="item-top">
                <strong>Incident handoff / postmortem draft</strong>
                <StatusBadge status={incidentReport.status} />
              </div>
              <p className="item-meta">{incidentReport.summary}</p>
              <pre>{incidentReport.markdown}</pre>
            </div>
          )}
          {safeState.agent_runtime ? (
            <div className="agent-activity-center">
              <div className="agent-loop-header">
                <div>
                  <div className="agent-live-label"><span className="agent-live-dot" />AUTONOMOUS LOOP LIVE</div>
                  <h3>How the agent is moving the incident forward</h3>
                </div>
                <div className="agent-loop-meta">
                  <span>Last cycle {formatTime(safeState.agent_runtime.timestamp)}</span>
                  <span className="status-badge status-corroborated">Demo · read only</span>
                </div>
              </div>

              <div className="agent-loop" aria-label="Agent workflow">
                {[
                  { label: 'Observe', caption: 'Listen to responders', Icon: Eye },
                  { label: 'Plan', caption: 'Find the next gap', Icon: Sparkles },
                  { label: 'Act', caption: 'Call a safe tool', Icon: Wrench },
                  { label: 'Verify', caption: 'Ground in evidence', Icon: ShieldCheck },
                  { label: 'Update', caption: 'Guide the team', Icon: Radio },
                ].map(({ label, caption, Icon }, index, stages) => (
                  <div className="agent-loop-fragment" key={label}>
                    <div className={`agent-loop-stage ${index < visibleAgentStage ? 'is-complete' : index === visibleAgentStage ? 'is-active' : 'is-waiting'}`}>
                      <span className="agent-stage-icon"><Icon size={16} aria-hidden="true" /></span>
                      <span><strong>{label}</strong><small>{caption}</small></span>
                      {index < visibleAgentStage && <CheckCircle2 className="agent-stage-check" size={14} aria-hidden="true" />}
                    </div>
                    {index < stages.length - 1 && <ArrowRight className="agent-loop-arrow" size={16} aria-hidden="true" />}
                  </div>
                ))}
              </div>

              {incidentRecovered && safeState.agent_runtime.remediation && (
                <section className="incident-result-card" aria-label="Autonomous incident result">
                  <div className="incident-result-heading">
                    <span><BadgeCheck size={18} />INCIDENT RECOVERED AUTONOMOUSLY</span>
                    <strong>Recovery verified</strong>
                  </div>
                  <div className="incident-result-grid">
                    <div><span>Detected impact</span><strong>HTTP 503 · 38.2% errors</strong></div>
                    <div><span>Root cause</span><strong>Pricing servlet DB pool timeout</strong></div>
                    <div><span>Targeted action</span><strong>Restarted only the failing servlet</strong></div>
                    <div><span>Recovery evidence</span><strong>{formatToolValue(safeState.agent_runtime.recovery_check?.result.error_rate_percent)}% error rate · baseline restored</strong></div>
                  </div>
                </section>
              )}

              <div className="agent-runtime-layout">
                <div className="agent-runtime-column">
                  <article className="agent-runtime-card agent-objective-card">
                    <div className="agent-card-eyebrow"><Sparkles size={13} />CURRENT OBJECTIVE</div>
                    <p>{safeState.agent_runtime.objective}</p>
                  </article>
                  <article className="agent-runtime-card">
                    <div className="agent-card-heading"><strong>Live response plan</strong><span>{safeState.agent_runtime.plan.filter(item => item.status === 'DONE').length}/{safeState.agent_runtime.plan.length} cleared</span></div>
                    <div className="agent-plan">
                      {safeState.agent_runtime.plan.map((item, index) => (
                        <div className={`agent-plan-step plan-${item.status.toLowerCase()}`} key={item.step}>
                          <span className="agent-plan-number">{item.status === 'DONE' ? <CheckCircle2 size={14} /> : index + 1}</span>
                          <span>{item.step}</span>
                          <StatusBadge status={item.status} />
                        </div>
                      ))}
                    </div>
                  </article>
                  {safeState.agent_runtime.diagnosis && (
                    <article className="agent-diagnosis-card">
                      <div className="agent-card-eyebrow"><SearchCheck size={13} />ROOT CAUSE ISOLATED</div>
                      <p>{safeState.agent_runtime.diagnosis}</p>
                    </article>
                  )}
                  <article className="agent-runtime-card">
                    <div className="agent-card-heading"><strong>Autonomous investigation</strong><span className="status-badge status-corroborated">Safe · read only</span></div>
                    {(safeState.agent_runtime.tool_calls ?? [safeState.agent_runtime.tool_call]).map(toolCall => (
                      <div className="tool-call-card" key={toolCall.tool}>
                        <div className="tool-call-name"><Database size={15} /><strong>{toolCall.tool.replaceAll('_', ' ')}</strong></div>
                        <div className="tool-result-grid">
                          {Object.entries(toolCall.result).map(([key, value]) => (
                            <div className="tool-result" key={key}><span>{key.replaceAll('_', ' ')}</span><strong>{formatToolValue(value)}</strong></div>
                          ))}
                        </div>
                      </div>
                    ))}
                    {(safeState.agent_runtime.hypothesis_updates ?? []).map(update => (
                      <div className="confidence-update" key={update.hypothesis_id}>
                        <span>Hypothesis confidence</span>
                        <strong>{Math.round(update.previous_confidence * 100)}%</strong>
                        <ArrowRight size={13} />
                        <strong className="confidence-new">{Math.round(update.new_confidence * 100)}%</strong>
                        <small>Matched: {update.evidence_overlap.join(', ')}</small>
                      </div>
                    ))}
                  </article>
                  {safeState.agent_runtime.remediation && (
                    <article className="agent-remediation-card">
                      <div>
                        <div className="agent-card-eyebrow"><Wrench size={13} />TARGETED REMEDIATION</div>
                        <p>{safeState.agent_runtime.remediation.action}</p>
                        <small>Whole-system restart avoided · {safeState.agent_runtime.remediation.mode.replaceAll('_', ' ')}</small>
                      </div>
                      <StatusBadge status={safeState.agent_runtime.remediation.status} />
                    </article>
                  )}
                  <article className="agent-decision-card">
                    <div className="agent-card-eyebrow"><Radio size={13} />NEXT TEAM PROMPT</div>
                    <p>{safeState.agent_runtime.next_question}</p>
                    {safeState.agent_runtime.recommended_owner && (
                      <span><UserRound size={13} /> Suggested owner: <strong>{safeState.agent_runtime.recommended_owner.name}</strong> · {safeState.agent_runtime.recommended_owner.role}</span>
                    )}
                  </article>
                </div>

                <article className="agent-runtime-card agent-feed-card">
                  <div className="agent-card-heading"><strong>Background activity</strong><span className="agent-feed-live"><span />Streaming</span></div>
                  <p className="agent-feed-intro">An auditable view of what the agent hears, extracts, checks, and communicates.</p>
                  <div className="agent-event-feed">
                    {safeState.agent_activity.slice(0, 14).map(item => (
                      <div className={`agent-event event-${item.phase}`} key={item.id}>
                        <div className="agent-event-rail"><span /></div>
                        <div className="agent-event-content">
                          <div className="agent-event-top"><span>{item.phase}</span><time>{formatTime(item.timestamp)}</time></div>
                          <strong>{item.title}</strong>
                          <p>{item.detail}</p>
                        </div>
                      </div>
                    ))}
                    {!safeState.agent_activity.length && <div className="empty-placeholder">Run an autonomous cycle to see the audit trail.</div>}
                  </div>
                </article>
              </div>
            </div>
          ) : (
            <div className="agent-empty-state">
              <Sparkles size={20} aria-hidden="true" />
              <div><strong>The autonomous loop is ready</strong><p>Run a cycle or speak in the Voice Room to visualize the agent's work.</p></div>
            </div>
          )}
        </div>
      </section>

      <div className="dashboard-grid">
        <section className="col-span-12 section-panel surface-panel" aria-labelledby="safety-title">
          <div className="panel-header">
            <h2 className="panel-title" id="safety-title"><ShieldAlert size={17} aria-hidden="true" />Safety approvals</h2>
            <span className="panel-count">SOP-gated execution</span>
          </div>
          <div className="panel-body safety-workspace">
            <div className="safety-form">
              <label className="field-group">
                <span className="field-label">Proposed action</span>
                <input className="form-control" value={proposedAction} onChange={event => setProposedAction(event.target.value)} />
              </label>
              <label className="field-group">
                <span className="field-label">Evidence trail (one item per line)</span>
                <textarea className="form-control" rows={3} value={actionEvidence} onChange={event => setActionEvidence(event.target.value)} />
              </label>
              <label className="field-group">
                <span className="field-label">Completed prerequisite steps (in order)</span>
                <textarea className="form-control" rows={4} value={completedSteps} onChange={event => setCompletedSteps(event.target.value)} />
              </label>
              <button className="btn btn-primary" onClick={() => void handleRecommend()} disabled={busyDecisionId !== null || !proposedAction.trim()}>
                <ShieldCheck size={14} aria-hidden="true" />
                {busyDecisionId === 'recommend' ? 'Checking SOP…' : 'Check SOP and recommend'}
              </button>
            </div>

            <div className="approval-queue">
              {safetyResult?.conflict && (
                <div className="notice-banner notice-warning" role="alert">
                  <AlertTriangle size={17} aria-hidden="true" />
                  <div>
                    <strong>Potential SOP Conflict</strong>
                    <p>{safetyResult.conflict.missing_steps.length} prerequisite step(s) missing{ safetyResult.conflict.out_of_order ? ' or out of order' : ''}. Approval is blocked.</p>
                  </div>
                </div>
              )}
              <div className="field-grid safety-controls">
                <label className="field-group">
                  <span className="field-label">Human approver</span>
                  <input className="form-control" value={approverName} onChange={event => setApproverName(event.target.value)} />
                </label>
                <label className="field-group">
                  <span className="field-label">Verification fact / metric</span>
                  <select className="form-control" value={verificationFactId} onChange={event => setVerificationFactId(event.target.value)}>
                    <option value="">Select the first available fact</option>
                    {safeState.facts.map(fact => <option value={fact.id} key={fact.id}>{fact.description}</option>)}
                  </select>
                  <input className="form-control" value={verificationMetric} onChange={event => setVerificationMetric(event.target.value)} />
                </label>
              </div>

              {safeState.decisions.length === 0 ? (
                <div className="empty-placeholder">No recommendations are waiting for safety review.</div>
              ) : safeState.decisions.map(decision => (
                <article className="item-card safety-decision" key={decision.id}>
                  <div className="item-top">
                    <StatusBadge status={decision.execution_status} />
                    <span className="item-meta mono">{decision.sop_reference || 'No SOP matched'}</span>
                  </div>
                  <p className="item-text"><strong>{decision.recommendation}</strong></p>
                  {decision.evidence.length > 0 && <div className="evidence-note">{decision.evidence.join(' · ')}</div>}
                  {decision.result?.startsWith('Potential SOP Conflict') && safetyResult?.conflict && (
                    <ol className="sop-sequence">
                      {safetyResult.conflict.recommended_sequence.map(step => <li key={step}>{step}</li>)}
                    </ol>
                  )}
                  <div className="decision-controls">
                    {decision.execution_status === 'PENDING_APPROVAL' && (
                      <button className="btn btn-primary" disabled={!approverName.trim() || busyDecisionId !== null} onClick={() => void advanceDecision(decision, 'approve')}>Approve explicitly</button>
                    )}
                    {decision.execution_status === 'APPROVED' && (
                      <button className="btn btn-secondary" disabled={busyDecisionId !== null} onClick={() => void advanceDecision(decision, 'execute')}>Execute mocked action</button>
                    )}
                    {decision.execution_status === 'AWAITING_VERIFICATION' && (
                      <button className="btn btn-primary" disabled={safeState.facts.length === 0 || !verificationMetric.trim() || busyDecisionId !== null} onClick={() => void advanceDecision(decision, 'verify')}>Verify metric and complete</button>
                    )}
                    {decision.approved_by && <span className="item-meta">Approved by <strong>{decision.approved_by}</strong> at {formatTime(decision.approval_time)}</span>}
                  </div>
                </article>
              ))}
            </div>
          </div>
        </section>

        <section className="col-span-6 section-panel surface-panel" aria-labelledby="known-title">
          <div className="panel-header">
            <h2 className="panel-title" id="known-title"><CheckCircle2 size={17} aria-hidden="true" />What we know</h2>
            <span className="panel-count">{safeState.facts.length} facts</span>
          </div>
          <div className="panel-body panel-body-scroll item-list">
            {safeState.facts.length === 0 ? (
              <div className="empty-placeholder">No confirmed facts extracted yet.</div>
            ) : safeState.facts.map(fact => (
              <article key={fact.id} className="item-card">
                <div className="item-top">
                  <StatusBadge status="CONFIRMED" />
                  <time className="item-meta mono" dateTime={fact.timestamp}><Clock3 size={11} aria-hidden="true" />{formatTime(fact.timestamp)}</time>
                </div>
                <p className="item-text">{fact.description}</p>
                <div className="item-meta">
                  <span>{fact.source}</span><span className="meta-separator">/</span><span className="meta-speaker">{fact.speaker}</span>
                </div>
              </article>
            ))}
          </div>
        </section>

        <section className="col-span-6 section-panel surface-panel" aria-labelledby="unknown-title">
          <div className="panel-header">
            <h2 className="panel-title" id="unknown-title"><CircleHelp size={17} aria-hidden="true" />What we don’t know</h2>
            <span className="panel-count">{safeState.hypotheses.length} hypotheses · {safeState.unknowns.filter(item => item.status === 'OPEN').length} questions</span>
          </div>
          <div className="panel-body panel-body-scroll item-list">
            {safeState.hypotheses.length === 0 ? (
              <div className="empty-placeholder">No active hypotheses recorded.</div>
            ) : safeState.hypotheses.map(hypothesis => (
              <article key={hypothesis.id} className="item-card">
                <div className="item-top">
                  <StatusBadge status={hypothesis.status} />
                  <span className="item-meta mono">{Math.round(hypothesis.confidence * 100)}% confidence</span>
                </div>
                <p className="item-text">{hypothesis.description}</p>
                <div className="item-meta"><span>Origin</span><span className="meta-speaker">{hypothesis.origin}</span></div>
                {hypothesis.supporting_evidence?.length > 0 && (
                  <div className="evidence-note"><strong>Supporting evidence:</strong> {hypothesis.supporting_evidence.join(', ')}</div>
                )}
              </article>
            ))}
            {safeState.unknowns.filter(item => item.status === 'OPEN').map(item => (
              <article key={item.id} className="item-card">
                <div className="item-top"><StatusBadge status="UNRESOLVED" label="Open question" /></div>
                <p className="item-text">{item.description}</p>
                <div className="item-meta">The agent will request clarification or evidence.</div>
              </article>
            ))}
          </div>
        </section>

        <section className="col-span-6 section-panel surface-panel" aria-labelledby="actions-title">
          <div className="panel-header">
            <h2 className="panel-title" id="actions-title"><ListTodo size={17} aria-hidden="true" />Response actions</h2>
            <span className="panel-count">{safeState.actions.length} total</span>
          </div>
          <div className="panel-body panel-body-scroll item-list">
            {safeState.actions.length === 0 ? (
              <div className="empty-placeholder">No action items currently assigned.</div>
            ) : safeState.actions.map(action => (
              <article key={action.id} className="item-card">
                <div className="item-top">
                  <StatusBadge status={action.status} />
                  <StatusBadge status={action.priority === 'P1' ? 'UNRESOLVED-CRITICAL' : 'UNKNOWN'} label={action.priority || 'Normal'} />
                </div>
                <p className="item-text">{action.task}</p>
                <div className="item-meta"><span>Owner</span><span className="meta-speaker">{action.owner}</span><span className="meta-separator">/</span><span className="mono">{formatTime(action.created_at)}</span></div>
              </article>
            ))}
          </div>
        </section>

        <section className="col-span-6 section-panel surface-panel" aria-labelledby="decisions-title">
          <div className="panel-header">
            <h2 className="panel-title" id="decisions-title"><ShieldCheck size={17} aria-hidden="true" />Authoritative decisions</h2>
            <span className="panel-count">{safeState.decisions.length} logged</span>
          </div>
          <div className="panel-body panel-body-scroll item-list">
            {safeState.decisions.length === 0 ? (
              <div className="empty-placeholder">No formal decisions recorded yet.</div>
            ) : safeState.decisions.map(decision => (
              <article key={decision.id} className="item-card">
                <div className="item-top">
                  <StatusBadge status={decision.execution_status || 'APPROVED'} />
                  <time className="item-meta mono" dateTime={decision.approval_time}>{formatTime(decision.approval_time)}</time>
                </div>
                <p className="item-text"><strong>{decision.recommendation}</strong></p>
                <div className="item-meta"><span>Commander</span><span className="meta-speaker">{decision.approved_by || 'Command'}</span>{decision.sop_reference && <><span className="meta-separator">/</span><span>{decision.sop_reference}</span></>}</div>
              </article>
            ))}
          </div>
        </section>

        <section className="col-span-12 section-panel surface-panel" aria-labelledby="roles-title">
          <div className="panel-header">
            <h2 className="panel-title" id="roles-title"><UserRound size={17} aria-hidden="true" />Role-aware summary</h2>
            <span className="panel-count">{roleSummaries.length} represented roles</span>
          </div>
          {roleSummaries.length === 0 ? (
            <div className="empty-placeholder">Role activity will appear as responders speak.</div>
          ) : (
            <div className="role-summary-grid">
              {roleSummaries.map(summary => (
                <div className="role-summary" key={summary.role}>
                  <div className="role-name">{summary.role}</div>
                  <div className="role-speakers">{summary.speakers.join(', ')}</div>
                  <div className="role-count">{summary.count} transcript {summary.count === 1 ? 'entry' : 'entries'}</div>
                </div>
              ))}
            </div>
          )}
        </section>

        <section className="col-span-7 section-panel surface-panel" aria-labelledby="transcript-title">
          <div className="panel-header">
            <h2 className="panel-title" id="transcript-title"><Radio size={17} aria-hidden="true" />{useMockData ? 'Mock transcript feed' : 'Live transcript feed'}</h2>
            <span className="panel-count">{safeState.transcripts.length} recent</span>
          </div>
          <div className="panel-body panel-body-scroll feed-list">
            {safeState.transcripts.length === 0 ? (
              <div className="empty-placeholder">No transcribed speech received yet.</div>
            ) : safeState.transcripts.map(item => (
              <article className="feed-row" key={item.id}>
                <div className="feed-author">
                  <span className="feed-speaker">{item.speaker}</span>
                  <span className="feed-role">{item.role}</span>
                </div>
                <p className="feed-text">{item.text}</p>
                <time className="feed-time" dateTime={item.timestamp}>{formatTime(item.timestamp)}</time>
              </article>
            ))}
          </div>
        </section>

        <section className="col-span-5 section-panel surface-panel" aria-labelledby="timeline-title">
          <div className="panel-header">
            <h2 className="panel-title" id="timeline-title"><History size={17} aria-hidden="true" />Incident timeline</h2>
            <span className="panel-count">Replay log</span>
          </div>
          <div className="panel-body panel-body-scroll timeline-list">
            {safeState.transcripts.length === 0 ? (
              <div className="empty-placeholder">The timeline begins with the first transcript.</div>
            ) : [...safeState.transcripts].reverse().map(item => (
              <div className="timeline-row" key={item.id}>
                <time className="timeline-time" dateTime={item.timestamp}>{formatTime(item.timestamp)}</time>
                <div className="timeline-rail"><span className="timeline-dot" /></div>
                <div className="timeline-content">
                  <div className="timeline-meta"><strong>{item.speaker}</strong><span>·</span><span>{item.role}</span></div>
                  <p className="timeline-text">{item.text}</p>
                </div>
              </div>
            ))}
          </div>
        </section>

        <section className="col-span-12 section-panel surface-panel" aria-labelledby="claims-title">
          <div className="panel-header">
            <h2 className="panel-title" id="claims-title"><MessageSquare size={17} aria-hidden="true" />Extracted claim feed</h2>
            <span className="panel-count">{safeState.claims.length} claims</span>
          </div>
          <div className="panel-body panel-body-scroll feed-list">
            {safeState.claims.length === 0 ? (
              <div className="empty-placeholder">No claims extracted from the voice channel.</div>
            ) : safeState.claims.map(claim => (
              <article className="feed-row" key={claim.id}>
                <div className="feed-author"><span className="feed-speaker">{claim.speaker}</span><span className="feed-role">{claim.role}</span></div>
                <div><p className="feed-text">{claim.text}</p><div style={{ marginTop: 6 }}><StatusBadge status={claim.status} /></div></div>
                <time className="feed-time" dateTime={claim.timestamp}>{formatTime(claim.timestamp)}</time>
              </article>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
}
