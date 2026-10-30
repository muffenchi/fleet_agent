// Commands tab: informational catalog of every command the agent can send.
// The catalog lives in voice_catalog.js and is shared with the Voice tab.
// This tab does NOT execute anything — voice and joystick fire commands
// directly; the Captain map's right-click menu is the manual path.

import { VOICE_CATALOG, GROUP_ORDER } from './voice_catalog.js';

const $ = (id) => document.getElementById(id);

export function initCommands({ api, state, bus, toast }) {
  async function loadCommands() {
    const s = state.status;
    if (!s || !s.captain || !s.captain.logged_in) {
      const tbody = $('commands-tbody');
      if (tbody) {
        tbody.innerHTML = '<tr><td colspan="5" class="muted" style="text-align: center; padding: 20px;">Log in to Captain to load the command catalog.</td></tr>';
      }
      return;
    }
    try {
      const data = await api('GET', '/api/commands');
      renderFromData(data);
    } catch (err) {
      console.warn('Could not query /api/commands:', err);
      renderFromData({
        commands: buildFromState(),
        target: state.status?.target,
        drones: state.status?.drones || [],
      });
    }
  }

  function buildFromState() {
    const s = state.status;
    if (!s) return [];
    const jsBindings = s.settings?.joystick?.bindings || {};
    const wake = s.settings?.voice?.wake_word || 'captain';
    const reqWake = s.settings?.voice?.require_wake_word;
    const actionToBtns = {};
    for (const [btn, act] of Object.entries(jsBindings)) {
      (actionToBtns[act] = actionToBtns[act] || []).push(`Button ${btn}`);
    }
    return Object.entries(s.actions || {}).map(([name, act]) => {
      const phrases = act.voice_phrases || [name];
      const triggers = phrases.map(p => reqWake && wake ? `${wake} ${p}` : p);
      return {
        name, label: act.label || name, command: act.command,
        params: act.params || {}, confirm: !!act.confirm,
        joystick_buttons: actionToBtns[name] || [],
        voice_triggers: triggers,
      };
    });
  }

  function renderFromData(data) {
    const tbody = $('commands-tbody');
    if (!tbody) return;

    const serverByName = {};
    for (const row of (data.commands || [])) serverByName[row.name] = row;

    const rows = VOICE_CATALOG.map(entry => {
      const srv = serverByName[entry.name] || {};
      return {
        ...entry,
        label: entry.label || srv.label || entry.name,
        joystick_buttons: srv.joystick_buttons || [],
        voice_triggers: entry.voiceHints || srv.voice_triggers || [entry.name],
        server: srv,
      };
    });

    for (const [name, srv] of Object.entries(serverByName)) {
      if (VOICE_CATALOG.some(e => e.name === name)) continue;
      rows.push({
        name,
        label: srv.label || name,
        group: 'drone',
        confirm: !!srv.confirm,
        exec: { mode: 'action', action: name },
        joystick_buttons: srv.joystick_buttons || [],
        voice_triggers: srv.voice_triggers || [name],
        server: srv,
      });
    }

    tbody.replaceChildren();
    let any = false;
    for (const [key, title] of GROUP_ORDER) {
      const inGroup = rows.filter(r => r.group === key);
      if (!inGroup.length) continue;
      any = true;
      const header = document.createElement('tr');
      header.className = 'cmd-group-row';
      header.innerHTML = `<td colspan="5"><span class="cmd-group-label">${escapeHtml(title)}</span></td>`;
      tbody.appendChild(header);
      for (const entry of inGroup) tbody.appendChild(renderRow(entry));
    }
    if (!any) {
      tbody.innerHTML = '<tr><td colspan="5" class="muted" style="text-align: center; padding: 20px;">No commands available.</td></tr>';
    }
  }

  function renderRow(entry) {
    const tr = document.createElement('tr');

    // 1. Command
    const tdCmd = document.createElement('td');
    tdCmd.innerHTML = `<strong>${escapeHtml(entry.label)}</strong> <code class="muted small">(${escapeHtml(entry.name)})</code>`;

    // 2. ID (MAVLink id / NAV / FLEET)
    const tdId = document.createElement('td');
    const idHint = entry.server?.command
                ?? entry.exec?.command
                ?? (entry.exec?.mode === 'broadcast' ? `MAV ${entry.exec.command}` : null);
    if (idHint != null && idHint !== '') {
      tdId.innerHTML = `<span class="badge">${escapeHtml(String(idHint))}</span>`;
    } else if (entry.exec?.mode === 'nav') {
      tdId.innerHTML = `<span class="badge voice">NAV</span>`;
    } else if (entry.exec?.mode === 'fleet') {
      tdId.innerHTML = `<span class="badge">FLEET</span>`;
    } else {
      tdId.innerHTML = `<span class="muted small">—</span>`;
    }

    // 3. Joystick
    const tdJoy = document.createElement('td');
    tdJoy.innerHTML = entry.joystick_buttons?.length
      ? `<div class="tag-list">${entry.joystick_buttons.map(b => `<span class="badge js">${escapeHtml(b)}</span>`).join('')}</div>`
      : `<span class="muted small">— unassigned —</span>`;

    // 4. Voice
    const tdVoice = document.createElement('td');
    tdVoice.innerHTML = entry.voice_triggers?.length
      ? `<div class="tag-list">${entry.voice_triggers.map(t => `<span class="badge voice">"${escapeHtml(t)}"</span>`).join('')}</div>`
      : `<span class="muted small">— none —</span>`;

    // 5. Safety
    const tdSafety = document.createElement('td');
    if (entry.needsBackend) {
      tdSafety.innerHTML = `<span class="badge warn" title="${escapeHtml(entry.needsBackendNote || 'Backend not wired')}">Backend TBD</span>`;
    } else if (entry.confirm) {
      tdSafety.innerHTML = `<span class="badge warn">Confirm</span>`;
    } else {
      tdSafety.innerHTML = `<span class="muted small">Immediate</span>`;
    }

    tr.append(tdCmd, tdId, tdJoy, tdVoice, tdSafety);
    return tr;
  }

  function escapeHtml(str) {
    return String(str || '').replace(/[&<>"']/g, (m) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[m]));
  }

  // Refresh from Captain — reloads catalog + settings
  $('btn-refresh-captain')?.addEventListener('click', async () => {
    try {
      $('btn-refresh-captain').disabled = true;
      $('btn-refresh-captain').textContent = '⟳ Loading…';
      await api('POST', '/api/settings/restore');
      await loadCommands();
      toast('Command catalog reloaded from Captain', 'good');
    } catch (err) {
      toast(`Refresh failed: ${err.message}`, 'bad');
    } finally {
      $('btn-refresh-captain').disabled = false;
      $('btn-refresh-captain').textContent = '⟳ Refresh from Captain';
    }
  });

  // Live event log — records every dispatched command (voice / joystick / agent)
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

  // Sync
  bus.on('status', (s) => { if (s?.captain?.logged_in) loadCommands(); });
  bus.on('settings', () => loadCommands());
  bus.on('drones', () => { if (state.status?.captain?.logged_in) loadCommands(); });

  return { onShow: loadCommands, render: loadCommands };
}