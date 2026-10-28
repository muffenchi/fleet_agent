// Register tab (Captain admins): create accounts + enrol biometrics.
import { createEngine, listEngines } from './stt/registry.js';
import { startCamera, stopStream, frameFromVideo, recordWav } from './media.js';

const $ = (id) => document.getElementById(id);
const PROMPTS = ['“The quick brown fox jumps over the lazy dog.”',
  '“Captain, hold position and wait for my next command.”', '“Seven green drones return to launch at dawn.”',
  '“Fleet agent online, all systems ready.”', '“Please confirm the flight plan before takeoff.”'];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));


export function initRegister({ api, state, bus, toast }) {
  let who = '', stream = null, busy = false;
  let editorMode = null;          // 'create' | 'enrol'

  const engines = () => state.status.engines;
  const err = (m) => { $('r-err').textContent = m || ''; };

  // ── helpers ──────────────────────────────────────────────────────────
  function escapeHtml(s) {
    return String(s || '').replace(/[&<>"']/g, (m) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[m]));
  }

  function setWho(name) {
    who = name;
    $('r-who').textContent = name || '—';
    const e = engines();
    $('r-face-go').disabled  = !(name && e.face.available)  || busy;
    $('r-voice-go').disabled = !(name && e.voice.available) || busy;
    $('r-rev-face').disabled = $('r-rev-voice').disabled = !name || busy;
  }

  function renderEngines() {
    const e = engines();
    $('r-face-note').textContent = e.face.available
      ? 'Look straight at the camera, one person only.'
      : `Face recognition is not available: ${e.face.reason}`;
    $('r-voice-note').textContent = e.voice.available
      ? 'Speak each prompt in a normal voice, close to the microphone.'
      : `Voice recognition is not available: ${e.voice.reason}`;
    setWho(who);
  }

  async function ensureCamera() {
    if (stream || !engines().face.available) return;
    try { stream = await startCamera($('r-video'), state.status.settings.camera.device_id); }
    catch (e) { $('r-face-prog').textContent = `Camera: ${e.message}`; }
  }

  // ── users table ──────────────────────────────────────────────────────
  function renderUsersTable(users) {
    const tbody = $('reg-users-tbody');
    if (!tbody) return;
    tbody.replaceChildren();
    if (!users || !users.length) {
      tbody.innerHTML = '<tr><td colspan="5" class="muted" style="text-align: center; padding: 16px;">No users returned.</td></tr>';
      return;
    }
    for (const u of users) {
      const b = u.biometrics || {};
      const tick = (on) => on
        ? '<span style="color: var(--go); font-weight: 700;">✓</span>'
        : '<span class="muted">—</span>';
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td><strong>${escapeHtml(u.username)}</strong></td>
        <td><span class="badge">${escapeHtml(u.role)}</span></td>
        <td style="text-align: center;">${tick(b.voice)}</td>
        <td style="text-align: center;">${tick(b.face)}</td>
        <td style="text-align: right;"></td>`;
      const btn = document.createElement('button');
      btn.className = 'quiet btn-sm';
      btn.textContent = 'Update';
      btn.title = 'Manage biometrics for this user';
      btn.addEventListener('click', () => showEditor('enrol', u.username));
      tr.lastElementChild.appendChild(btn);
      tbody.appendChild(tr);
    }
  }

  async function fetchUsers() {
    const btn = $('reg-fetch-users'), stateEl = $('reg-fetch-state');
    if (btn) btn.disabled = true;
    if (stateEl) stateEl.textContent = 'Fetching…';
    try {
      const users = await api('GET', '/api/register/users');
      renderUsersTable(users);
      if (stateEl) stateEl.textContent = `Loaded ${users.length} user(s)`;
    } catch (e) {
      console.error('[register] fetchUsers failed:', e);
      if (stateEl) stateEl.textContent = e.message || 'fetch failed';
      toast(e.message || 'Failed to load users from Captain', 'bad');
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // ── editor show/hide ─────────────────────────────────────────────────
  function showEditor(mode, username) {
    editorMode = mode;
    const editor = $('reg-editor');
    editor.classList.remove('hidden');
    err('');

    const isCreate = mode === 'create';
    $('reg-editor-title').textContent = isCreate ? 'Create new account' : `Update — ${username}`;

    $('r-user').value     = isCreate ? '' : (username || '');
    $('r-user').disabled  = !isCreate;
    $('r-pass').value     = '';
    $('r-pass').disabled  = !isCreate;
    $('r-role').disabled  = !isCreate;
    $('r-role').value     = 'operator';
    $('r-save').classList.toggle('hidden', !isCreate);

    setWho(isCreate ? '' : username);
    if (!isCreate) ensureCamera();

    editor.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function hideEditor() {
    $('reg-editor').classList.add('hidden');
    editorMode = null;
    setWho('');
    stopStream(stream);
    stream = null;
  }

  // ── enrollment loop ──────────────────────────────────────────────────
  async function enrol(kind, capture, promptFn, progEl) {
    if (busy || !who) return;
    busy = true; setWho(who); err('');
    try {
      const s = await api('POST', '/api/register/start', { username: who, kind });
      let got = 0, tries = 0;
      while (got < s.needed && tries < s.needed * 4) {
        tries++;
        promptFn(got);
        let data;
        try { data = await capture(); } catch (e) { progEl.textContent = e.message; break; }
        try {
          const r = await api('POST', '/api/register/sample', { session_id: s.session_id, data_b64: data });
          got = r.count;
          progEl.textContent = `${got} of ${s.needed} samples`;
        } catch (e) { progEl.textContent = e.message; await sleep(1200); }
      }
      if (got >= s.needed) {
        await api('POST', '/api/register/commit', { session_id: s.session_id });
        progEl.textContent = 'Enrolled.';
        toast(`${kind === 'face' ? 'Face' : 'Voice'} enrolled for ${who}`, 'good');
        fetchUsers();
      } else if (!progEl.textContent.includes('Enrolled')) {
        toast('Enrolment not finished — try again.', 'bad');
      }
    } catch (e) { progEl.textContent = e.message; }
    finally {
      busy = false;
      $('r-voice-prompt').textContent = '—';
      setWho(who);
    }
  }

  // ── wiring ───────────────────────────────────────────────────────────
  $('reg-fetch-users')?.addEventListener('click', fetchUsers);
  $('r-create')?.addEventListener('click', () => showEditor('create'));
  $('reg-editor-close')?.addEventListener('click', hideEditor);

  $('reg-form').addEventListener('submit', async (e) => {
    e.preventDefault(); err('');
    const username = $('r-user').value.trim();
    const password = $('r-pass').value;
    const role     = $('r-role').value;
    if (!username) { err('Username is required'); return; }
    if (!password) { err('Password is required'); return; }
    try {
      const u = await api('POST', '/api/register/user', { username, password, role });
      $('r-pass').value = '';
      toast(`Account “${u.username}” created`, 'good');
      await fetchUsers();
      // Transition to biometric-enrolment mode for the new user
      showEditor('enrol', u.username);
    } catch (e2) { err(e2.message); }
  });

  $('r-dictate').addEventListener('click', () => {
    const name = state.status.settings.voice.stt_engine;
    if (!listEngines().some((x) => x.name === name && x.available)) {
      err('The chosen speech engine is not available here.'); return;
    }
    err(''); $('r-dictate').textContent = 'Listening…';
    const eng = createEngine(name, {
      language: state.status.settings.voice.language,
      onState:  (s) => { if (s === 'stopped') $('r-dictate').textContent = 'Dictate'; },
      onError:  (m) => err(m),
      onResult: ({ text, final }) => {
        if (final) { $('r-user').value = text.toLowerCase().replace(/[^a-z0-9]+/g, ''); eng.stop(); }
      },
    });
    eng.start();
  });

  $('r-face-go').addEventListener('click', async () => {
    await ensureCamera();
    if (!stream) return;
    enrol('face',
      async () => { await sleep(900); return frameFromVideo($('r-video')); },
      () => {},
      $('r-face-prog'));
  });

  $('r-voice-go').addEventListener('click', () => enrol('voice',
    () => recordWav(3.5, state.status.settings.voice.microphone_id),
    (i) => {
      $('r-voice-prompt').textContent = PROMPTS[i % PROMPTS.length];
      $('r-voice-prog').textContent = 'Recording…';
    },
    $('r-voice-prog')));

  for (const kind of ['face', 'voice']) {
    $(`r-rev-${kind}`).addEventListener('click', async () => {
      if (!window.confirm(`Remove ${who}'s ${kind} template? They will no longer be able to use it to unlock voice control.`)) return;
      try {
        await api('POST', '/api/register/revoke', { username: who, kind });
        toast(`${kind} template removed`, 'good');
        fetchUsers();
      } catch (e) { err(e.message); }
    });
  }

  bus.on('status', () => { if (state.status.captain.logged_in) renderEngines(); });

  return {
    onShow() {
      renderEngines();
      fetchUsers();
    },
    onHide() {
      if (editorMode) hideEditor();
      stopStream(stream); stream = null;
    },
  };
}