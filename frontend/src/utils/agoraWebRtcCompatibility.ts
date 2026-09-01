const PATCH_FLAG = '__resqvoiceAgoraSdpCompatibilityInstalled';

type PatchedWindow = Window & {
  __resqvoiceAgoraSdpCompatibilityInstalled?: boolean;
};

function sanitizeAgoraIncompatibleIceOptions(sdp?: string): string | undefined {
  if (!sdp || !sdp.includes('goog-sped-v1')) return sdp;

  return sdp.replace(/^a=ice-options:([^\r\n]*)$/gm, (line, options: string) => {
    const supportedOptions = options
      .trim()
      .split(/\s+/)
      .filter(option => option !== 'goog-sped-v1');
    return supportedOptions.length > 0
      ? `a=ice-options:${supportedOptions.join(' ')}`
      : line;
  });
}

/**
 * Chromium 151 adds `goog-sped-v1` beside `trickle` in ICE options. Agora Web
 * SDK 4.24.x currently parses that standards-valid multi-token attribute as a
 * single token and throws `GET_LOCAL_CONNECTION_PARAMS_FAILED: Invalid space`.
 * Sanitize only that experimental option before Agora sees the local offer.
 */
export function installAgoraWebRtcCompatibility() {
  const patchedWindow = window as PatchedWindow;
  if (patchedWindow[PATCH_FLAG] || !window.RTCPeerConnection) return;

  const prototype = window.RTCPeerConnection.prototype as any;
  const originalCreateOffer = prototype.createOffer;
  const originalCreateAnswer = prototype.createAnswer;

  const wrapDescriptionFactory = (original: (...args: any[]) => Promise<RTCSessionDescriptionInit>) => (
    async function(this: RTCPeerConnection, ...args: any[]) {
      const description = await original.apply(this, args);
      const sanitizedSdp = sanitizeAgoraIncompatibleIceOptions(description.sdp);
      if (sanitizedSdp === description.sdp) return description;

      console.info('[Agora Compatibility] Removed Chromium goog-sped-v1 ICE option.');
      return { type: description.type, sdp: sanitizedSdp };
    }
  );

  prototype.createOffer = wrapDescriptionFactory(originalCreateOffer);
  prototype.createAnswer = wrapDescriptionFactory(originalCreateAnswer);
  patchedWindow[PATCH_FLAG] = true;
}
