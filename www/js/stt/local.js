// Local, offline speech engine — the fallback for when the browser's cloud
// SpeechRecognition dies with "network"/"offline" (Chrome and Edge send audio
// to a vendor service; an offline laptop gets nothing).
//
// How it works, entirely on-device:
//   1. getUserMedia + AudioWorklet capture raw PCM (no network involved).
//   2. A small energy-based VAD segments utterances (speech / silence).
//   3. Each utterance is downsampled to 16 kHz mono 16-bit WAV and POSTed to
//      the agent at /api/stt/transcribe, which runs a local model (Vosk or
//      faster-whisper) if installed. The agent decides what the text means —
//      this engine only reports transcripts, like every other engine here.
//
// If no local model is installed on the agent, available() still returns true
// (the mic pipeline works); transcriptions fail with a clear message that says
// exactly what to install.
import { registerEngine } from './registry.js';

const SAMPLES_PER_CHUNK = 1200;          // ~75 ms at 16 kHz
const SPEECH_RMS = 0.012;                // above this → speech
const MIN_SPEECH_S = 0.3;                // shorter blips are noise
const MAX_UTTERANCE_S = 10;              // hard cap per utterance
const LEADING_KEEP_S = 0.3;              // keep a little pre-speech context
const TRAILING_SILENCE_S = 0.6;          // this much quiet ends an utterance

async function fetchWav(workletUrl) {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  });
  const ac = new AudioContext();
  await ac.audioWorklet.addModule(workletUrl);
  const node = new AudioWorkletNode(ac, 'pcm-capture');
  const mute = ac.createGain();
  mute.gain.value = 0;
  ac.createMediaStreamSource(stream).connect(node);
  node.connect(mute).connect(ac.destination);
  return { ac, stream, node };
}

function resampleTo16k(pcm, inRate) {
  const outRate = 16000;
  if (inRate === outRate) return pcm;
  const ratio = inRate / outRate, n = Math.floor(pcm.length / ratio), out = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const x = i * ratio, i0 = Math.floor(x), i1 = Math.min(i0 + 1, pcm.length - 1);
    out[i] = pcm[i0] + (pcm[i1] - pcm[i0]) * (x - i0);
  }
  return out;
}

function wavBytes(float32, rate) {
  const buf = new ArrayBuffer(44 + float32.length * 2), v = new DataView(buf);
  const str = (o, s) => [...s].forEach((ch, i) => v.setUint8(o + i, ch.charCodeAt(0)));
  str(0, 'RIFF'); v.setUint32(4, 36 + float32.length * 2, true); str(8, 'WAVE'); str(12, 'fmt ');
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  str(36, 'data'); v.setUint32(40, float32.length * 2, true);
  float32.forEach((s, i) => v.setInt16(44 + i * 2, Math.max(-1, Math.min(1, s)) * 0x7fff, true));
  return new Uint8Array(buf);
}

function bytesToB64(bytes) {
  let s = '';
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

registerEngine('local', {
  label: 'Local offline STT via agent (works without internet)',
  available: () => !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia
                      && window.AudioContext && window.AudioWorkletNode),
  create({ language, onResult, onState, onError }) {
    let wanted = false, stopping = false;
    let cap = null;                 // { ac, stream, node }
    let ring = [];                  // recent chunks (Float32Array at input rate)
    let ringSamples = 0;
    let utter = [];                 // chunks of the current utterance
    let utterSamples = 0, speechSamples = 0, silenceRun = 0;
    let inflight = 0;               // concurrent POSTs, to stay light on the agent

    const inputRate = () => (cap ? cap.ac.sampleRate : 48000);
    const secOf = (n) => n / inputRate();

    function resetSegments() {
      ring = []; ringSamples = 0; utter = []; utterSamples = 0;
      speechSamples = 0; silenceRun = 0;
    }

    function cleanupStream() {
      if (!cap) return;
      try { cap.node.disconnect(); } catch {}
      try { cap.stream.getTracks().forEach((t) => t.stop()); } catch {}
      try { cap.ac.close(); } catch {}
      cap = null;
    }

    async function transcribe(chunks, totalSamples) {
      const merged = new Float32Array(totalSamples);
      let o = 0;
      for (const c of chunks) { merged.set(c, o); o += c.length; }
      const pcm = resampleTo16k(merged, inputRate());
      const b64 = bytesToB64(wavBytes(pcm, 16000));
      inflight++;
      try {
        const resp = await fetch('/api/stt/transcribe', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ data_b64: b64, language: language || 'en-US' }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
          onError(data.detail || `Local transcription failed (HTTP ${resp.status})`);
          return;
        }
        const text = (data.text || '').trim();
        if (text) onResult({ text, final: true });
      } catch (e) {
        onError(`Local STT could not reach the agent: ${e.message}`);
      } finally {
        inflight--;
      }
    }

    function endUtterance() {
      if (utter.length && speechSamples >= MIN_SPEECH_S * inputRate()) {
        const payload = utter;
        const total = utterSamples;
        if (inflight < 2) transcribe(payload, total);   // drop if the agent is busy
      }
      resetSegments();
    }

    function onChunk(chunk) {
      if (!wanted || stopping) return;
      let sum = 0;
      for (let i = 0; i < chunk.length; i++) sum += chunk[i] * chunk[i];
      const rms = Math.sqrt(sum / chunk.length);
      const speaking = rms > SPEECH_RMS;

      ring.push(chunk); ringSamples += chunk.length;
      const keep = Math.ceil((LEADING_KEEP_S + MAX_UTTERANCE_S) * inputRate());
      while (ringSamples > keep) { ringSamples -= ring.shift().length; }

      if (speaking) {
        if (!utter.length) {                       // utterance start: seed with pre-speech ring
          const lead = Math.ceil(LEADING_KEEP_S * inputRate());
          let taken = 0;
          for (let i = ring.length - 1; i >= 0 && taken < lead; i--) {
            utter.unshift(ring[i]); taken += ring[i].length;
          }
          utterSamples = taken;
        }
        utter.push(chunk); utterSamples += chunk.length;
        speechSamples += chunk.length;
        silenceRun = 0;
        if (secOf(utterSamples) >= MAX_UTTERANCE_S) endUtterance();
      } else if (utter.length) {
        utter.push(chunk); utterSamples += chunk.length;
        silenceRun += chunk.length;
        if (secOf(silenceRun) >= TRAILING_SILENCE_S) endUtterance();
      }
    }

    async function run() {
      try {
        cap = await fetchWav('/static/js/pcm-worklet.js');
        cap.node.port.onmessage = (e) => onChunk(e.data);
        if (!wanted) { cleanupStream(); return; }
        onState('listening');
      } catch (e) {
        // Not-allowed / no-mic errors are real failures, not a silent stop.
        if (wanted && !stopping) {
          onError(e.name === 'NotAllowedError'
            ? 'Microphone access was blocked.'
            : `Local STT microphone failed: ${e.message}`);
        }
        onState('stopped');
      }
    }

    return {
      start() {
        wanted = true; stopping = false;
        resetSegments();
        run();
      },
      stop() {
        wanted = false; stopping = true;
        endUtterance();          // flush whatever the operator just said
        cleanupStream();
        onState('stopped');
      },
    };
  },
});
