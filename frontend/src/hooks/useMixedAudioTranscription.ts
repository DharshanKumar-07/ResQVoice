import { useCallback, useEffect, useState } from 'react';


const BACKEND_URL = 'http://localhost:8000';
const AUDIO_CHUNK_INTERVAL_MS = 15_000;
const MIN_AUDIO_BYTES = 1_000;
const SILENCE_RMS_THRESHOLD = 0.01;
const DEFAULT_RETRY_SECONDS = 15;
const TRANSCRIPTION_TIMEOUT_MS = 60_000;
const MAX_TRANSIENT_ATTEMPTS = 3;

export type RecorderStatus =
  | 'idle'
  | 'waiting-for-audio'
  | 'listening'
  | 'speech-detected'
  | 'uploading'
  | 'rate-limited'
  | 'background-paused';

type TranscriptCallback = (text: string, speaker: string, eventId?: number) => void;

interface RecorderOptions {
  uid: string;
  speaker: string;
  role: string;
  onTranscript: TranscriptCallback;
  onError: (message: string | null) => void;
  onNotice: (message: string | null) => void;
  onStatus: (status: RecorderStatus) => void;
}

interface ErrorPayload {
  status?: string;
  code?: string;
  message?: string;
  transcript?: string;
  event_id?: number;
}

interface QueuedAudioChunk {
  blob: Blob;
  sequence: number;
}

class MixedAudioRecorder {
  private options: RecorderOptions;
  private audioContext: AudioContext | null = null;
  private merger: ChannelMergerNode | null = null;
  private analyser: AnalyserNode | null = null;
  private destination: MediaStreamAudioDestinationNode | null = null;
  private sources = new Map<string, MediaStreamAudioSourceNode>();
  private recorder: MediaRecorder | null = null;
  private chunkTimer: number | null = null;
  private vadTimer: number | null = null;
  private retryTimer: number | null = null;
  private retryResolve: (() => void) | null = null;
  private enabled = false;
  private speechDetected = false;
  private pendingChunks: QueuedAudioChunk[] = [];
  private processingQueue = false;
  private nextSequence = 1;
  private activeRequestController: AbortController | null = null;

  constructor(options: RecorderOptions) {
    this.options = options;
  }

  updateOptions(options: RecorderOptions) {
    this.options = options;
  }

  async addTrack(id: string, track: MediaStreamTrack) {
    this.ensureAudioGraph();
    this.removeTrack(id);

    const source = this.audioContext!.createMediaStreamSource(new MediaStream([track]));
    // A one-channel merger sums every connected source into a single mono feed.
    source.connect(this.merger!, 0, 0);
    this.sources.set(id, source);
    await this.audioContext!.resume();
    this.options.onStatus('listening');
    this.startCycleIfReady();
  }

  removeTrack(id: string) {
    this.sources.get(id)?.disconnect();
    this.sources.delete(id);
  }

  start() {
    this.enabled = true;
    document.addEventListener('visibilitychange', this.handleVisibilityChange);
    this.startVad();
    this.options.onStatus(this.sources.size > 0 ? 'listening' : 'waiting-for-audio');
    this.startCycleIfReady();
  }

  stop() {
    this.enabled = false;
    document.removeEventListener('visibilitychange', this.handleVisibilityChange);
    this.clearTimers();
    if (this.recorder && this.recorder.state !== 'inactive') this.recorder.stop();
    this.recorder = null;
    this.sources.forEach(source => source.disconnect());
    this.sources.clear();
    this.merger?.disconnect();
    this.analyser?.disconnect();
    void this.audioContext?.close();
    this.audioContext = null;
    this.merger = null;
    this.analyser = null;
    this.destination = null;
    this.speechDetected = false;
    this.pendingChunks = [];
    this.activeRequestController?.abort();
    this.activeRequestController = null;
    this.retryResolve?.();
    this.retryResolve = null;
    this.options.onNotice(null);
    this.options.onStatus('idle');
  }

  private ensureAudioGraph() {
    if (this.audioContext) return;

    this.audioContext = new AudioContext();
    this.merger = this.audioContext.createChannelMerger(1);
    this.analyser = this.audioContext.createAnalyser();
    this.analyser.fftSize = 2048;
    this.destination = this.audioContext.createMediaStreamDestination();
    this.destination.channelCount = 1;
    this.merger.connect(this.analyser);
    this.analyser.connect(this.destination);
  }

  private startVad() {
    if (this.vadTimer !== null || !this.analyser) return;
    const samples = new Float32Array(this.analyser.fftSize);
    this.vadTimer = window.setInterval(() => {
      if (!this.enabled || document.visibilityState !== 'visible' || !this.analyser) return;
      this.analyser.getFloatTimeDomainData(samples);
      let sumSquares = 0;
      for (const sample of samples) sumSquares += sample * sample;
      const rms = Math.sqrt(sumSquares / samples.length);
      if (rms >= SILENCE_RMS_THRESHOLD && !this.speechDetected) {
        this.speechDetected = true;
        this.options.onStatus('speech-detected');
      }
    }, 250);
  }

  private startCycleIfReady = () => {
    if (
      !this.enabled ||
      document.visibilityState !== 'visible' ||
      this.sources.size === 0 ||
      !this.destination ||
      (this.recorder && this.recorder.state !== 'inactive')
    ) return;

    const mimeType = this.selectMimeType();
    try {
      this.recorder = mimeType
        ? new MediaRecorder(this.destination.stream, { mimeType })
        : new MediaRecorder(this.destination.stream);
    } catch (error) {
      this.options.onError(`MediaRecorder could not start: ${String(error)}`);
      return;
    }

    this.speechDetected = false;
    this.recorder.ondataavailable = event => this.enqueueChunk(event.data);
    this.recorder.onerror = event => {
      this.options.onError(`Audio recorder error: ${event.type}`);
    };
    this.recorder.onstop = () => {
      this.recorder = null;
      // Restart immediately. The previous 100 ms delay created a real capture
      // gap at every chunk boundary and could clip short words.
      if (this.enabled) this.startCycleIfReady();
    };
    this.recorder.start();
    this.options.onStatus('listening');
    this.chunkTimer = window.setTimeout(() => {
      this.chunkTimer = null;
      if (this.recorder?.state === 'recording') this.recorder.stop();
    }, AUDIO_CHUNK_INTERVAL_MS);
  };

  private enqueueChunk(blob: Blob) {
    if (
      document.visibilityState !== 'visible' ||
      !this.speechDetected ||
      blob.size < MIN_AUDIO_BYTES
    ) {
      return;
    }

    this.pendingChunks.push({ blob, sequence: this.nextSequence++ });
    void this.processQueue();
  }

  private async processQueue() {
    if (this.processingQueue) return;
    this.processingQueue = true;

    try {
      while (this.enabled && this.pendingChunks.length > 0) {
        const chunk = this.pendingChunks[0];
        await this.submitQueuedChunk(chunk);
        // The queue head is removed only after success or a surfaced terminal
        // failure. A 429 always retries this exact Blob instance in place.
        this.pendingChunks.shift();
      }
    } finally {
      this.processingQueue = false;
      if (this.enabled && document.visibilityState === 'visible') {
        this.options.onStatus('listening');
      }
    }
  }

  private async submitQueuedChunk(chunk: QueuedAudioChunk) {
    const extension = chunk.blob.type.includes('ogg') ? 'ogg' : chunk.blob.type.includes('mp4') ? 'm4a' : 'webm';
    let attempt = 0;

    while (this.enabled) {
      attempt += 1;
      this.options.onStatus('uploading');
      const formData = new FormData();
      formData.append('audio', chunk.blob, `room-mix-${chunk.sequence}.${extension}`);
      formData.append('speaker', this.options.speaker);
      formData.append('role', this.options.role);
      formData.append('uid', this.options.uid);
      formData.append('sequence', String(chunk.sequence));
      formData.append('speech_detected', 'true');

      const controller = new AbortController();
      this.activeRequestController = controller;
      const timeout = window.setTimeout(() => controller.abort(), TRANSCRIPTION_TIMEOUT_MS);

      try {
        const response = await fetch(`${BACKEND_URL}/api/audio/transcribe`, {
          method: 'POST',
          body: formData,
          signal: controller.signal,
        });
        const data = await response.json() as ErrorPayload;

        if (!response.ok || data.status === 'error') {
          if (response.status === 429 || data.code === 'QUOTA_EXCEEDED') {
            const retrySeconds = Number(response.headers.get('Retry-After')) || DEFAULT_RETRY_SECONDS;
            await this.pauseForRateLimit(retrySeconds, chunk.sequence);
            continue;
          }
          throw new Error(data.message || `Transcription failed with HTTP ${response.status}`);
        }

        this.options.onError(null);
        this.options.onNotice(null);
        if (data.status === 'ok' && data.transcript) {
          this.options.onTranscript(data.transcript, this.options.speaker, data.event_id);
        }
        return;
      } catch (error) {
        if (!this.enabled) return;
        const timedOut = error instanceof DOMException && error.name === 'AbortError';
        const message = timedOut
          ? `Audio chunk ${chunk.sequence} timed out after 60s.`
          : error instanceof Error ? error.message : String(error);

        if (timedOut && attempt < MAX_TRANSIENT_ATTEMPTS) {
          this.options.onError(`${message} Retrying the same chunk.`);
          await this.waitForRetry(1_000);
          continue;
        }

        this.options.onError(`Transcription failed for audio chunk ${chunk.sequence}: ${message}`);
        return;
      } finally {
        window.clearTimeout(timeout);
        if (this.activeRequestController === controller) {
          this.activeRequestController = null;
        }
      }
    }
  }

  private async pauseForRateLimit(retrySeconds: number, sequence: number) {
    const message = `Transcription Rate Limit Hit (429) - Retrying audio chunk ${sequence} in ${retrySeconds}s`;
    this.options.onError(message);
    this.options.onNotice(message);
    this.options.onStatus('rate-limited');
    await this.waitForRetry(retrySeconds * 1000);
    this.options.onNotice(null);
  }

  private waitForRetry(delayMs: number) {
    return new Promise<void>(resolve => {
      this.retryResolve = resolve;
      this.retryTimer = window.setTimeout(() => {
        this.retryTimer = null;
        this.retryResolve = null;
        resolve();
      }, delayMs);
    });
  }

  private handleVisibilityChange = () => {
    if (document.visibilityState === 'hidden') {
      this.options.onNotice('Audio submission paused while this tab is in the background.');
      this.options.onStatus('background-paused');
      this.speechDetected = false;
      if (this.recorder?.state === 'recording') this.recorder.stop();
      return;
    }

    this.options.onNotice(null);
    this.options.onStatus(this.sources.size > 0 ? 'listening' : 'waiting-for-audio');
    void this.audioContext?.resume();
    this.startCycleIfReady();
  };

  private selectMimeType() {
    return [
      'audio/webm;codecs=opus',
      'audio/webm',
      'audio/ogg;codecs=opus',
      'audio/mp4',
    ].find(type => MediaRecorder.isTypeSupported(type));
  }

  private clearTimers() {
    if (this.chunkTimer !== null) window.clearTimeout(this.chunkTimer);
    if (this.vadTimer !== null) window.clearInterval(this.vadTimer);
    if (this.retryTimer !== null) window.clearTimeout(this.retryTimer);
    this.retryResolve?.();
    this.retryResolve = null;
    this.chunkTimer = null;
    this.vadTimer = null;
    this.retryTimer = null;
  }
}


export function useMixedAudioTranscription(
  enabled: boolean,
  uid: string,
  speaker: string,
  role: string,
  onTranscript: TranscriptCallback,
) {
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [status, setStatus] = useState<RecorderStatus>('idle');
  const [recorder] = useState(() => (
    new MixedAudioRecorder({
      uid,
      speaker,
      role,
      onTranscript,
      onError: setError,
      onNotice: setNotice,
      onStatus: setStatus,
    })
  ));

  useEffect(() => {
    recorder.updateOptions({
      uid,
      speaker,
      role,
      onTranscript,
      onError: setError,
      onNotice: setNotice,
      onStatus: setStatus,
    });
  }, [onTranscript, recorder, role, speaker, uid]);

  useEffect(() => {
    if (!enabled) {
      recorder.stop();
      return;
    }

    recorder.start();
    return () => recorder.stop();
  }, [enabled, recorder]);

  const addTrack = useCallback((id: string, track: MediaStreamTrack) => {
    return recorder.addTrack(id, track);
  }, [recorder]);

  const removeTrack = useCallback((id: string) => {
    recorder.removeTrack(id);
  }, [recorder]);

  const stop = useCallback(() => {
    recorder.stop();
  }, [recorder]);

  const clearError = useCallback(() => setError(null), []);

  return { addTrack, removeTrack, stop, error, notice, status, clearError };
}
