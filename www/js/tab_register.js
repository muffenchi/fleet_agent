// Register tab (Captain admins): manage accounts and their biometrics.

import { startCamera, stopStream, frameFromVideo, previewFrameFromVideo, recordWav } from './media.js';


const $ = (id) => document.getElementById(id);
const PROMPTS = [
  '“The quick brown fox jumps over the lazy dog.”',
  '“Captain, hold position and wait for my next command.”',
  '“Seven green drones return to launch at dawn.”',
  '“Fleet agent online, all systems ready.”',
  '“Please confirm the flight plan before takeoff.”',
];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));


export function initRegister({ api, state, bus, toast }) {
  let mode = null;              // null | 'create' | 'edit'
  let targetUser = null;        // user object when in 'edit'
  let previewTimer = null;
  let lastUsers = [];
  let faceStream = null;
  let busy = false;

  const engines     = () => state.status.engines;
  const isAdmin     = () => state.status?.captain?.user?.role === 'admin';
  const currentUser = () => state.status?.captain?.user?.username || null;

  // ── helpers ──────────────────────────────────────────────────────
  const escapeHtml = (s) => String(s || '').replace(/[&<>"']/g, (m) => ({    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',  }[m]));

  async function ensureFaceCamera() {
    if (faceStream || !engines().face.available) return;
    try {
      faceStream = await startCamera($('r-video'), state.status.settings.camera.device_id);
    } catch (e) {
      $('r-face-prog').textContent = `Camera: ${e.message}`;
    }
  }

  // ── users table ──────────────────────────────────────────────────
  function renderUsersTable(users) {
    const tbody = $('reg-users-tbody');
    if (!tbody) return;
    lastUsers = users || [];
    tbody.replaceChildren();
    if (!users.length) {
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
      btn.addEventListener('click', () => openEditor('edit', u));
      tr.lastElementChild.appendChild(btn);
      tbody.appendChild(tr);
    }
  }


  

  function stopFacePreview() {
    if (previewTimer) { clearTimeout(previewTimer); previewTimer = null; }
    const el = $('face-guide');
    if (el) el.classList.remove('ready');
    const hint = $('face-guide-hint');
    if (hint) hint.textContent = 'Align your face in the oval';
    const btn = $('r-face-go');
    if (btn) btn.disabled = true;
  }

  function applyFaceGuide(r) {
    const el = $('face-guide'), hint = $('face-guide-hint'), btn = $('r-face-go');
    if (!el || !hint || !btn) return;
    if (!r || !r.ok) {
      el.classList.remove('ready');
      btn.disabled = true;
      if      (r?.reason === 'multiple_faces')  hint.textContent = 'Only one person in frame';
      else if (r?.reason === 'face_too_small')  hint.textContent = 'Move closer to the camera';
      else if (r?.reason === 'no_face')         hint.textContent = 'No face detected';
      else                                       hint.textContent = 'Align your face in the oval';
      return;
    }
    el.classList.add('ready');
    hint.textContent = 'Hold still — press Capture';
    btn.disabled = busy || !engines().face.available;
  }

  async function startFacePreview() {
    stopFacePreview();
    if (!engines().face.available) return;
    await ensureFaceCamera();
    if (!faceStream) return;

    const tick = async () => {
      if (!faceStream || !mode /* closed */) return;
      try {
        const data = await previewFrameFromVideo($('r-video'), 480);
        const r = await api('POST', '/api/register/preview', { data_b64: data });
        applyFaceGuide(r);
      } catch (e) {
        // Engine unavailable or 409 — stop the loop; the note already explains why.
        return;
      }
      previewTimer = setTimeout(tick, 140);   // ~7 fps
    };
    tick();
  }


  async function fetchUsers() {
    const btn = $('reg-fetch-users'), st = $('reg-fetch-state');
    if (btn) btn.disabled = true;
    if (st) st.textContent = 'Fetching…';
    try {
      const users = await api('GET', '/api/register/users');
      renderUsersTable(users);
      if (st) st.textContent = `Loaded ${users.length} user(s)`;
    } catch (e) {
      console.error('[register] fetch failed:', e);
      if (st) st.textContent = e.message || 'fetch failed';
      toast(e.message || 'Failed to load users', 'bad');
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // ── editor ───────────────────────────────────────────────────────
  function refreshEditorBioStatus() {
    if (!targetUser) return;
    const bio = targetUser.biometrics || {};
    $('r-face-badge').textContent = bio.face ? '✓ enrolled' : '— not enrolled';
    $('r-face-badge').className   = 'badge ' + (bio.face ? 'js' : '');
    $('r-voice-badge').textContent = bio.voice ? '✓ enrolled' : '— not enrolled';
    $('r-voice-badge').className   = 'badge ' + (bio.voice ? 'js' : '');
    $('r-rev-face').disabled  = !bio.face;
    $('r-rev-voice').disabled = !bio.voice;
  }

  function updateDeleteButtonState() {
    if (!targetUser) return;
    const btn = $('reg-delete'), note = $('reg-delete-note');
    btn.disabled = false;
    note.textContent = '';
    if (currentUser() === targetUser.username) {
      btn.disabled = true;
      note.textContent = 'You cannot delete your own account';
      return;
    }
    if (targetUser.role === 'admin' && targetUser.active) {
      const activeAdmins = lastUsers.filter(u => u.role === 'admin' && u.active);
      if (activeAdmins.length <= 1) {
        btn.disabled = true;
        note.textContent = 'Cannot delete the last active admin';
      }
    }
  }

  function openEditor(newMode, user) {
    mode = newMode;
    targetUser = user || null;
    $('reg-editor').classList.remove('hidden');
    $('reg-create-err').textContent = '';

    if (mode === 'create') {
      $('reg-editor-title').textContent = 'Create new account';
      $('reg-create-form').classList.remove('hidden');
      $('reg-edit-section').classList.add('hidden');
      $('r-user').value = '';
      $('r-pass').value = '';
      $('r-role').value = 'operator';
      stopStream(faceStream); faceStream = null;
    } else if (mode === 'edit' && user) {
      $('reg-editor-title').textContent = `Update — ${user.username}`;
      $('reg-create-form').classList.add('hidden');
      $('reg-edit-section').classList.remove('hidden');

      $('reg-edit-username').textContent = user.username;
      $('r-edit-pass').value = '';
      $('r-edit-role').value = user.role;

      $('r-face-go').disabled  = !engines().face.available;
      $('r-voice-go').disabled = !engines().voice.available;

      refreshEditorBioStatus();
      updateDeleteButtonState();
      ensureFaceCamera();
      startFacePreview();
    }
    $('reg-editor').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function closeEditor() {
    mode = null; targetUser = null;
    $('reg-editor').classList.add('hidden');
    stopStream(faceStream); faceStream = null;
    stopFacePreview();
  }

  // ── create ───────────────────────────────────────────────────────
  $('reg-create-form')?.addEventListener('submit', async (e) => {
    e.preventDefault();
    $('reg-create-err').textContent = '';
    const username = $('r-user').value.trim();
    const password = $('r-pass').value;
    const role     = $('r-role').value;
    if (!username) { $('reg-create-err').textContent = 'Username is required'; return; }
    if (!password) { $('reg-create-err').textContent = 'Password is required'; return; }
    try {
      await api('POST', '/api/register/user', { username, password, role });
      toast(`Account “${username}” created`, 'good');
      await fetchUsers();
      const created = lastUsers.find(u => u.username === username);
      if (created) {
        openEditor('edit', created);
        toast('You can now enrol face and voice for this user', 'good');
      } else {
        closeEditor();
      }
    } catch (err) {
      $('reg-create-err').textContent = err.message;
    }
  });

  // ── edit: password ───────────────────────────────────────────────
  $('r-edit-pass-save')?.addEventListener('click', async () => {
    if (mode !== 'edit' || !targetUser) return;
    const password = $('r-edit-pass').value;
    if (!password) { toast('Enter a new password first', 'bad'); return; }
    if (currentUser() === targetUser.username &&
        !window.confirm('Changing your own password will log you out everywhere. Continue?')) {
      return;
    }
    try {
      await api('PATCH', `/api/register/user/${encodeURIComponent(targetUser.username)}`,
                { password });
      $('r-edit-pass').value = '';
      toast('Password updated', 'good');
    } catch (e) { toast(e.message, 'bad'); }
  });

  // ── edit: role ───────────────────────────────────────────────────
  $('r-edit-role-save')?.addEventListener('click', async () => {
    if (mode !== 'edit' || !targetUser) return;
    const role = $('r-edit-role').value;
    if (role === targetUser.role) { toast('Role unchanged', ''); return; }
    try {
      await api('PATCH', `/api/register/user/${encodeURIComponent(targetUser.username)}`,
                { role });
      toast(`Role updated to ${role}`, 'good');
      await fetchUsers();
      const updated = lastUsers.find(u => u.username === targetUser.username);
      if (updated) { targetUser = updated; updateDeleteButtonState(); }
    } catch (e) { toast(e.message, 'bad'); }
  });

  // ── edit: delete ─────────────────────────────────────────────────
  $('reg-delete')?.addEventListener('click', async () => {
    if (mode !== 'edit' || !targetUser) return;
    if (!window.confirm(`Delete user "${targetUser.username}" permanently? This cannot be undone.`)) return;
    try {
      await api('DELETE', `/api/register/user/${encodeURIComponent(targetUser.username)}`);
      toast(`User "${targetUser.username}" deleted`, 'good');
      closeEditor();
      await fetchUsers();
    } catch (e) { toast(e.message, 'bad'); }
  });

  // ── edit: enrolment ──────────────────────────────────────────────
  async function enrol(kind, capture, promptFn, progEl) {
    if (busy || !targetUser) return;
    busy = true; progEl.textContent = '';
    try {
      const s = await api('POST', '/api/register/start', { username: targetUser.username, kind });
      let got = 0, tries = 0;
      stopFacePreview();
      while (got < s.needed && tries < s.needed * 4) {
                                                    tries++;
                                                    promptFn(got);
                                                    let data;
                                                    try { data = await capture(); } catch (e) { progEl.textContent = e.message; break; }
                                                    try {
                                                      const r = await api('POST', '/api/register/sample',
                                                        { session_id: s.session_id, data_b64: data });
                                                      got = r.count;
                                                      progEl.textContent = `${got} of ${s.needed} samples`;
                                                    } catch (e) { progEl.textContent = e.message; await sleep(1200); }
                                                  }
      if (got >= s.needed) {
                            await api('POST', '/api/register/commit', { session_id: s.session_id });
                            progEl.textContent = 'Enrolled.';
                            toast(`${kind === 'face' ? 'Face' : 'Voice'} enrolled`, 'good');
                            await fetchUsers();
                            const updated = lastUsers.find(u => u.username === targetUser.username);
                            if (updated) { targetUser = updated; refreshEditorBioStatus(); }
                          } 
      else if (!progEl.textContent.includes('Enrolled')) {
                          toast('Enrolment not finished — try again.', 'bad');
                        }
    }
    catch (e) { progEl.textContent = e.message; }
    finally { 
            busy = false;
            if (mode === 'edit' && targetUser) startFacePreview();
          }
  }

  $('r-face-go')?.addEventListener('click', async () => {
    await ensureFaceCamera();
    if (!faceStream) return;
    stopFacePreview();                        // ← hide overlay during capture
    await enrol('face',
      async () => { await sleep(900); return frameFromVideo($('r-video')); },
      () => {},
      $('r-face-prog'));
    if (mode === 'edit' && targetUser) startFacePreview();   // ← resume
  });

  $('r-voice-go')?.addEventListener('click', () => enrol('voice',
    () => recordWav(3.5, state.status.settings.voice.microphone_id),
    (i) => { $('r-voice-prog').textContent = `Say: ${PROMPTS[i % PROMPTS.length]}`; },
    $('r-voice-prog')));

  for (const kind of ['face', 'voice']) {
    $(`r-rev-${kind}`)?.addEventListener('click', async () => {
      if (!targetUser) return;
      if (!window.confirm(`Remove ${kind} template for ${targetUser.username}?`)) return;
      try {
        await api('POST', '/api/register/revoke',
          { username: targetUser.username, kind });
        toast(`${kind} template removed`, 'good');
        await fetchUsers();
        const updated = lastUsers.find(u => u.username === targetUser.username);
        if (updated) { targetUser = updated; refreshEditorBioStatus(); }
      } catch (e) { toast(e.message, 'bad'); }
    });
  }

  // ── wiring ───────────────────────────────────────────────────────
  $('reg-fetch-users')?.addEventListener('click', fetchUsers);
  $('r-create')?.addEventListener('click', () => openEditor('create'));
  $('reg-editor-close')?.addEventListener('click', closeEditor);

  // Live-refresh the table whenever a write changes it server-side.
  bus.on('register.users.changed', () => { if (isAdmin()) fetchUsers(); });
  // Also re-fetch when we come back online (session restored, WS reconnect).
  bus.on('status', () => { if (isAdmin() && !mode) fetchUsers(); });

  return {
    onShow() { if (isAdmin()) fetchUsers(); },
    onHide()  { closeEditor(); },
  };
}