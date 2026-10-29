// Camera and microphone helpers. getUserMedia works on http://127.0.0.1 (a secure context).

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export async function listDevices() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return { mics: [], cams: [] };
  const all = await navigator.mediaDevices.enumerateDevices();
  const pick = (kind) => all.filter((d) => d.kind === kind)
    .map((d, i) => ({ id: d.deviceId, label: d.label || `${kind === 'audioinput' ? 'Microphone' : 'Camera'} ${i + 1}` }));
  return { mics: pick('audioinput'), cams: pick('videoinput') };
}

export function bytesToB64(bytes) {
  let s = '';
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

export async function startCamera(video, deviceId) {
  const stream = await navigator.mediaDevices.getUserMedia({
    video: deviceId ? { deviceId: { exact: deviceId }, width: 640, height: 480 } : { width: 640, height: 480 },
  });
  video.srcObject = stream;
  await video.play().catch(() => {});
  return stream;
}

export function stopStream(stream) {
  if (stream) stream.getTracks().forEach((t) => t.stop());
}

export function frameFromVideo(video) {
  const c = document.createElement('canvas');
  c.width = video.videoWidth || 640;
  c.height = video.videoHeight || 480;
  c.getContext('2d').drawImage(video, 0, 0, c.width, c.height);
  return new Promise((resolve, reject) => c.toBlob(async (blob) => {
    if (!blob) return reject(new Error('Could not capture a camera frame'));
    resolve(bytesToB64(new Uint8Array(await blob.arrayBuffer())));
  }, 'image/jpeg', 0.9));
}

export async function previewFrameFromVideo(video, maxWidth = 480) {
  const c = document.createElement('canvas');
  const vw = video.videoWidth  || 640;
  const vh = video.videoHeight || 480;
  const scale = Math.min(1, maxWidth / vw);
  c.width  = Math.round(vw * scale);
  c.height = Math.round(vh * scale);
  c.getContext('2d').drawImage(video, 0, 0, c.width, c.height);
  return new Promise((resolve, reject) => c.toBlob(async (blob) => {
    if (!blob) return reject(new Error('preview capture failed'));
    resolve(bytesToB64(new Uint8Array(await blob.arrayBuffer())));
  }, 'image/jpeg', 0.6));
}
// One-off capture for unlocking: open the camera, let exposure settle, grab a frame, close it.
export async function captureFace(deviceId) {
  const video = document.createElement('video');
  video.muted = true;
  const stream = await startCamera(video, deviceId);
  try {
    await sleep(700);
    return await frameFromVideo(video);
  } finally {
    stopStream(stream);
  }
}

function resample(pcm, from, to) {
  if (from === to) return pcm;
  const ratio = from / to, n = Math.floor(pcm.length / ratio), out = new Float32Array(n);
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

// Record `seconds` of mono 16 kHz 16-bit WAV (what the voice engine expects). Returns base64.
export async function recordWav(seconds, deviceId) {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { deviceId: deviceId ? { exact: deviceId } : undefined, channelCount: 1,
             echoCancellation: false, noiseSuppression: false },
  });
  const ac = new AudioContext();
  try {
    await ac.audioWorklet.addModule('/static/js/pcm-worklet.js');
    const node = new AudioWorkletNode(ac, 'pcm-capture');
    const chunks = [];
    node.port.onmessage = (e) => chunks.push(e.data);
    const mute = ac.createGain();
    mute.gain.value = 0;                       // keeps the node pulled by the graph, silent
    ac.createMediaStreamSource(stream).connect(node);
    node.connect(mute).connect(ac.destination);
    await sleep(seconds * 1000);
    const total = chunks.reduce((n, c) => n + c.length, 0), pcm = new Float32Array(total);
    let o = 0;
    for (const c of chunks) { pcm.set(c, o); o += c.length; }
    return bytesToB64(wavBytes(resample(pcm, ac.sampleRate, 16000), 16000));
  } finally {
    stopStream(stream);
    ac.close();
  }
}
