// Commands tab: informational view of Captain's stored command→button/voice
// bindings, plus a per-row Execute button for testing. There is no target-drone
// picker in this UI — the operator selects the drone on the Captain map, and any
// future push of the current focus will surface here as state.status.target.
const $ = (id) => document.getElementById(id);

export function initCommands({ api, state, bus, toast, refresh }) {
  let pendingAction = null;
  let cachedData = null;

  async function loadCommands() {
    const s = state.status;
    if (!s || !s.captain || !s.captain.logged_in) {
      const tbody = $('commands-tbody');
      if (tbody) {
        tbody.innerHTML = '<tr><td colspan="6" class="muted" style="text-align: center; padding: 20px;">Please log in to Captain to retrieve assigned commands.</td></tr>';
      }
      return;
    }

    try {
      const data = await api('GET', '/api/commands');
      cachedData = data;
      renderTable(data);
    } catch (err) {
      console.warn('Could not query /api/commands:', err);
      buildFromState();
    }
  }

  function renderTable(data) {
    const tbody = $('commands-tbody');
    if (!tbody) return;

    const commands = data.commands || [];
    if (!commands.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="muted" style="text-align: center; padding: 20px;">No commands returned from Captain.</td></tr>';
      return;
    }

    tbody.replaceChildren();

    commands.forEach((cmd) => {
      const tr = document.createElement('tr');

      // 1. Action Name & Label
      const tdAction = document.createElement('td');
      tdAction.innerHTML = `<strong>${escapeHtml(cmd.label)}</strong> <code class="muted small">(${escapeHtml(cmd.name)})</code>`;

      // 2. MAVLink Command ID
      const tdMav = document.createElement('td');
      tdMav.innerHTML = `<span class="badge">CMD ${cmd.command}</span>`;

      // 3. Joystick Button Assignment (retrieved from Captain)
      const tdJoy = document.createElement('td');
      if (cmd.joystick_buttons && cmd.joystick_buttons.length) {
        tdJoy.innerHTML = `<div class="tag-list">${cmd.joystick_buttons.map((b) => `<span class="badge js">${escapeHtml(b)}</span>`).join('')}</div>`;
      } else {
        tdJoy.innerHTML = `<span class="muted small">— unassigned —</span>`;
      }

      // 4. Voice Trigger Phrases (retrieved with wake word)
      const tdVoice = document.createElement('td');
      if (cmd.voice_triggers && cmd.voice_triggers.length) {
        tdVoice.innerHTML = `<div class="tag-list">${cmd.voice_triggers.map((t) => `<span class="badge voice">“${escapeHtml(t)}”</span>`).join('')}</div>`;
      } else {
        tdVoice.innerHTML = `<span class="muted small">— none —</span>`;
      }

      // 5. Safety Mode
      const tdSafety = document.createElement('td');
      if (cmd.confirm) {
        tdSafety.innerHTML = `<span class="badge warn">Hold / Confirm</span>`;
      } else {
        tdSafety.innerHTML = `<span class="muted small">Immediate</span>`;
      }

      // 6. Execute Button & Controls — target comes from state.status.target;
      //    if there isn't one, the click handler toasts a clear message.
      const tdExec = document.createElement('td');
      tdExec.style.textAlign = 'right';
      const execWrap = document.createElement('div');
      execWrap.style.display = 'inline-flex';
      execWrap.style.alignItems = 'center';
      execWrap.style.gap = '8px';
      execWrap.style.justifyContent = 'flex-end';

      if (cmd.name === 'takeoff') {
        const altInput = document.createElement('input');
        altInput.type = 'number';
        altInput.min = '2';
        altInput.max = '120';
        altInput.value = cmd.takeoff_alt_m || 25;
        altInput.style.width = '55px';
        altInput.style.padding = '3px 6px';
        altInput.title = 'Takeoff altitude in meters';
        const mLabel = document.createElement('span');
        mLabel.className = 'muted small';
        mLabel.textContent = 'm';

        const btn = document.createElement('button');
        btn.className = 'primary btn-sm';
        btn.textContent = 'Takeoff';
        btn.addEventListener('click', () => {
          const alt = parseFloat(altInput.value) || 25;
          sendAction('takeoff', { takeoff_alt_m: alt });
        });
        execWrap.append(altInput, mLabel, btn);
      } else if (cmd.name === 'arm') {
        const btn = document.createElement('button');
        btn.className = 'btn-sm';
        btn.style.background = '#e67e22';
        btn.style.borderColor = '#d35400';
        btn.style.color = '#fff';
        btn.textContent = 'Arm';
        btn.addEventListener('click', () => confirmAction('arm', 'ARM motors'));
        execWrap.appendChild(btn);
      } else if (cmd.name === 'disarm') {
        const btn = document.createElement('button');
        btn.className = 'btn-sm';
        btn.style.background = '#c0392b';
        btn.style.borderColor = '#962d22';
        btn.style.color = '#fff';
        btn.textContent = 'Disarm';
        btn.addEventListener('click', () => confirmAction('disarm', 'DISARM motors'));
        execWrap.appendChild(btn);
      } else {
        const btn = document.createElement('button');
        btn.className = 'btn-sm';
        btn.textContent = 'Send';
        btn.addEventListener('click', () => sendAction(cmd.name));
        execWrap.appendChild(btn);
      }

      tdExec.appendChild(execWrap);
      tr.append(tdAction, tdMav, tdJoy, tdVoice, tdSafety, tdExec);
      tbody.appendChild(tr);
    });
  }

  function buildFromState() {
    const s = state.status;
    if (!s || !s.captain.logged_in) return;
    const jsBindings = (s.settings && s.settings.joystick && s.settings.joystick.bindings) || {};
    const wake = (s.settings && s.settings.voice && s.settings.voice.wake_word) || 'captain';
    const reqWake = s.settings && s.settings.voice && s.settings.voice.require_wake_word;

    const actionToBtns = {};
    for (const [btn, act] of Object.entries(jsBindings)) {
      (actionToBtns[act] = actionToBtns[act] || []).push(`Button ${btn}`);
    }

    const rows = Object.entries(s.actions || {}).map(([name, act]) => {
      const phrases = act.voice_phrases || [name];
      const triggers = phrases.map((p) => (reqWake && wake ? `${wake} ${p}` : p));
      return {
        name,
        label: act.label || name,
        command: act.command || '—',
        params: act.params || {},
        confirm: Boolean(act.confirm),
        joystick_buttons: actionToBtns[name] || [],
        voice_triggers: triggers,
        takeoff_alt_m: 25,
      };
    });

    renderTable({
      commands: rows,
      drones: s.drones || [],
      target: s.target,
    });
  }

  function escapeHtml(str) {
    return String(str || '').replace(/[&<>"']/g, (m) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[m]));
  }

  // Execute one action against the current target (from state.status.target).
  // The agent UI has no target picker; the operator sets the target on the
  // Captain map, and if we ever get a captain→agent push of the current
  // focus, state.status.target will carry it. Until then this is a
  // "no target set" toast — deliberate, so we never fire at the wrong drone.
  async function sendAction(action, extra = {}) {
    const target = state.status && state.status.target;
    if (!target) {
      toast('No target drone set — select one on the Captain map first', 'bad');
      return;
    }
    try {
      const res = await api('POST', '/api/command', { action, drone_id: target, ...extra });
      toast(res.detail || `${action} sent to Captain`, 'good');
    } catch (err) {
      toast(err.message, 'bad');
    }
  }

  function confirmAction(action, label) {
    pendingAction = action;
    $('cmd-confirm-text').textContent = `Are you sure you want to ${label}?`;
    $('cmd-confirm-bar').classList.remove('hidden');
  }

  function hideConfirm() {
    pendingAction = null;
    $('cmd-confirm-bar').classList.add('hidden');
  }

  // Confirm motor action listeners
  $('cmd-confirm-yes').addEventListener('click', () => {
    if (pendingAction) {
      sendAction(pendingAction);
      hideConfirm();
    }
  });
  $('cmd-confirm-no').addEventListener('click', hideConfirm);

  // Refresh from Captain button
  $('btn-refresh-captain').addEventListener('click', async () => {
    try {
      $('btn-refresh-captain').disabled = true;
      $('btn-refresh-captain').textContent = '⟳ Loading…';
      await api('POST', '/api/settings/restore');
      await loadCommands();
      toast('Command assignments and settings reloaded from Captain', 'good');
    } catch (err) {
      toast(`Refresh failed: ${err.message}`, 'bad');
    } finally {
      $('btn-refresh-captain').disabled = false;
      $('btn-refresh-captain').textContent = '⟳ Refresh from Captain';
    }
  });

  // Custom command form
  $('cmd-custom-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const cmdId = parseInt($('cmd-custom-id').value, 10);
    if (isNaN(cmdId)) {
      toast('Invalid command ID', 'bad');
      return;
    }
    const label = $('cmd-custom-label').value.trim() || `Command ${cmdId}`;
    const params = {};
    for (let i = 1; i <= 7; i++) {
      params[`param${i}`] = parseFloat($(`cmd-param-${i}`).value) || 0;
    }
    const target = state.status && state.status.target;
    if (!target) {
      toast('No target drone set — select one on the Captain map first', 'bad');
      return;
    }
    try {
      const res = await api('POST', '/api/command', {
        action: 'custom',
        drone_id: target,
        custom_command: cmdId,
        params,
        label,
      });
      toast(res.detail || 'Custom command accepted by Captain', 'good');
    } catch (err) {
      toast(err.message, 'bad');
    }
  });

  // Live WebSocket command history log
  bus.on('command.result', (ev) => {
    const list = $('cmd-log');
    if (!list) return;
    const li = document.createElement('li');
    li.className = ev.ok ? 'executed' : 'rejected';
    const lamp = document.createElement('i');
    lamp.className = `lamp ${ev.ok ? 'go' : 'stop'}`;
    const body = document.createElement('div');
    const timeStr = new Date().toLocaleTimeString();
    body.innerHTML = `<strong>${escapeHtml(ev.label)}</strong> [${escapeHtml(ev.source)}] — ${ev.ok ? 'Accepted' : 'Failed'}<span class="sub">${timeStr} · ${escapeHtml(ev.detail || '')}</span>`;
    li.append(lamp, body);
    list.prepend(li);
    while (list.children.length > 50) list.removeChild(list.lastChild);
  });

  // Synchronize on events
  bus.on('status', (s) => {
    if (s && s.captain && s.captain.logged_in) {
      loadCommands();
    }
  });

  bus.on('settings', () => {
    loadCommands();
  });

  // Drones changed — just re-run the table so takeoff buttons reflect the
  // current alt. No dropdown to sync anymore.
  bus.on('drones', () => {
    if (state.status && state.status.captain.logged_in) loadCommands();
  });

  return {
    onShow: loadCommands,
    render: loadCommands,
  };
}