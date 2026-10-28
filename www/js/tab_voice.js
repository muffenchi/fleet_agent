// Voice tab: gate, listening, spoken-command log, plus registered phrase table.
import { listEngines, createEngine } from './stt/registry.js';
import './stt/webspeech.js';
import { recordWav, captureFace } from './media.js';

const $ = (id) => document.getElementById(id);
const LAMP = { executed: 'go', needs_confirm: 'warn', locked: 'warn',
               rejected: 'stop', cancelled: 'off', expired: 'off', ignored: 'off' };


export function initVoice({ api, state, bus, toast }) {
  let sample = { voice: null, face: null };
  let gateUntil = 0, engine = null, listening = false, confirmUntil = 0;

  const settings = () => state.status.settings.voice;
  const gate     = () => state.status.voice.gate;

  // ── Voice commands table ─────────────────────────────────────────────
  let vcSample = null;
  let vcEditingId = null;           // null → create mode; string → edit mode

  function endpointLabel() {
    const url = state.status?.captain?.url || '';
    if (!url) return 'POST /command';
    try {
      const u = new URL(url);
      return `POST ${u.host}/command`;
    } catch { return 'POST /command'; }
  }

  function fillVcActions() {
    const sel = $('vc-action');
    if (!sel) return;
    sel.replaceChildren();
    Object.entries(state.status.actions || {}).forEach(([name, action]) => {
      sel.appendChild(new Option(action.label + (action.confirm ? ' (confirm)' : ''), name));
    });
  }

  function renderVcTable(commands) {
    const tbody = $('vc-tbody');
    if (!tbody) return;
    tbody.replaceChildren();
    if (!commands || !commands.length) {
      tbody.innerHTML = '<tr><td colspan="4" class="muted" style="text-align: center; padding: 16px;">No voice commands registered yet.</td></tr>';
      return;
    }
    const route = endpointLabel();
    const captainUrl = state.status?.captain?.url || '';
    for (const command of commands) {
      const tr = document.createElement('tr');

      const tdPhrase = document.createElement('td');
      tdPhrase.innerHTML = `<span class="vc-phrase">“${escapeHtml(command.phrase)}”</span>`;

      const tdAction = document.createElement('td');
      tdAction.innerHTML = `<code>${escapeHtml(command.action)}</code>`;

      const tdRoute = document.createElement('td');
      tdRoute.innerHTML = `<code title="${escapeHtml(captainUrl)}/command">${escapeHtml(route)}</code>`;

      const tdActions = document.createElement('td');
      tdActions.style.textAlign = 'right';
      const wrap = document.createElement('div');
      wrap.style.display = 'inline-flex'; wrap.style.gap = '6px';
      const edit = document.createElement('button');
      edit.className = 'quiet btn-sm';
      edit.textContent = 'Edit';
      edit.addEventListener('click', () => showEditor(command));
      const del = document.createElement('button');
      del.className = 'quiet btn-sm';
      del.textContent = '×';
      del.title = 'Remove';
      del.addEventListener('click', () => deleteVc(command.id));
      wrap.append(edit, del);
      tdActions.appendChild(wrap);

      tr.append(tdPhrase, tdAction, tdRoute, tdActions);
      tbody.appendChild(tr);
    }
  }

  function escapeHtml(s) {
    return String(s || '').replace(/[&<>"']/g, (m) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[m]));
  }

  async function loadVcList() {
    try {
      const r = await api('GET', '/api/voice/commands');
      renderVcTable(r.commands || []);
      $('vc-err').textContent = '';
    } catch (e) {
      if (state.status?.captain?.logged_in) $('vc-err').textContent = e.message;
    }
  }

  async function deleteVc(id) {
    if (!window.confirm('Remove this voice command?')) return;
    try {
      await api('DELETE', `/api/voice/commands/${encodeURIComponent(id)}`);
      await loadVcList();
      toast('Voice command removed', 'good');
    } catch (e) { toast(e.message, 'bad'); }
  }

  // ── Create / edit editor ─────────────────────────────────────────────
  function showEditor(command) {
    const editor = $('vc-editor');
    editor.classList.remove('hidden');
    $('vc-err').textContent = '';
    vcSample = null;
    $('vc-record-state').textContent = 'Optional — verifies the phrase against your enrolled voiceprint.';

    if (command) {
      vcEditingId = command.id;
      $('vc-editor-title').textContent = 'Edit voice command';
      $('vc-phrase').value = command.phrase || '';
      if ([...$('vc-action').options].some((o) => o.value === command.action)) {
        $('vc-action').value = command.action;
      }
    } else {
      vcEditingId = null;
      $('vc-editor-title').textContent = 'New voice command';
      $('vc-phrase').value = '';
    }

    editor.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function hideEditor() {
    $('vc-editor').classList.add('hidden');
    vcEditingId = null;
    vcSample = null;
    $('vc-phrase').value = '';
    $('vc-err').textContent = '';
  }

  $('vc-create')?.addEventListener('click', () => showEditor(null));
  $('vc-cancel')?.addEventListener('click', hideEditor);

  $('vc-record')?.addEventListener('click', async () => {
    $('vc-record-state').textContent = 'Recording…';
    try {
      vcSample = await recordWav(2.5, settings().microphone_id);
      $('vc-record-state').textContent = 'Recorded.';
    } catch (e) {
      vcSample = null;
      $('vc-record-state').textContent = e.message;
    }
  });

  $('vc-save')?.addEventListener('click', async () => {
    $('vc-err').textContent = '';
    const phrase = ($('vc-phrase').value || '').trim();
    const action = $('vc-action').value;
    if (!phrase) { $('vc-err').textContent = 'Phrase is required'; return; }
    if (!action) { $('vc-err').textContent = 'Command is required'; return; }
    try {
      if (vcEditingId) {
        await api('PUT', `/api/voice/commands/${encodeURIComponent(vcEditingId)}`, {
          phrase, action, voice_wav_b64: vcSample || null,
        });
        toast(`“${phrase}” updated`, 'good');
      } else {
        await api('POST', '/api/voice/commands', {
          phrase, action, voice_wav_b64: vcSample || null,
        });
        toast(`“${phrase}” → ${action} registered`, 'good');
      }
      hideEditor();
      await loadVcList();
    } catch (e) { $('vc-err').textContent = e.message; }
  });

  $('vc-dictate')?.addEventListener('click', () => {
    const name = state.status.settings.voice.stt_engine;
    if (!listEngines().some((x) => x.name === name && x.available)) {
      $('vc-err').textContent = 'Speech engine not available';
      return;
    }
    $('vc-dictate').textContent = 'Listening…';
    const eng = createEngine(name, {
      language: settings().language,
      onState:  (s) => { if (s === 'stopped') $('vc-dictate').textContent = 'Dictate'; },
      onError:  (m) => { $('vc-err').textContent = m; },
      onResult: ({ text, final }) => { if (final) { $('vc-phrase').value = text.trim(); eng.stop(); } },
    });
    eng.start();
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
    el.textContent = `Unlocked with ${gate().unlocked_by.join(' + ') || 'password'}. Locks again after ${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')} without a command.`;
    if (confirmUntil && Date.now() > confirmUntil) hideConfirm();
  }
  setInterval(tick, 1000);

  // Auto-submit unlock once the required samples are present
  function maybeAutoSubmit() {
    const factors = settings().unlock_factors || [];
    if (factors.includes('password')) return;         // password waits for Enter
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
    () => api('POST', '/api/voice/lock').catch((e) => toast(e.message, 'bad')));

  // ── listening ────────────────────────────────────────────────────────
  function fillEngines() {
    const sel = $('stt-engine'), want = state.status.settings.voice.stt_engine;
    if (!sel.options.length) listEngines().forEach((e) =>
      sel.add(new Option(e.label + (e.available ? '' : ' — not available here'), e.name)));
    if ([...sel.options].some((o) => o.value === want)) sel.value = want;
  }

  $('listen-btn').addEventListener('click', () => {
    if (listening) { engine.stop(); return; }
    try {
      engine = createEngine($('stt-engine').value, {
        language: settings().language,
        onState:  (s) => { listening = s === 'listening'; $('listen-btn').textContent = listening ? 'Stop listening' : 'Start listening'; },
        onError:  (m) => { $('listen-note').textContent = m; },
        onResult: ({ text, final }) => {
          $('listen-note').textContent = final ? '' : `Hearing: ${text}`;
          if (final && text) api('POST', '/api/voice/transcript', { text, final: true }).catch((e) => toast(e.message, 'bad'));
        },
      });
      $('listen-note').textContent = ''; engine.start();
    } catch (e) { $('listen-note').textContent = e.message; }
  });

  // ── spoken-command log ───────────────────────────────────────────────
  function hideConfirm() { confirmUntil = 0; $('confirm-bar').classList.add('hidden'); }
  bus.on('voice.heard', (ev) => {
    const li = document.createElement('li');
    li.className = ev.status;
    const lamp = document.createElement('i'); lamp.className = `lamp ${LAMP[ev.status] || 'off'}`;
    const body = document.createElement('div');
    body.textContent = `“${ev.text}”`;
    const sub = document.createElement('span'); sub.className = 'sub';
    sub.textContent = ev.status === 'ignored' ? 'not a command'
                    : `${ev.status.replace('_', ' ')}${ev.detail ? ' — ' + ev.detail : ''}`;
    body.appendChild(sub); li.append(lamp, body);
    const log = $('heard'); log.prepend(li);
    while (log.children.length > 40) log.lastChild.remove();
    if (ev.status === 'needs_confirm') {
      $('confirm-text').textContent = ev.detail;
      $('confirm-bar').classList.remove('hidden');
      confirmUntil = Date.now() + settings().confirm_window_s * 1000;
    } else if (ev.status !== 'ignored') hideConfirm();
  });
  $('confirm-yes').addEventListener('click', () => api('POST', '/api/voice/confirm', { ok: true }).catch((e) => toast(e.message, 'bad')));
  $('confirm-no').addEventListener('click',  () => api('POST', '/api/voice/confirm', { ok: false }).catch((e) => toast(e.message, 'bad')));

  // ── event plumbing ───────────────────────────────────────────────────
  bus.on('status', () => {
    if (!state.status.captain.logged_in) return;
    fillVcActions();
    loadVcList();
    renderGate();
    fillEngines();
  });
  bus.on('voice.commands.changed', (event) => renderVcTable(event.commands));

  return {
    onShow() {
      if (!state.status?.captain?.logged_in) return;
      renderGate();
      fillEngines();
      fillVcActions();
      loadVcList();
    },
  };
}