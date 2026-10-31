// Browser speech recognition (Chrome, Edge, Safari). In Chrome and Edge the audio is sent to
// the browser vendor's cloud service, so it needs internet — stt/local.js is the offline backup.
import { registerEngine } from './registry.js';

const Rec = window.SpeechRecognition || window.webkitSpeechRecognition;

// Give up restarting after this many consecutive early session ends (see rec.onend below).
const MAX_RESTARTS = 5;

registerEngine('webspeech', {
  label: 'Browser speech recognition (needs internet)',
  available: () => !!Rec,
  create({ language, onResult, onState, onError }) {
    let rec = null, wanted = false;
    const open = () => {
      rec = new Rec();
      rec.continuous = true;
      rec.interimResults = true;
      rec.lang = language || 'en-US';
      rec.onstart = () => onState('listening');
      rec.onresult = (e) => {
        for (let i = e.resultIndex; i < e.results.length; i++) {
          const r = e.results[i];
          onResult({ text: r[0].transcript.trim(), final: r.isFinal });
        }
      };
      rec.onerror = (e) => {
        if (e.error === 'no-speech' || e.error === 'aborted') return;
        if (e.error === 'not-allowed') { wanted = false; onError('Microphone access was blocked.'); }
        else if (e.error === 'network') onError('Speech recognition needs an internet connection.');
        else onError(`Speech recognition error: ${e.error}`);
      };
      // The service ends the session after silence; restart while the operator still wants to listen.
      // Reliability fix: back off between restarts and give up after a few consecutive
      // failures. The old tight restart loop hammered the cloud service every time it
      // refused, which is what surfaced as "speech engine offline after a couple of tries".
      rec.onend = () => {
        if (!wanted) { onState('stopped'); return; }
        if (restarts >= MAX_RESTARTS) {
          wanted = false;
          onError('Browser speech engine keeps stopping (it needs internet). '
                + 'Switch to the local offline STT engine.');
          onState('stopped');
          return;
        }
        restarts++;
        clearTimeout(restartTimer);
        restartTimer = setTimeout(() => {
          if (!wanted) return;
          try { open(); rec.start(); } catch (_) { onState('stopped'); }
        }, Math.min(800 * restarts, 4000));
      };
    };
    let restarts = 0, restartTimer = null;
    return {
      start() { wanted = true; restarts = 0; open(); rec.onstart = ((prev) => (...a) => {
        restarts = 0; if (prev) prev.apply(rec, a);
      })(rec.onstart); rec.start(); },
      stop() { wanted = false; clearTimeout(restartTimer); if (rec) rec.stop(); },
    };
  },
});
