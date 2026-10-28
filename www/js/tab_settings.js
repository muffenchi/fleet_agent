// Settings tab: speech/microphone, camera, and what Captain has saved for this account.
import { listEngines } from './stt/registry.js';
import { listDevices } from './media.js';

const $ = (id) => document.getElementById(id);
const SECTIONS = [['joystick', 'Joystick'], ['voice', 'Voice and microphone'], ['camera', 'Camera']];

export function initSettings({ api, state, bus, toast }) {
  const factorBoxes = () => [...document.querySelectorAll('fieldset.factors input')];

  async function fillDevices() {
    const { mics, cams } = await listDevices();
    const fill = (sel, items, keep) => {
      sel.replaceChildren(new Option('System default', ''), ...items.map((d) => new Option(d.label, d.id)));
      sel.value = items.some((d) => d.id === keep) ? keep : '';
    };
    const s = state.status.settings;
    fill($('s-mic'), mics, s.voice.microphone_id); fill($('s-cam'), cams, s.camera.device_id);
  }

  function render() {
    const s = state.status;
    if (!s.captain.logged_in) return;
    const v = s.settings.voice;
    const eng = $('s-engine');
    eng.replaceChildren(...listEngines().map((e) => new Option(e.label + (e.available ? '' : ' — not available here'), e.name)));
    eng.value = v.stt_engine;
    $('s-lang').value = v.language; $('s-wake').value = v.wake_word;
    $('s-ttl').value = Math.round(v.unlock_ttl_s / 60); $('s-confirm').value = v.confirm_window_s;
    $('s-wake-req').checked = v.require_wake_word;
    for (const box of factorBoxes()) {
      box.checked = v.unlock_factors.includes(box.value);
      const kind = { voiceprint: 'voice', face: 'face' }[box.value];
      if (kind) {
        const e = s.engines[kind];
        box.disabled = !e.available;
        $(kind === 'voice' ? 'fx-voice' : 'fx-face').textContent = e.available ? '' : `— not available: ${e.reason}`;
      }
    }
    $('s-voice-saved').textContent = s.saved_ts.voice ? `Saved ${new Date(s.saved_ts.voice / 1000).toLocaleString()}` : 'Not saved yet — using defaults';
    $('s-cam-saved').textContent = s.saved_ts.camera ? `Saved ${new Date(s.saved_ts.camera / 1000).toLocaleString()}` : 'Not saved yet — using defaults';
    const rows = $('saved-rows'); rows.replaceChildren();
    for (const [scope, label] of SECTIONS) {
      const tr = document.createElement('tr');
      const ts = s.saved_ts[scope];
      tr.append(Object.assign(document.createElement('td'), { textContent: label }),
        Object.assign(document.createElement('td'), { textContent: ts ? new Date(ts / 1000).toLocaleString() : 'never — using defaults' }));
      const td = document.createElement('td'), b = document.createElement('button');
      b.textContent = 'Reset to defaults'; b.className = 'quiet';
      b.addEventListener('click', async () => {
        try { await api('POST', `/api/settings/${scope}/reset`); toast(`${label} reset`, 'good'); } catch (e) { toast(e.message, 'bad'); }
      });
      td.appendChild(b); tr.appendChild(td); rows.appendChild(tr);
    }
    $('s-err').textContent = s.settings_storage ? '' : 'This Captain has no agent settings storage (the agent patch is not installed). Changes made here will not be kept.';
  }

  $('s-save-voice').addEventListener('click', async () => {
    $('s-err').textContent = '';
    try {
      await api('PUT', '/api/settings/voice', {
        password: $('s-pass').value || null,
        value: {
          stt_engine: $('s-engine').value, language: $('s-lang').value.trim(), microphone_id: $('s-mic').value,
          wake_word: $('s-wake').value.trim(), require_wake_word: $('s-wake-req').checked,
          unlock_ttl_s: Number($('s-ttl').value) * 60, confirm_window_s: Number($('s-confirm').value),
          unlock_factors: factorBoxes().filter((b) => b.checked).map((b) => b.value),
        } });
      $('s-pass').value = '';
      toast('Voice settings saved to Captain', 'good');
    } catch (e) { $('s-err').textContent = e.message; }
  });
  $('s-save-cam').addEventListener('click', async () => {
    try { await api('PUT', '/api/settings/camera', { value: { device_id: $('s-cam').value } }); toast('Camera saved to Captain', 'good'); }
    catch (e) { toast(e.message, 'bad'); }
  });
  $('s-restore').addEventListener('click', async () => {
    try { await api('POST', '/api/settings/restore'); toast('Restored from Captain', 'good'); } catch (e) { toast(e.message, 'bad'); }
  });

  if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
    navigator.mediaDevices.addEventListener('devicechange', () => state.status && state.status.captain.logged_in && fillDevices());
  }
  bus.on('status', render);
  bus.on('settings', () => { render(); fillDevices(); });
  return { onShow() { render(); fillDevices(); } };
}
