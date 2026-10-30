// Voice tab: unlock gate, per-command phrase assignment, live conversation.
import { listEngines, createEngine } from './stt/registry.js';
import './stt/webspeech.js';
import { recordWav, captureFace } from './media.js';
import { VOICE_CATALOG, GROUP_ORDER } from './voice_catalog.js';

const $ = (id) => document.getElementById(id);
const LAMP = { executed: 'go', needs_confirm: 'warn', locked: 'warn',
               rejected: 'stop', cancelled: 'off', expired: 'off', ignored: 'off' };
const RECORD_SECONDS = 4;

export function initVoice({ api, state, bus, toast }) {
  let sample = { voice: null, face: null };
  let gateUntil = 0, engine = null, listening = false, confirmUntil = 0;

  // Recorder state
  let recorderAction    = null;
  let recorderPhrase    = null;    // STT result (final only)
  let recorderWavB64    = null;
  let recorderFaceB64   = null;
  let recorderAbort     = null;
  let sttEngineForRecord = null;

  const settings = () => state.status.settings.voice;
  const gate     = () => state.status.voice.gate;
  const captainHost = () => {
    try { return new URL(state.status.captain.url).host; } catch { return '<captain>'; }
  };

  // ── voice command table (catalog-driven) ─────────────────────────────
  let localCommands = {};          // action -> { id, phrase }

  function escapeHtml(s) {
    return String(s || '').replace(/[&<>"']/g, (m) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[m]));
  }

  async function loadLocalCommands() {
    try {
      const r = await api('GET', '/api/voice/commands');
      localCommands = {};
      for (const c of (r.commands || [])) localCommands[c.action] = c;
      renderTable();
    } catch (e) {
      if (state.status?.captain?.logged_in) {
        toast(`Could not load voice commands: ${e.message}`, 'bad');
      }
    }
  }

  function routeForEntry(entry) {
    const host = captainHost();
    switch (entry.exec?.mode) {
      case 'action':    return `POST ${host}/command`;
      case 'mavlink':   return `POST ${host}/command (custom)`;
      case 'broadcast': return `POST ${host}/command × N`;
      case 'fleet':     return `POST ${host}/agent (fleet)`;
      case 'nav':       return `POST ${host}/agent (nav)`;
      default:          return `POST ${host}/command`;
    }
  }

  function voiceAssignable(entry) {
    // Only ACTIONS-backed entries can be triggered by the voice engine today.
    // Everything else (mavlink, broadcast, fleet, nav) is executed manually
    // from the Commands tab.
    return entry.exec?.mode === 'action';
  }

  function renderTable() {
    const tbody = $('vc-tbody');
    if (!tbody) return;
    tbody.replaceChildren();

    const catalogNames = new Set(VOICE_CATALOG.map(e => e.name));
    const orphanActions = Object.keys(localCommands).filter(a => !catalogNames.has(a));

    let any = false;

    for (const [key, title] of GROUP_ORDER) {
      const inGroup = VOICE_CATALOG.filter(e => e.group === key);
      if (!inGroup.length) continue;
      any = true;

      const header = document.createElement('tr');
      header.className = 'cmd-group-row';
      header.innerHTML = `<td colspan="5"><span class="cmd-group-label">${escapeHtml(title)}</span></td>`;
      tbody.appendChild(header);

      for (const entry of inGroup) tbody.appendChild(renderCatalogRow(entry));
    }

    if (orphanActions.length) {
      any = true;
      const header = document.createElement('tr');
      header.className = 'cmd-group-row';
      header.innerHTML = `<td colspan="5"><span class="cmd-group-label">Other (registered, not in catalog)</span></td>`;
      tbody.appendChild(header);
      for (const action of orphanActions) {
        tbody.appendChild(renderOrphanRow(action, localCommands[action]));
      }
    }

    if (!any) {
      tbody.innerHTML = '<tr><td colspan="5" class="muted" style="text-align:center;padding:16px;">No voice commands available.</td></tr>';
    }
  }

  function renderCatalogRow(entry) {
    const local  = localCommands[entry.name];
    const phrase = local?.phrase || '';
    const route  = routeForEntry(entry);
    const canBind = voiceAssignable(entry);

    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><strong>${escapeHtml(entry.label || entry.name)}</strong>
          <code class="muted small">(${escapeHtml(entry.name)})</code></td>
      <td>${phrase
            ? `<span class="vc-phrase">"${escapeHtml(phrase)}"</span>`
            : '<span class="muted">— not assigned —</span>'}</td>
      <td><code class="muted small">${escapeHtml(route)}</code></td>
      <td style="text-align:center;">${entry.confirm
            ? '<span class="badge warn">Confirm</span>'
            : '<span class="muted small">Immediate</span>'}</td>
      <td style="text-align:right;"></td>`;

    const td = tr.lastElementChild;
    if (!canBind) {
      td.innerHTML = '<span class="muted small" title="Only ACTIONS-backed commands can be voice-bound today">not voice-assignable</span>';
      return tr;
    }

    const setBtn = document.createElement('button');
    setBtn.className = 'quiet btn-sm';
    setBtn.textContent = phrase ? 'Change' : 'Set';
    setBtn.addEventListener('click', () => startRecorder(entry.name));
    td.appendChild(setBtn);

    if (phrase) {
      const clearBtn = document.createElement('button');
      clearBtn.className = 'quiet btn-sm';
      clearBtn.textContent = '×';
      clearBtn.title = 'Clear phrase';
      clearBtn.style.marginLeft = '6px';
      clearBtn.addEventListener('click', () => clearPhrase(entry.name));
      td.appendChild(clearBtn);
    }
    return tr;
  }

  function renderOrphanRow(action, local) {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><strong>${escapeHtml(action)}</strong>
          <code class="muted small">(orphan)</code></td>
      <td><span class="vc-phrase">"${escapeHtml(local.phrase)}"</span></td>
      <td><code class="muted small">POST ${escapeHtml(captainHost())}/command</code></td>
      <td><span class="muted small">—</span></td>
      <td style="text-align:right;"></td>`;
    const clearBtn = document.createElement('button');
    clearBtn.className = 'quiet btn-sm';
    clearBtn.textContent = '×';
    clearBtn.title = 'Clear phrase';
    clearBtn.addEventListener('click', () => clearPhrase(action));
    tr.lastElementChild.appendChild(clearBtn);
    return tr;
  }

  async function clearPhrase(action) {
    const local = localCommands[action];
    if (!local) return;
    if (!window.confirm(`Clear phrase "${local.phrase}" for "${action}"?`)) return;
    try {
      await api('DELETE', `/api/voice/commands/${encodeURIComponent(local.id)}`);
      toast('Phrase removed', 'good');
      await loadLocalCommands();
    } catch (e) { toast(e.message, 'bad'); }
  }

  // ── indicator ────────────────────────────────────────────────────────
  function setIndicator(state_, title) {
    const el = $('vc-indicator');
    if (!el) return;
    el.className = 'vc-indicator' + (state_ ? ' ' + state_ : '');
    el.title = title || (state_ === 'good' ? 'Verified'
                       : state_ === 'bad'  ? 'Rejected'
                       : state_ === 'busy' ? 'Recording…'
                       : 'Not verified');
  }

  // ── recorder ────────────────────────────────────────────────────────
  function startRecorder(action) {
    recorderAction  = action;
    recorderPhrase  = null;
    recorderWavB64  = null;
    recorderFaceB64 = null;
    $('vc-target-action').textContent = action;
    $('vc-record').textContent = 'Start';
    $('vc-record').disabled = false;
    $('vc-phrase-input').value = '';
    $('vc-save').classList.add('hidden');
    $('vc-record-state').textContent = '';
    $('vc-err').textContent = '';
    $('vc-progress-wrap').classList.add('hidden');
    $('vc-progress-fill').style.width = '0';
    $('vc-progress-fill').classList.remove('done');
    $('vc-progress-label').textContent = `${RECORD_SECONDS.toFixed(1)} s`;
    setIndicator(null);
    $('vc-recorder').classList.remove('hidden');
    $('vc-recorder').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function endRecorder() {
    if (recorderAbort) { try { recorderAbort.abort(); } catch {} recorderAbort = null; }
    if (sttEngineForRecord) { try { sttEngineForRecord.stop(); } catch {} sttEngineForRecord = null; }
    recorderAction = recorderPhrase = recorderWavB64 = recorderFaceB64 = null;
    $('vc-recorder').classList.add('hidden');
  }

  async function doRecord() {
    $('vc-record').disabled = true;
    $('vc-record').textContent = 'Stop';
    $('vc-phrase-input').value = '';
    $('vc-save').classList.add('hidden');
    $('vc-err').textContent = '';
    $('vc-record-state').textContent = 'Recording… speak the phrase clearly.';
    setIndicator('busy');
    $('vc-progress-wrap').classList.remove('hidden');
    $('vc-progress-fill').style.width = '0';
    $('vc-progress-fill').classList.remove('done');
    recorderPhrase = null;

    // STT in parallel — result populates vc-phrase-input (readonly).
    const sttName = state.status.settings.voice.stt_engine;
    if (listEngines().some(e => e.name === sttName && e.available)) {
      sttEngineForRecord = createEngine(sttName, {
        language: settings().language,
        onState: () => {},
        onError: (m) => { $('vc-record-state').textContent = `STT: ${m}`; },
        onResult: ({ text, final }) => {
          if (final && text) {
            recorderPhrase = text.trim();
            $('vc-phrase-input').value = recorderPhrase;
          }
        },
      });
      try { sttEngineForRecord.start(); } catch { sttEngineForRecord = null; }
    }

    recorderAbort = new AbortController();
    try {
      recorderWavB64 = await recordWav(RECORD_SECONDS, settings().microphone_id, {
        signal: recorderAbort.signal,
        onProgress: (frac, left) => {
          $('vc-progress-fill').style.width = `${(frac * 100).toFixed(1)}%`;
          $('vc-progress-label').textContent = `${left.toFixed(1)} s`;
        },
      });
    } catch (e) {
      $('vc-err').textContent = `Recording failed: ${e.message}`;
      $('vc-record').disabled = false;
      $('vc-record').textContent = 'Start';
      setIndicator('bad');
      return;
    } finally {
      if (sttEngineForRecord) { try { sttEngineForRecord.stop(); } catch {} sttEngineForRecord = null; }
      recorderAbort = null;
      $('vc-progress-fill').style.width = '100%';
      $('vc-progress-fill').classList.add('done');
      $('vc-progress-label').textContent = '0.0 s';
      setTimeout(() => $('vc-progress-wrap').classList.add('hidden'), 1200);
    }

    // STT is the only source of truth for the phrase now.
    $('vc-record').disabled = false;
    $('vc-record').textContent = 'Start';       // back to Start; a second click re-records

    if (!recorderPhrase) {
      $('vc-record-state').textContent = 'No speech recognised — please re-record.';
      setIndicator('bad', 'STT returned no text');
      return;
    }
    $('vc-phrase-input').value = recorderPhrase;
    $('vc-record-state').textContent = 'Captured. Checking against your voiceprint…';
    await precheckVoice();
  }

  // Ask the server: does this recording match my enrolled voiceprint?
  // Server-side: /api/voice/precheck (no storage, no add_or_update).
  async function precheckVoice() {
    if (!recorderWavB64) { setIndicator(null); return; }
    setIndicator('busy', 'Checking voice…');
    try {
      const r = await api('POST', '/api/voice/precheck', {
        data_b64: recorderWavB64,
        face_jpeg_b64: recorderFaceB64,
      });
      if (r.ok) {
        const parts = [];
        if (r.voice_score != null) parts.push(`voice ${r.voice_score}`);
        if (r.face_score != null) parts.push(`face ${r.face_score}`);
        setIndicator('good', `Matched (${parts.join(' / ')})`);
        $('vc-save').classList.remove('hidden');
        $('vc-save').disabled = false;
        $('vc-record-state').textContent = 'Verified. Press Save to store this phrase.';
      } else {
        setIndicator('bad', `Not matched — ${r.voice_message || 'voice too low'}`);
        $('vc-save').classList.remove('hidden');
        $('vc-save').disabled = true;
        $('vc-record-state').textContent = `Not matched. Voice score ${r.voice_score ?? '—'} `
                                         + `(needs ≥ ${r.threshold ?? '—'}). Re-record, or capture a face first.`;
      }
    } catch (e) {
      if (e.status === 404) {
        // Endpoint not deployed — degrade gracefully; verification happens on Save.
        setIndicator(null, 'Verification will run on Save');
        $('vc-save').classList.remove('hidden');
        $('vc-save').disabled = false;
        $('vc-record-state').textContent = 'Recorded. Press Save to verify and store.';
      } else {
        setIndicator('bad', e.message);
        $('vc-save').classList.remove('hidden');
        $('vc-save').disabled = true;
        $('vc-record-state').textContent = e.message;
      }
    }
  }

  $('vc-face-capture')?.addEventListener('click', async () => {
    $('vc-face-state').textContent = 'Capturing…';
    try {
      recorderFaceB64 = await captureFace(state.status.settings.camera.device_id);
      $('vc-face-state').textContent = 'Face captured.';
      // If voice already recorded, re-run precheck with face assist
      if (recorderWavB64) await precheckVoice();
    } catch (e) {
      recorderFaceB64 = null;
      $('vc-face-state').textContent = e.message;
    }
  });

  $('vc-cancel')?.addEventListener('click', endRecorder);

  $('vc-record')?.addEventListener('click', async () => {
    if (!recorderAction) return;
    if (recorderAbort) { recorderAbort.abort(); return; }
    await doRecord();
  });

  $('vc-save')?.addEventListener('click', async () => {
    if (!recorderAction || !recorderWavB64) return;
    const phrase = ($('vc-phrase-input').value || recorderPhrase || '').trim();
    if (!phrase) { $('vc-err').textContent = 'Phrase is empty — re-record.'; return; }
    $('vc-save').disabled = true;
    $('vc-err').textContent = '';
    try {
      await api('POST', '/api/voice/assign', {
        action: recorderAction,
        phrase,
        data_b64: recorderWavB64,
        face_jpeg_b64: recorderFaceB64,
      });
      toast(`"${phrase}" → ${recorderAction} saved`, 'good');
      endRecorder();
      await loadLocalCommands();
    } catch (e) {
      setIndicator('bad', e.message);
      $('vc-err').textContent = e.message;
    } finally {
      $('vc-save').disabled = false;
    }
  });

  // ── gate card ────────────────────────────────────────────────────────
  function renderGate() {
    if (!state.status || !state.status.captain.logged_in) return;
    const g = gate(), factors = settings().unlock_factors;
    $('gate-card').dataset.open = String(g.unlocked);
    $('gate-title').textContent = g.unlocked ? 'Voice control is unlocked' : 'Voice control is locked';
    gateUntil = g.unlocked ? Date.now() + g.remaining_s * 1000 : 0;
    tick();
    $('unlock-form').classList.toggle('hidden', g.unlocked);
    $('lock-btn').classList.toggle('hidden', !g.unlocked);
    $('uf-password-row').classList.toggle('hidden', !factors.includes('password'));
    $('uf-voice-row').classList.toggle('hidden', !factors.includes('voiceprint'));
    $('uf-face-row').classList.toggle('hidden', !factors.includes('face'));
  }

  function tick() {
    const el = $('gate-sub');
    if (!gateUntil) { el.textContent = 'Nothing you say will reach a drone until you unlock it.'; return; }
    const left = Math.max(0, Math.round((gateUntil - Date.now()) / 1000));
    el.textContent = `Unlocked with ${gate().unlocked_by.join(' + ') || 'password'}. `
                   + `Locks again after ${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')} without a command.`;
    if (confirmUntil && Date.now() > confirmUntil) hideConfirm();
  }
  setInterval(tick, 1000);

  function maybeAutoSubmit() {
    const factors = settings().unlock_factors || [];
    if (factors.includes('password')) return;
    const needVoice = factors.includes('voiceprint');
    const needFace  = factors.includes('face');
    if ((!needVoice || sample.voice) && (!needFace || sample.face)) {
      $('unlock-form').dispatchEvent(new Event('submit', { cancelable: true }));
    }
  }

  $('uf-voice-btn').addEventListener('click', async () => {
    $('uf-voice-state').textContent = 'Recording…';
    try {
      sample.voice = await recordWav(3, settings().microphone_id);
      $('uf-voice-state').textContent = 'Recorded.';
      maybeAutoSubmit();
    } catch (e) { sample.voice = null; $('uf-voice-state').textContent = e.message; }
  });

  $('uf-face-btn').addEventListener('click', async () => {
    $('uf-face-state').textContent = 'Capturing…';
    try {
      sample.face = await captureFace(state.status.settings.camera.device_id);
      $('uf-face-state').textContent = 'Captured.';
      maybeAutoSubmit();
    } catch (e) { sample.face = null; $('uf-face-state').textContent = e.message; }
  });

  $('unlock-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    $('unlock-err').textContent = '';
    try {
      const r = await api('POST', '/api/voice/unlock', {
        password:      $('uf-password').value || null,
        voice_wav_b64: sample.voice,
        face_jpeg_b64: sample.face,
      });
      if (!r.unlocked) {
        $('unlock-err').textContent = Object.entries(r.factors).filter(([, v]) => !v.ok)
          .map(([k, v]) => `${k}: ${v.message}`).join(' · ');
      }
    } catch (err) { $('unlock-err').textContent = err.message; }
    finally {
      sample = { voice: null, face: null };
      $('uf-password').value = '';
      $('uf-voice-state').textContent = 'Speak normally for 3 seconds.';
      $('uf-face-state').textContent = 'Look at the camera.';
    }
  });

  $('lock-btn').addEventListener('click',
    () => api('POST', '/api/voice/lock').catch(e => toast(e.message, 'bad')));

  // ── listening ────────────────────────────────────────────────────────
  function fillEngines() {
    const sel = $('stt-engine'), want = state.status.settings.voice.stt_engine;
    if (!sel.options.length) listEngines().forEach(e =>
      sel.add(new Option(e.label + (e.available ? '' : ' — not available here'), e.name)));
    if ([...sel.options].some(o => o.value === want)) sel.value = want;
  }

  $('listen-btn').addEventListener('click', () => {
    if (listening) { engine.stop(); return; }
    try {
      engine = createEngine($('stt-engine').value, {
        language: settings().language,
        onState: s => { listening = s === 'listening';
                        $('listen-btn').textContent = listening ? 'Stop listening' : 'Start listening'; },
        onError: m => { $('listen-note').textContent = m; },
        onResult: ({ text, final }) => {
          $('listen-note').textContent = final ? '' : `Hearing: ${text}`;
          if (final && text) api('POST', '/api/voice/transcript', { text, final: true }).catch(e => toast(e.message, 'bad'));
        },
      });
      $('listen-note').textContent = ''; engine.start();
    } catch (e) { $('listen-note').textContent = e.message; }
  });

  // ── conversational prompt bar ────────────────────────────────────────
  function showPrompt(ev) {
    $('vc-prompt-text').textContent = ev.detail || `Say confirm to ${ev.action}`;
    $('vc-prompt-bar').classList.remove('hidden');
    confirmUntil = Date.now() + (settings().confirm_window_s || 8) * 1000;
  }
  function hideConfirm() {
    confirmUntil = 0;
    $('confirm-bar').classList.add('hidden');
    $('vc-prompt-bar').classList.add('hidden');
  }

  bus.on('voice.heard', (ev) => {
    const li = document.createElement('li');
    li.className = ev.status;
    const lamp = document.createElement('i'); lamp.className = `lamp ${LAMP[ev.status] || 'off'}`;
    const body = document.createElement('div');
    body.textContent = `"${ev.text}"`;
    const sub = document.createElement('span'); sub.className = 'sub';
    sub.textContent = ev.status === 'ignored' ? 'not a command'
                    : `${ev.status.replace('_', ' ')}${ev.detail ? ' — ' + ev.detail : ''}`;
    body.appendChild(sub); li.append(lamp, body);
    const log = $('heard'); log.prepend(li);
    while (log.children.length > 40) log.lastChild.remove();
    if (ev.status === 'needs_confirm') showPrompt(ev);
    else if (ev.status !== 'ignored') hideConfirm();
  });

  $('vc-prompt-confirm').addEventListener('click',
    () => api('POST', '/api/voice/confirm', { ok: true }).catch(e => toast(e.message, 'bad')));
  $('vc-prompt-cancel').addEventListener('click',
    () => api('POST', '/api/voice/confirm', { ok: false }).catch(e => toast(e.message, 'bad')));
  $('vc-prompt-hold').addEventListener('click',
    () => api('POST', '/api/voice/confirm', { ok: false, then_action: 'hold' }).catch(e => toast(e.message, 'bad')));
  $('confirm-yes').addEventListener('click',
    () => api('POST', '/api/voice/confirm', { ok: true }).catch(e => toast(e.message, 'bad')));
  $('confirm-no').addEventListener('click',
    () => api('POST', '/api/voice/confirm', { ok: false }).catch(e => toast(e.message, 'bad')));

  bus.on('status', () => {
    if (!state.status.captain.logged_in) return;
    renderGate();
    fillEngines();
    renderTable();
    loadLocalCommands();
  });
  bus.on('voice.commands.changed', () => loadLocalCommands());
  bus.on('voice.gate.toggle', (ev) => {
    if (ev.changed) {
      toast(ev.reason === 'locked by button'
            ? 'Voice control locked'
            : 'Voice control re-enabled', 'good');
    } else {
      toast(ev.reason || 'Voice control is locked — re-unlock via password / face first', 'bad');
    }
  });



  
  return {
    onShow() {
      if (!state.status?.captain?.logged_in) return;
      renderGate();
      fillEngines();
      renderTable();
      loadLocalCommands();
    },
  };
}