import { useCallback, useEffect, useRef, useState } from 'react';
import axios from 'axios';
import AgoraRTC from 'agora-rtc-sdk-ng';
import type { IAgoraRTCClient, IMicrophoneAudioTrack, IRemoteAudioTrack } from 'agora-rtc-sdk-ng';
import { BACKEND_URL } from '../lib/backend';

const AGORA_JOIN_TIMEOUT_MS = 20_000;
const AGENT_UID = Number(import.meta.env.VITE_AGORA_AGENT_UID ?? 1000);
const RTC_UID_STORAGE_KEY = 'resqvoice-rtc-uid';

function stableRtcUid(): number {
  const stored = Number(window.sessionStorage.getItem(RTC_UID_STORAGE_KEY));
  if (Number.isInteger(stored) && stored >= 10_000 && stored <= 999_999) return stored;
  const generated = Math.floor(Math.random() * 900_000) + 10_000;
  window.sessionStorage.setItem(RTC_UID_STORAGE_KEY, String(generated));
  return generated;
}

export interface IncidentStateEvent {
  type: 'incident_state_snapshot' | 'incident_state_mutation' | 'speaking_started' | 'speaking_stopped';
  counts?: Record<string, number>;
  transcript?: { id: number; speaker: string; role: string; text: string; timestamp: string };
  mutations?: Record<string, string[]>;
  intervention_id?: string;
  trigger_type?: string;
  message?: string;
  success?: boolean;
}

export type SpeechPhase = 'idle' | 'speech_detected' | 'transcribing';

interface SpeakingIntent {
  interventionId?: string;
  triggerType: string;
  message?: string;
}

function withTimeout<T>(promise: Promise<T>, timeoutMs: number, message: string): Promise<T> {
  return new Promise((resolve, reject) => {
    const timeout = window.setTimeout(() => reject(new Error(message)), timeoutMs);
    promise.then(
      value => { window.clearTimeout(timeout); resolve(value); },
      reason => { window.clearTimeout(timeout); reject(reason); },
    );
  });
}

function stateWebSocketUrl(): string {
  const base = new URL(BACKEND_URL, window.location.href);
  base.protocol = base.protocol === 'https:' ? 'wss:' : 'ws:';
  base.pathname = '/api/agora-agent/state/ws';
  base.search = '';
  return base.toString();
}

function readableError(value: unknown): string {
  if (typeof value === 'string' && value.trim()) return value;
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    for (const key of ['detail', 'message', 'reason', 'error']) {
      const candidate = record[key];
      if (typeof candidate === 'string' && candidate.trim()) return candidate;
    }
    try { return JSON.stringify(value); } catch { /* fall through */ }
  }
  return 'The voice agent could not start. Check the backend log for details.';
}

export function useAgoraIncidentRoom() {
  const [joined, setJoined] = useState(false);
  const [joining, setJoining] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [joinedUid, setJoinedUid] = useState<number | null>(null);
  const [remoteUsers, setRemoteUsers] = useState<string[]>([]);
  const [isMuted, setIsMuted] = useState(false);
  const [agentConnected, setAgentConnected] = useState(false);
  const [stateConnected, setStateConnected] = useState(false);
  const [incidentEvents, setIncidentEvents] = useState<IncidentStateEvent[]>([]);
  const [speechPhase, setSpeechPhase] = useState<SpeechPhase>('idle');
  const [speakingIntent, setSpeakingIntent] = useState<SpeakingIntent | null>(null);
  const [agentAudioActive, setAgentAudioActive] = useState(false);

  const clientRef = useRef<IAgoraRTCClient | null>(null);
  const localAudioTrackRef = useRef<IMicrophoneAudioTrack | null>(null);
  const agentSessionIdRef = useRef<string | null>(null);
  const stateSocketRef = useRef<WebSocket | null>(null);
  const stateReconnectTimerRef = useRef<number | null>(null);
  const shouldReconnectStateRef = useRef(false);
  const localVolumeTimerRef = useRef<number | null>(null);
  const agentVolumeTimerRef = useRef<number | null>(null);
  const transcribingTimeoutRef = useRef<number | null>(null);
  const agentAudioTrackRef = useRef<IRemoteAudioTrack | null>(null);

  const stopActivityMonitoring = useCallback(() => {
    if (localVolumeTimerRef.current !== null) window.clearInterval(localVolumeTimerRef.current);
    if (agentVolumeTimerRef.current !== null) window.clearInterval(agentVolumeTimerRef.current);
    if (transcribingTimeoutRef.current !== null) window.clearTimeout(transcribingTimeoutRef.current);
    localVolumeTimerRef.current = null;
    agentVolumeTimerRef.current = null;
    transcribingTimeoutRef.current = null;
    agentAudioTrackRef.current = null;
    setSpeechPhase('idle');
    setAgentAudioActive(false);
    setSpeakingIntent(null);
  }, []);

  const closeStateSocket = useCallback(() => {
    shouldReconnectStateRef.current = false;
    if (stateReconnectTimerRef.current !== null) {
      window.clearTimeout(stateReconnectTimerRef.current);
      stateReconnectTimerRef.current = null;
    }
    stateSocketRef.current?.close();
    stateSocketRef.current = null;
    setStateConnected(false);
  }, []);

  const leaveRoom = useCallback(async () => {
    const agentId = agentSessionIdRef.current;
    agentSessionIdRef.current = null;
    if (agentId) {
      try { await axios.post(`${BACKEND_URL}/api/agora/agent/stop/${encodeURIComponent(agentId)}`); }
      catch (stopError) { console.warn('[Agora Agent] Stop failed:', stopError); }
    }
    closeStateSocket();
    stopActivityMonitoring();
    localAudioTrackRef.current?.close();
    localAudioTrackRef.current = null;
    const client = clientRef.current;
    clientRef.current = null;
    if (client) {
      client.removeAllListeners();
      try { await client.leave(); } catch (leaveError) { console.warn('[Agora RTC] Leave failed:', leaveError); }
    }
    setJoined(false);
    setJoinedUid(null);
    setRemoteUsers([]);
    setAgentConnected(false);
    setIsMuted(false);
  }, [closeStateSocket, stopActivityMonitoring]);

  const connectStateSocket = useCallback(() => {
    closeStateSocket();
    shouldReconnectStateRef.current = true;
    const open = () => {
      if (!shouldReconnectStateRef.current) return;
      const socket = new WebSocket(stateWebSocketUrl());
      stateSocketRef.current = socket;
      socket.onopen = () => setStateConnected(true);
      socket.onclose = () => {
        setStateConnected(false);
        if (shouldReconnectStateRef.current) {
          stateReconnectTimerRef.current = window.setTimeout(open, 1500);
        }
      };
      socket.onerror = () => setStateConnected(false);
      socket.onmessage = message => {
        try {
          const event = JSON.parse(message.data) as IncidentStateEvent;
          setIncidentEvents(previous => [...previous.slice(-99), event]);
          if (event.type === 'speaking_started') {
            setSpeakingIntent({
              interventionId: event.intervention_id,
              triggerType: event.trigger_type ?? 'VOICE_INTERVENTION',
              message: event.message,
            });
          } else if (event.type === 'speaking_stopped') {
            setSpeakingIntent(null);
          }
          if (event.type === 'incident_state_mutation' && event.transcript) {
            setSpeechPhase('idle');
            if (transcribingTimeoutRef.current !== null) {
              window.clearTimeout(transcribingTimeoutRef.current);
              transcribingTimeoutRef.current = null;
            }
          }
        } catch (parseError) {
          console.warn('[Incident State] Ignoring malformed WebSocket event:', parseError);
        }
      };
    };
    open();
  }, [closeStateSocket]);

  const toggleMute = useCallback(async () => {
    const track = localAudioTrackRef.current;
    if (!track) return;
    await track.setEnabled(isMuted);
    setIsMuted(previous => !previous);
  }, [isMuted]);

  const joinRoom = useCallback(async (channelName: string, speakerName: string, role: string) => {
    setJoining(true);
    setError(null);
    try {
      // Keep one UID for the life of this browser tab. Reusing a cloud agent
      // that subscribed to a newly-randomized UID makes it look online while
      // receiving no microphone audio.
      const uid = stableRtcUid();
      const tokenResponse = await axios.post(`${BACKEND_URL}/api/agora/token`, { channel: channelName, uid });
      const { token, app_id: appId } = tokenResponse.data;
      const client = AgoraRTC.createClient({ mode: 'rtc', codec: 'vp8' });
      clientRef.current = client;

      const removeRemote = (remoteUid: string) => {
        setRemoteUsers(previous => previous.filter(value => value !== remoteUid));
        if (remoteUid === String(AGENT_UID)) {
          setAgentConnected(false);
          setAgentAudioActive(false);
          agentAudioTrackRef.current = null;
        }
      };
      client.on('user-published', async (user, mediaType) => {
        if (mediaType !== 'audio') return;
        await client.subscribe(user, mediaType);
        const track = user.audioTrack as IRemoteAudioTrack | undefined;
        if (!track) return;
        track.play();
        const remoteUid = String(user.uid);
        setRemoteUsers(previous => previous.includes(remoteUid) ? previous : [...previous, remoteUid]);
        if (remoteUid === String(AGENT_UID)) setAgentConnected(true);
        if (remoteUid === String(AGENT_UID)) {
          agentAudioTrackRef.current = track;
          let lastAudibleAt = 0;
          if (agentVolumeTimerRef.current !== null) window.clearInterval(agentVolumeTimerRef.current);
          agentVolumeTimerRef.current = window.setInterval(() => {
            const level = agentAudioTrackRef.current?.getVolumeLevel() ?? 0;
            if (level >= 0.01) {
              lastAudibleAt = Date.now();
              setAgentAudioActive(true);
            } else if (Date.now() - lastAudibleAt > 650) {
              setAgentAudioActive(false);
            }
          }, 100);
        }
      });
      client.on('user-unpublished', (user, mediaType) => {
        if (mediaType === 'audio') removeRemote(String(user.uid));
      });
      client.on('user-left', user => removeRemote(String(user.uid)));

      const resolvedUid = await withTimeout(
        client.join(appId, channelName, token, uid), AGORA_JOIN_TIMEOUT_MS,
        'Agora join timed out. Check RTC credentials and browser WebRTC access.',
      );
      const microphone = await AgoraRTC.createMicrophoneAudioTrack();
      localAudioTrackRef.current = microphone;
      await client.publish(microphone);
      const speechThreshold = Number(import.meta.env.VITE_SPEECH_ACTIVITY_THRESHOLD ?? 0.025);
      const silenceGapMs = Number(import.meta.env.VITE_SPEECH_SILENCE_GAP_MS ?? 500);
      let localSpeechActive = false;
      let lastSpeechAt = 0;
      localVolumeTimerRef.current = window.setInterval(() => {
        const level = microphone.getVolumeLevel();
        if (level >= speechThreshold) {
          localSpeechActive = true;
          lastSpeechAt = Date.now();
          setSpeechPhase('speech_detected');
          if (transcribingTimeoutRef.current !== null) {
            window.clearTimeout(transcribingTimeoutRef.current);
            transcribingTimeoutRef.current = null;
          }
        } else if (localSpeechActive && Date.now() - lastSpeechAt >= silenceGapMs) {
          localSpeechActive = false;
          setSpeechPhase('transcribing');
          transcribingTimeoutRef.current = window.setTimeout(
            () => setSpeechPhase('idle'),
            12_000,
          );
        }
      }, 100);
      setJoinedUid(Number(resolvedUid));
      setJoined(true);
      connectStateSocket();

      const agentTokenResponse = await axios.post(`${BACKEND_URL}/api/agora/token`, {
        channel: channelName, uid: AGENT_UID,
      });
      await axios.post(`${BACKEND_URL}/api/agora-agent/participants`, {
        agora_uid: String(resolvedUid),
        user_id: String(resolvedUid),
        display_name: speakerName,
        role,
        participant_type: 'human',
        channel: channelName,
      });
      const agentResponse = await axios.post(`${BACKEND_URL}/api/agora/agent/start`, {
        channel_name: channelName,
        agent_uid: AGENT_UID,
        token: agentTokenResponse.data.token,
        // Agora currently supports one subscribed remote RTC UID per agent.
        // Use the concrete browser UID so the agent receives this speaker's
        // audio and the callback always has deterministic attribution.
        remote_rtc_uids: [String(resolvedUid)],
        speaker_uid: String(resolvedUid),
        speaker_name: speakerName,
        speaker_role: role,
      });
      agentSessionIdRef.current = agentResponse.data.agent_id ?? agentResponse.data.session_id;
      if (agentResponse.data.callback_ready !== true) {
        throw new Error(
          'The voice agent joined RTC, but its public callback has not passed the readiness check.',
        );
      }
      // The agent may not publish an audio track until its first utterance, so a
      // successful session start is the authoritative readiness signal.
      setAgentConnected(true);
    } catch (cause: unknown) {
      const responseBody = axios.isAxiosError(cause) ? cause.response?.data : undefined;
      const message = readableError(responseBody ?? (cause instanceof Error ? cause.message : cause));
      setError(`Join failed: ${message}`);
      await leaveRoom();
    } finally {
      setJoining(false);
    }
  }, [connectStateSocket, leaveRoom]);

  useEffect(() => () => { void leaveRoom(); }, [leaveRoom]);

  return {
    joined, joining, error, joinedUid, remoteUsers, isMuted,
    agentConnected, stateConnected, incidentEvents,
    speechPhase,
    engineSpeaking: speakingIntent !== null && agentAudioActive,
    speakingTrigger: speakingIntent?.triggerType ?? null,
    speakingMessage: speakingIntent?.message ?? null,
    joinRoom, leaveRoom, toggleMute,
  };
}
