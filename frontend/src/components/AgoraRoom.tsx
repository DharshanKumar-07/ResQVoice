import { useState, useEffect, useRef, useCallback } from 'react';
import axios from 'axios';
import AgoraRTC from 'agora-rtc-sdk-ng';
import type { IAgoraRTCClient, IMicrophoneAudioTrack, IRemoteAudioTrack } from 'agora-rtc-sdk-ng';
import { Mic, MicOff, Radio, Volume2, AlertCircle, CheckCircle2, Users } from 'lucide-react';
import { useMixedAudioTranscription } from '../hooks/useMixedAudioTranscription';
import type { RecorderStatus } from '../hooks/useMixedAudioTranscription';
import type { TranscriptEvent } from '../types';
import { installAgoraWebRtcCompatibility } from '../utils/agoraWebRtcCompatibility';

const ROLES = [
  "Incident Commander", "Backend Engineer", "DevOps/SRE", 
  "Database Engineer", "Security Engineer", "Support Engineer", 
  "Product Manager", "Business Stakeholder"
];

const BACKEND_URL = 'http://localhost:8000';
const CHANNEL_NAME = 'incident-room';
const AGORA_JOIN_TIMEOUT_MS = 20_000;

installAgoraWebRtcCompatibility();

function withTimeout<T>(promise: Promise<T>, timeoutMs: number, message: string): Promise<T> {
  return new Promise((resolve, reject) => {
    const timeout = window.setTimeout(() => reject(new Error(message)), timeoutMs);
    promise.then(
      value => {
        window.clearTimeout(timeout);
        resolve(value);
      },
      reason => {
        window.clearTimeout(timeout);
        reject(reason);
      },
    );
  });
}

const RECORDER_STATUS_LABELS: Record<RecorderStatus, string> = {
  idle: 'Recorder idle',
  'waiting-for-audio': 'Waiting for audio track',
  listening: 'Listening — next upload in up to 15 seconds',
  'speech-detected': 'Speech detected',
  uploading: 'Uploading audio for transcription…',
  'rate-limited': 'Rate limited — audio upload paused',
  'background-paused': 'Paused while tab is in the background',
};

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------
export default function AgoraRoom() {
  const [joined, setJoined] = useState(false);
  const [joining, setJoining] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [role, setRole] = useState(ROLES[0]);
  const [speaker, setSpeaker] = useState("Priya");
  const [transcript, setTranscript] = useState<TranscriptEvent[]>([]);
  const [isMuted, setIsMuted] = useState(false);
  const [joinedUid, setJoinedUid] = useState<number | null>(null);
  const [remoteUsers, setRemoteUsers] = useState<string[]>([]);
  
  const clientRef = useRef<IAgoraRTCClient | null>(null);
  const localAudioTrackRef = useRef<IMicrophoneAudioTrack | null>(null);
  const transcriptCursorRef = useRef(0);

  // FALLBACK: Demo-mode local-only STT via Web Speech API.
  // This is a documented fallback that only captures the local tab's own
  // microphone. It does NOT capture remote participants' audio from Agora.
  // The real pipeline is: Agora audio → MediaRecorder → /api/audio/transcribe → Gemini STT.
  const recognitionRef = useRef<any>(null);
  const [fallbackMode, setFallbackMode] = useState(false);

  useEffect(() => {
    const SpeechRecognition = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
    if (SpeechRecognition) {
      const recognition = new SpeechRecognition();
      recognition.continuous = true;
      recognition.interimResults = false;
      
      recognition.onresult = (event: any) => {
        const text = event.results[event.results.length - 1][0].transcript;
        axios.post(`${BACKEND_URL}/api/transcript`, {
          speaker,
          role,
          text,
          timestamp: new Date().toISOString()
        }).then(response => {
          const eventId = Number(response.data?.event_id) || -Date.now();
          setTranscript(prev => prev.some(item => item.id === eventId) ? prev : [...prev, {
            id: eventId,
            speaker,
            role: `${role} (fallback)`,
            text,
            timestamp: new Date().toISOString(),
          }]);
        }).catch(console.error);
      };

      recognition.onerror = (event: any) => {
        console.warn('[WebSpeech Fallback] Error:', event.error);
      };

      recognitionRef.current = recognition;
    }
  }, [speaker, role]);

  const addTranscript = useCallback((text: string, spk: string, eventId?: number) => {
    const id = eventId ?? -Date.now();
    setTranscript(prev => prev.some(item => item.id === id) ? prev : [...prev, {
      id,
      speaker: spk,
      role,
      text,
      timestamp: new Date().toISOString(),
    }]);
  }, [role]);

  const {
    addTrack: addMixedTrack,
    removeTrack: removeMixedTrack,
    stop: stopMixedAudio,
    error: transcriptionError,
    notice: transcriptionNotice,
    status: recorderStatus,
    clearError: clearTranscriptionError,
  } = useMixedAudioTranscription(
    joined,
    String(joinedUid ?? 0),
    speaker.trim() || `Participant ${joinedUid ?? ''}`.trim(),
    role,
    addTranscript,
  );

  useEffect(() => {
    if (!joined && !fallbackMode) return;

    let cancelled = false;
    const syncTranscripts = async () => {
      try {
        const response = await axios.get<TranscriptEvent[]>(`${BACKEND_URL}/api/transcripts`, {
          params: { after_id: transcriptCursorRef.current, limit: 100 },
        });
        if (cancelled || response.data.length === 0) return;
        transcriptCursorRef.current = Math.max(
          transcriptCursorRef.current,
          ...response.data.map(item => item.id),
        );
        setTranscript(previous => {
          const byId = new Map(previous.map(item => [item.id, item]));
          response.data.forEach(item => byId.set(item.id, item));
          return Array.from(byId.values()).sort((a, b) => a.id - b.id).slice(-100);
        });
      } catch (syncError) {
        console.warn('[Transcript Sync] Could not fetch recent transcripts:', syncError);
      }
    };

    void syncTranscripts();
    const interval = window.setInterval(syncTranscripts, 1_500);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [joined, fallbackMode]);

  const stopAgoraSession = useCallback(async () => {
    stopMixedAudio();

    if (localAudioTrackRef.current) {
      localAudioTrackRef.current.close();
      localAudioTrackRef.current = null;
    }

    const client = clientRef.current;
    clientRef.current = null;
    if (client) {
      client.removeAllListeners();
      try {
        await client.leave();
      } catch (leaveError) {
        console.warn('[Agora] Failed to leave cleanly:', leaveError);
      }
    }
  }, [stopMixedAudio]);

  useEffect(() => {
    return () => {
      void stopAgoraSession();
      if (recognitionRef.current) {
        try { recognitionRef.current.stop(); } catch {}
      }
    };
  }, [stopAgoraSession]);

  // ── Join Room ──────────────────────────────────────────────────────────
  const joinRoom = async () => {
    setJoining(true);
    setError(null);
    clearTranscriptionError();

    try {
      // 1. Generate a random UID for this participant
      const uid = Math.floor(Math.random() * 100000) + 1;

      // 2. Fetch token from backend
      console.log(`[Agora] Requesting token for channel=${CHANNEL_NAME} uid=${uid}...`);
      const tokenResp = await axios.post(`${BACKEND_URL}/api/agora/token`, {
        channel: CHANNEL_NAME,
        uid: uid,
      });

      const { token, app_id } = tokenResp.data;
      console.log(`[Agora] Token received. app_id=${app_id}`);

      // 3. Create Agora client and join
      const client = AgoraRTC.createClient({ mode: "rtc", codec: "vp8" });
      clientRef.current = client;

      // Register listeners before joining so no remote publication event is missed.
      client.on("user-published", async (user, mediaType) => {
        if (mediaType !== "audio") return;

        const remoteUid = String(user.uid);
        try {
          await client.subscribe(user, mediaType);
          const remoteAudioTrack = user.audioTrack as IRemoteAudioTrack | undefined;
          if (!remoteAudioTrack) {
            throw new Error(`No remote audio track available for uid=${remoteUid}`);
          }
          console.log(`[Agora] Subscribed to remote user uid=${remoteUid}`);

          remoteAudioTrack.play();

          await addMixedTrack(`remote:${remoteUid}`, remoteAudioTrack.getMediaStreamTrack());
          setRemoteUsers(prev => prev.includes(remoteUid) ? prev : [...prev, remoteUid]);
        } catch (subscribeError) {
          console.error(`[Agora] Failed to capture remote uid=${remoteUid}:`, subscribeError);
        }
      });

      const stopRemoteCapture = (uidToStop: string) => {
        removeMixedTrack(`remote:${uidToStop}`);
        setRemoteUsers(prev => prev.filter(id => id !== uidToStop));
      };

      client.on("user-unpublished", (user, mediaType) => {
        if (mediaType === "audio") {
          const remoteUid = String(user.uid);
          console.log(`[Agora] Remote user uid=${remoteUid} unpublished audio.`);
          stopRemoteCapture(remoteUid);
        }
      });

      client.on("user-left", user => {
        const remoteUid = String(user.uid);
        console.log(`[Agora] Remote user uid=${remoteUid} left.`);
        stopRemoteCapture(remoteUid);
      });

      const resolvedUid = await withTimeout(
        client.join(app_id, CHANNEL_NAME, token, uid),
        AGORA_JOIN_TIMEOUT_MS,
        'Agora join timed out after 20 seconds. Check browser/WebRTC compatibility.',
      );
      console.log(`✅ Agora joined channel=${CHANNEL_NAME} uid=${resolvedUid}`);
      setJoinedUid(resolvedUid as number);

      // 4. Create and publish local audio track
      const localTrack = await AgoraRTC.createMicrophoneAudioTrack();
      localAudioTrackRef.current = localTrack;
      console.log(`[Agora] Local audio track created:`, localTrack.getTrackId());

      await client.publish([localTrack]);
      console.log(`[Agora] Local audio track published successfully.`);

      // 5. Add local and remote tracks to one mono, VAD-gated recorder.
      await addMixedTrack('local', localTrack.getMediaStreamTrack());

      setJoined(true);
      setFallbackMode(false);

    } catch (e: any) {
      const errorMsg = e?.response?.data?.detail || e?.message || String(e);
      console.error(`❌ Agora join failed:`, errorMsg);
      setError(`Join failed: ${errorMsg}`);
      setJoined(false);
      setJoinedUid(null);
      setRemoteUsers([]);
      await stopAgoraSession();

      // FALLBACK: If Agora join fails, activate Web Speech API as demo mode
      if (recognitionRef.current) {
        console.warn('[Fallback] Activating Web Speech API demo mode.');
        try {
          recognitionRef.current.start();
          setFallbackMode(true);
        } catch (fallbackError) {
          console.warn('[Fallback] Web Speech API could not start:', fallbackError);
          setFallbackMode(false);
        }
      } else {
        setFallbackMode(false);
      }
    } finally {
      setJoining(false);
    }
  };

  // ── Leave Room ─────────────────────────────────────────────────────────
  const leaveRoom = async () => {
    await stopAgoraSession();
    if (recognitionRef.current) {
      try { recognitionRef.current.stop(); } catch {}
    }
    setJoined(false);
    setJoinedUid(null);
    setRemoteUsers([]);
    setError(null);
    clearTranscriptionError();
    setFallbackMode(false);
    setIsMuted(false);
  };

  const toggleMute = async () => {
    if (localAudioTrackRef.current) {
      await localAudioTrackRef.current.setEnabled(isMuted);
    } else if (fallbackMode && recognitionRef.current) {
      try {
        if (isMuted) recognitionRef.current.start();
        else recognitionRef.current.stop();
      } catch (fallbackError) {
        console.warn('[Fallback] Could not change microphone state:', fallbackError);
      }
    }
    setIsMuted(!isMuted);
  };

  // ── Render ─────────────────────────────────────────────────────────────
  const sessionActive = joined || fallbackMode;

  return (
    <div className="dashboard-container voice-container">
      <header className="page-header">
        <div>
          <h1 className="page-title">
            <Radio className="page-title-icon" size={23} aria-hidden="true" />
            Voice incident room
          </h1>
          <p className="page-description">
            Connect responders, capture operational context, and transcribe the active channel
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
              onClick={joinRoom}
              disabled={joining}
            >
              <Mic size={17} aria-hidden="true" />
              <span>{joining ? 'Connecting...' : 'Enter Incident Room & Start Speaking'}</span>
            </button>
          </div>
        ) : (
          <div>
            {(transcriptionError || transcriptionNotice) && (
              <div className="notice-banner notice-warning" role="alert">
                <AlertCircle size={16} aria-hidden="true" />
                <span>{transcriptionError || transcriptionNotice}</span>
              </div>
            )}

            {error && (
              <div className="notice-banner notice-error" role="alert">
                <AlertCircle size={16} aria-hidden="true" />
                <span>{error}{fallbackMode ? ' — using local-only demo transcription.' : ''}</span>
              </div>
            )}

            <div className="voice-session">
              <div className="voice-session-identity">
                <div className={!isMuted ? "pulse-dot" : ""} style={isMuted ? { width: 8, height: 8, borderRadius: '50%', background: 'var(--text-muted)' } : {}} />
                <div>
                  <div className="voice-session-title">
                    {fallbackMode ? (
                      <>
                        <AlertCircle size={16} aria-hidden="true" />
                        <span>Demo Mode: {speaker}</span>
                      </>
                    ) : (
                      <>
                        <CheckCircle2 size={16} aria-hidden="true" style={{ color: 'var(--status-confirmed)' }} />
                        <span>Connected: {speaker} <span style={{ color: 'var(--text-muted)', fontSize: 12, fontWeight: 400 }}>(uid: {joinedUid})</span></span>
                      </>
                    )}
                  </div>
                  <div className="voice-session-role">
                    {role}
                    {fallbackMode && ' — Web Speech API fallback (local mic only)'}
                  </div>
                </div>
              </div>
              
              <div className="voice-actions">
                {!fallbackMode && (
                  <div className="participant-count" title="Connected participants">
                    <Users size={15} aria-hidden="true" />
                    <span>{1 + remoteUsers.length}</span>
                  </div>
                )}
                <button className="btn btn-secondary" onClick={toggleMute}>
                  {isMuted ? <MicOff size={16} aria-hidden="true" /> : <Mic size={16} aria-hidden="true" />}
                  <span>{isMuted ? 'Unmute' : 'Mute'}</span>
                </button>
                <button className="btn btn-danger" onClick={leaveRoom}>
                  Leave Room
                </button>
              </div>
            </div>

            <div className="stream-section">
              <div className="stream-heading">
                <div className="stream-heading-title">
                  <Volume2 size={16} aria-hidden="true" />
                  <span>Live Speech Recognition</span>
                </div>
                {!isMuted && (
                  <div className="stream-indicator">
                    <span>Listening...</span>
                    <div className={`waveform-container ${isMuted ? 'muted' : ''}`}>
                      <div className="waveform-bar" />
                      <div className="waveform-bar" />
                      <div className="waveform-bar" />
                      <div className="waveform-bar" />
                      <div className="waveform-bar" />
                    </div>
                  </div>
                )}
              </div>
              <div className="stream-window" aria-live="polite">
                {transcript.length === 0 ? (
                  <p className="stream-empty">
                    {fallbackMode
                      ? 'Demo mode: Speaking into your microphone will transcribe via browser STT. Remote participants are not captured in this mode.'
                      : 'Speak normally. Audible room audio is mixed into one stream and sent to Gemini every 15 seconds; silent chunks and background tabs are skipped.'}
                  </p>
                ) : (
                  transcript.map(item => (
                    <div key={item.id} className="stream-row">
                      <div>
                        <span className="stream-speaker">{item.speaker}</span>
                        <span className="stream-role">({item.role})</span>
                        <span style={{ float: 'right', color: 'var(--text-muted)', fontSize: 11, fontFamily: 'var(--font-mono)' }}>
                          {new Date(item.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
                        </span>
                      </div>
                      <span className="stream-text">{item.text}</span>
                    </div>
                  ))
                )}
              </div>
              {!fallbackMode && (
                <div className="pipeline-status" data-state={recorderStatus}>
                  Audio pipeline: {RECORDER_STATUS_LABELS[recorderStatus]}
                </div>
              )}
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
