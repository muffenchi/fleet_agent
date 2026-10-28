// Browser speech recognition (Chrome, Edge, Safari). In Chrome and Edge the audio is sent to
// the browser vendor's cloud service, so it needs internet — plan another engine for field use.
import { registerEngine } from './registry.js';

const Rec = window.SpeechRecognition || window.webkitSpeechRecognition;

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
      rec.onend = () => { if (wanted) { try { open(); rec.start(); } catch (_) { onState('stopped'); } } else onState('stopped'); };
    };
    return {
      start() { wanted = true; open(); rec.start(); },
      stop() { wanted = false; if (rec) rec.stop(); },
    };
  },
});
