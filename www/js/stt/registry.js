// Speech-to-text engines plug in here. To add one (for example a local Vosk or Whisper
// engine), create stt/<name>.js and call registerEngine() — then import it in tab_voice.js.
//
// An engine factory is create(options) → { start(), stop() } and reports through the
// callbacks it is given:
//   options.language              BCP-47 tag such as "en-US"
//   options.onResult({text, final})   interim and final transcripts
//   options.onState(state)        "listening" | "stopped"
//   options.onError(message)      human-readable problem
// Only final transcripts are sent on to the agent; the agent decides what they mean.
const engines = new Map();

export function registerEngine(name, def) {
  engines.set(name, def);   // def: { label, available(), create(options) }
}

export function listEngines() {
  return [...engines].map(([name, d]) => ({ name, label: d.label, available: !!d.available() }));
}

export function createEngine(name, options) {
  const d = engines.get(name);
  if (!d) throw new Error(`Unknown speech engine “${name}”`);
  if (!d.available()) throw new Error(`${d.label} is not available in this browser`);
  return d.create(options);
}
