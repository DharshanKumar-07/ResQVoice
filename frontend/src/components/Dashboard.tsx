import { useEffect, useMemo, useState } from 'react';
import axios from 'axios';
import {
  Activity,
  AlertOctagon,
  AlertTriangle,
  BadgeCheck,
  CheckCircle2,
  CircleHelp,
  Clock3,
  History,
  Link2,
  ListTodo,
  MessageSquare,
  Radio,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  TestTube2,
  UserRound,
  XCircle,
} from 'lucide-react';
import type {
  Action,
  Claim,
  Conflict,
  Decision,
  Fact,
  Hypothesis,
  TranscriptEvent,
} from '../types';
import { MOCK_PAYMENT_OUTAGE_TRANSCRIPTS } from '../data/mockIncident';

interface State {
  facts: Fact[];
  hypotheses: Hypothesis[];
  actions: Action[];
  decisions: Decision[];
  claims: Claim[];
  conflicts: Conflict[];
  transcripts: TranscriptEvent[];
}

const EMPTY_STATE: State = {
  facts: [],
  hypotheses: [],
  actions: [],
  decisions: [],
  claims: [],
  conflicts: [],
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

export default function Dashboard() {
  const [state, setState] = useState<State | null>(null);
  const [loading, setLoading] = useState(true);
  const [useMockData, setUseMockData] = useState(false);
  const [isResetting, setIsResetting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchState = async () => {
    try {
      const [stateResponse, transcriptResponse] = await Promise.all([
        axios.get('http://localhost:8000/api/state'),
        axios.get<TranscriptEvent[]>('http://localhost:8000/api/transcripts', {
          params: { limit: 50 },
        }),
      ]);
      setState({ ...EMPTY_STATE, ...stateResponse.data, transcripts: transcriptResponse.data });
      setError(null);
    } catch (fetchError) {
      console.error('Failed to fetch incident state:', fetchError);
      setError('Unable to connect to the command engine at localhost:8000.');
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

  const handleResetWorkspace = async () => {
    const confirmed = window.confirm(
      'Reset Workspace will permanently clear all live transcripts and incident state. Continue?',
    );
    if (!confirmed) return;

    setIsResetting(true);
    try {
      await axios.post('http://localhost:8000/api/workspace/reset');
      setUseMockData(false);
      setState(EMPTY_STATE);
      await fetchState();
    } catch (resetError) {
      console.error('Workspace reset failed:', resetError);
      setError('Workspace reset failed. The backend store may not be empty.');
    } finally {
      setIsResetting(false);
    }
  };

  const liveState = state ?? EMPTY_STATE;
  const safeState: State = useMockData
    ? { ...EMPTY_STATE, transcripts: MOCK_PAYMENT_OUTAGE_TRANSCRIPTS }
    : liveState;

  const roleSummaries = useMemo(() => {
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
  }, [safeState.transcripts]);

  const inProgressActions = safeState.actions.filter(action =>
    action.status === 'IN_PROGRESS' || action.status === 'TODO',
  ).length;
  const unresolvedSignals = safeState.hypotheses.filter(hypothesis =>
    hypothesis.status === 'UNCONFIRMED' || hypothesis.status === 'DISPUTED',
  ).length + safeState.conflicts.length;

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
          <button
            className={`btn ${useMockData ? 'mock-toggle-active' : 'btn-secondary'}`}
            onClick={() => setUseMockData(enabled => !enabled)}
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

      <section className="surface-panel health-panel" aria-labelledby="incident-health-title">
        <div className="panel-header">
          <h2 className="panel-title" id="incident-health-title">
            <ShieldCheck size={17} aria-hidden="true" />
            Incident health
          </h2>
          <StatusBadge status={unresolvedSignals > 0 ? 'UNRESOLVED-CRITICAL' : 'CONFIRMED'} label={unresolvedSignals > 0 ? 'Attention required' : 'Monitoring'} />
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
            <div className="metric-value">{safeState.hypotheses.length}</div>
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

      <div className="dashboard-grid">
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
            <span className="panel-count">{safeState.hypotheses.length} hypotheses</span>
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
