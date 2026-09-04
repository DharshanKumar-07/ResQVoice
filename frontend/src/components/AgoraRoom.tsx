import { useState, useEffect, useRef } from 'react';
import axios from 'axios';
import { Mic, MicOff, Radio, Volume2, AlertCircle, CheckCircle2, Users } from 'lucide-react';
import { useAgoraIncidentRoom } from '../hooks/useAgoraIncidentRoom';
import type { TranscriptEvent } from '../types';
import { installAgoraWebRtcCompatibility } from '../utils/agoraWebRtcCompatibility';

const ROLES = [
  "Incident Commander", "Backend Engineer", "DevOps/SRE", 
  "Database Engineer", "Security Engineer", "Support Engineer", 
  "Product Manager", "Business Stakeholder"
];

const BACKEND_URL = import.meta.env.VITE_BACKEND_URL ?? 'http://localhost:8000';
const CHANNEL_NAME = 'incident-room';

installAgoraWebRtcCompatibility();

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------
export default function AgoraRoom() {
  const [role, setRole] = useState(ROLES[0]);
  const [speaker, setSpeaker] = useState("Priya");
  const [transcript, setTranscript] = useState<TranscriptEvent[]>([]);
  
  const transcriptCursorRef = useRef(0);

  const {
    joined,
    joining,
    error,
    joinedUid,
    remoteUsers,
    isMuted,
    agentConnected,
    stateConnected,
    speechPhase,
    engineSpeaking,
    speakingTrigger,
    speakingMessage,
    joinRoom,
    leaveRoom,
    toggleMute
  } = useAgoraIncidentRoom();

  const handleJoin = () => {
    joinRoom(CHANNEL_NAME, speaker, role);
  };

  useEffect(() => {
    if (!joined) return;

    let cancelled = false;
    const mergeTranscripts = (incoming: TranscriptEvent[]) => {
      if (cancelled || incoming.length === 0) return;
      transcriptCursorRef.current = Math.max(
        transcriptCursorRef.current,
        ...incoming.map(item => item.id),
      );
      setTranscript(previous => {
        const byId = new Map(previous.map(item => [item.id, item]));
        incoming.forEach(item => byId.set(item.id, item));
        return Array.from(byId.values()).sort((a, b) => a.id - b.id).slice(-100);
      });
    };

    const syncTranscripts = async () => {
      try {
        const response = await axios.get<TranscriptEvent[]>(`${BACKEND_URL}/api/transcripts`, {
          params: { after_id: transcriptCursorRef.current, limit: 100 },
        });
        mergeTranscripts(response.data);
      } catch (syncError) {
        console.warn('[Transcript Sync] Could not fetch recent transcripts:', syncError);
      }
    };

    void syncTranscripts();
    const eventSource = new EventSource(`${BACKEND_URL}/api/transcripts/stream`);
    eventSource.onmessage = event => {
      try {
        mergeTranscripts([JSON.parse(event.data) as TranscriptEvent]);
      } catch (streamError) {
        console.warn('[Transcript Stream] Ignoring malformed event:', streamError);
      }
    };
    eventSource.onerror = () => {
      console.warn('[Transcript Stream] Connection interrupted; browser will retry.');
    };
    return () => {
      cancelled = true;
      eventSource.close();
    };
  }, [joined]);

  // ── Render ─────────────────────────────────────────────────────────────
  const sessionActive = joined;
  const speakingReason: Record<string, string> = {
    CONFLICT_DETECTED: 'Flagging a conflict',
    STALE_CRITICAL_CLAIM: 'Raising an unresolved critical claim',
    UNASSIGNED_CRITICAL_ACTION: 'Raising an unassigned critical action',
    SOP_CONFLICT: 'Flagging a potential SOP conflict',
    DECISION_APPROVED: 'Confirming an approved decision',
    SUMMARY_REQUEST: 'Providing a requested summary',
    PERIODIC_SUMMARY: 'Providing a status summary',
  };

  return (
    <div className="dashboard-container voice-container">
      <header className="page-header">
        <div>
          <h1 className="page-title">
            <Radio className="page-title-icon" size={23} aria-hidden="true" />
            Voice incident room
          </h1>
          <p className="page-description">
            Connect responders and the Conversational AI Agent for real-time triage.
          </p>
        </div>
      </header>

      <section className="surface-panel voice-panel" aria-label="Voice room controls">
        {!sessionActive ? (
          <div className="voice-form">
            <div className="field-grid">
              <div className="field-group">
                <label className="field-label" htmlFor="responder-name">Responder name</label>
                <input
                  id="responder-name"
                  className="form-control"
                  value={speaker} 
                  onChange={e => setSpeaker(e.target.value)} 
                  placeholder="Enter your name (e.g. Priya)" 
                  autoComplete="name"
                />
              </div>

              <div className="field-group">
                <label className="field-label" htmlFor="incident-role">Incident role</label>
                <select
                  id="incident-role"
                  className="form-control"
                  value={role} 
                  onChange={e => setRole(e.target.value)}
                >
                  {ROLES.map(r => <option key={r} value={r}>{r}</option>)}
                </select>
              </div>
            </div>

            {error && (
              <div className="notice-banner notice-error" role="alert">
                <AlertCircle size={16} aria-hidden="true" />
                <span>{error}</span>
              </div>
            )}

            <button 
              className="btn btn-primary voice-primary-action"
              onClick={handleJoin}
              disabled={joining}
            >
              <Mic size={17} aria-hidden="true" />
              <span>{joining ? 'Connecting...' : 'Enter Incident Room & Start Agent'}</span>
            </button>
          </div>
        ) : (
          <div>
            {error && (
              <div className="notice-banner notice-error" role="alert">
                <AlertCircle size={16} aria-hidden="true" />
                <span>{error}</span>
              </div>
            )}

            <div className="voice-session">
              <div className="voice-session-identity">
                <div className="pulse-dot" />
                <div>
                  <div className="voice-session-title">
                    <CheckCircle2 size={14} aria-hidden="true" />
                    <span>Connected: {speaker} (uid: {joinedUid})</span>
                  </div>
                  <div className="voice-session-role">
                    {role}
                  </div>
                </div>
              </div>
              
              <div className="voice-actions">
                <div className="participant-count" title="Connected participants">
                  <Users size={14} aria-hidden="true" />
                  <span>{1 + remoteUsers.length} participant(s) · agent {agentConnected ? 'online' : 'joining'} · state {stateConnected ? 'live' : 'connecting'}</span>
                </div>
                <button className="btn btn-secondary" onClick={toggleMute}>
                  {isMuted ? <MicOff size={15} aria-hidden="true" /> : <Mic size={15} aria-hidden="true" />}
                  <span>{isMuted ? 'Unmute' : 'Mute'}</span>
                </button>
                <button className="btn btn-danger" onClick={leaveRoom}>
                  Leave Room
                </button>
              </div>
            </div>

            <div className="stream-section">
              <div className="stream-heading">
                <Volume2 size={15} aria-hidden="true" />
                <span>Live Speech Recognition Stream</span>
              </div>
              <div className="voice-live-indicators" aria-live="polite">
                {speechPhase !== 'idle' && !engineSpeaking && (
                  <div className={`activity-chip activity-${speechPhase}`}>
                    <span className="activity-dot" aria-hidden="true" />
                    {speechPhase === 'speech_detected' ? 'Speech detected…' : 'Transcribing…'}
                  </div>
                )}
                {engineSpeaking && (
                  <div className="activity-chip speaking-chip" title={speakingMessage ?? undefined}>
                    <span className="speaking-bars" aria-hidden="true"><i /><i /><i /></span>
                    <span>
                      <strong>ResQVoice is speaking…</strong>
                      {' · '}{speakingReason[speakingTrigger ?? ''] ?? 'Incident intervention'}
                    </span>
                  </div>
                )}
              </div>
              <div className="stream-window" aria-live="polite">
                {transcript.length === 0 ? (
                  <p className="stream-empty">
                    Speak naturally. The Conversational AI agent is listening and will transcribe your voice via Agora webhooks.
                  </p>
                ) : (
                  transcript.map(item => (
                    <div key={item.id} className="stream-row">
                      <span className="stream-speaker">{item.speaker}</span>{' '}
                      <span className="stream-role">({item.role})</span>: {item.text}
                    </div>
                  ))
                )}
              </div>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
