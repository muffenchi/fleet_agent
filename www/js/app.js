// Fleet Agent UI shell: session state, live events from the agent, tab switching.
import { api } from './api.js';
import { initCommands } from './tab_commands.js';
import { initVoice } from './tab_voice.js';
import { initJoystick } from './tab_joystick.js';
import { initSettings } from './tab_settings.js';
import { initRegister } from './tab_register.js';

const $ = (id) => document.getElementById(id);
const state = { status: null };
const handlers = new Map();
const bus = {
  on(type, fn) { (handlers.get(type) || handlers.set(type, []).get(type)).push(fn); },
  emit(type, data) { (handlers.get(type) || []).forEach((fn) => fn(data)); },
};

function toast(msg, kind = '') {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $('toasts').appendChild(el);
  setTimeout(() => el.remove(), 5000);
}

let ws = null, wsTries = 0, watching = false;
function sendWatch() {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify({ watch: 'joystick', on: watching }));
}
function watchJoystick(on) { watching = on; sendWatch(); }

const ctx = { api, state, bus, toast, refresh, watchJoystick };
const tabs = {};
let current = 'voice';

// Every status snapshot and event carries the agent's revision number. A snapshot older than what the
// page already shows (a slow GET that lost the race with a newer event) is dropped, never rendered.
function adopt(status) {
  if (state.status && status.rev < (state.status.rev || 0)) return;
  state.status = status;
  render();
}

async function refresh() {
  adopt(await api('GET', '/api/status'));
}

function render() {
  const s = state.status;
  if (!s) return;
  const loggedIn = s.captain.logged_in, user = s.captain.user;

  // link lamp
  const lamp = $('link-state').querySelector('.lamp');
  const up = loggedIn && s.captain.presence === 'up';
  lamp.className = 'lamp ' + (up ? 'go' : loggedIn ? 'warn' : 'off');
  $('link-text').textContent = up ? `Connected to ${new URL(s.captain.url).host}`
    : loggedIn ? 'Reconnecting to Captain…' : 'Not connected';
  $('who').textContent = user ? `${user.username} · ${user.role}` : '';
  $('logout').classList.toggle('hidden', !loggedIn);

  // NOTE: no target-drone picker in the agent UI — drone selection happens on
  // the Captain map (right-click or joystick focus) and lives Captain-side.
  // The agent learns a target only if a future captain-side push sets it; see
  // the TODO in tab_commands.js sendAction().

  // tabs
  const hasPad = !!s.joystick.present, isAdmin = loggedIn && user?.role === 'admin';
  $('tab-btn-commands').disabled = !loggedIn;
  $('tab-btn-joystick').disabled = !loggedIn || !hasPad;
  $('tab-btn-joystick').title = hasPad ? '' : (s.joystick.error
    ? `Joystick support unavailable: ${s.joystick.error}` : 'No joystick detected on this laptop');
  $('tab-btn-register').disabled = !isAdmin;
  $('tab-btn-register').title = isAdmin ? '' : loggedIn
    ? `Signed in as ${user?.role || 'unknown'}; only a Captain admin can register users`
    : 'Log in as a Captain admin to register users';
  document.querySelector('.tab[data-tab="voice"]').disabled = !loggedIn;
  document.querySelector('.tab[data-tab="settings"]').disabled = !loggedIn;
  $('lamp-commands').className = 'lamp ' + (loggedIn ? 'go' : 'off');
  $('lamp-joystick').className = 'lamp ' + (hasPad ? 'go' : 'off');
  $('lamp-voice').className = 'lamp ' + (!loggedIn ? 'off' : s.voice.gate.unlocked ? 'go' : 'warn');

  if (!loggedIn) show('login');
  else if (current === 'login' || document.querySelector(`.tab[data-tab="${current}"]`).disabled) show('commands');
  bus.emit('status', s);
}

function show(name) {
  if (name !== 'login' && tabs[current] && tabs[current].onHide && current !== name) tabs[current].onHide();
  if (name !== 'login') current = name;
  else current = 'login';
  document.querySelectorAll('.view').forEach((v) => v.classList.toggle('hidden', v.id !== `view-${name}`));
  document.querySelectorAll('.tab').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === name)));
  if (tabs[name] && tabs[name].onShow) tabs[name].onShow();
}

function onEvent(ev) {
  const s = state.status;
  if (s && ev.type !== 'status' && ev.rev > (s.rev || 0)) s.rev = ev.rev;
  switch (ev.type) {
    case 'status': adopt(ev); break;
    case 'session': case 'session.expired':
      if (ev.type === 'session.expired') toast('Your Captain session expired — log in again.', 'bad');
      refresh(); break;
    case 'presence': if (s) { s.captain.presence = ev.state; render(); } break;
    case 'joystick.presence': if (s) { s.joystick = { ...s.joystick, present: ev.present, name: ev.name, count: ev.count, error: ev.error ?? s.joystick.error }; render(); } break;
    case 'voice.gate': if (s) { s.voice.gate = { unlocked: ev.unlocked, remaining_s: ev.remaining_s, ttl_s: ev.ttl_s, unlocked_by: ev.unlocked_by }; render(); } break;
    case 'settings': if (s) { s.settings = ev.settings; s.saved_ts = ev.saved_ts; render(); } break;
    case 'drones': if (s) { s.drones = ev.drones; s.target = ev.target; render(); } break;
    case 'warning': toast(ev.detail, 'bad'); break;
    case 'command.result': toast(ev.ok ? `${ev.label} — accepted by Captain` : `${ev.label} — ${ev.detail}`, ev.ok ? 'good' : 'bad'); break;
  }
  bus.emit(ev.type, ev);
}

function connect() {
  ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/ui`);
  ws.onopen = () => { wsTries = 0; sendWatch(); };
  ws.onmessage = (e) => { try { onEvent(JSON.parse(e.data)); } catch (err) { console.error(err); } };
  ws.onclose = () => setTimeout(connect, Math.min(5000, 400 * 2 ** wsTries++));
}

async function boot() {
  document.querySelectorAll('.tab').forEach((b) => b.addEventListener('click', () => { if (!b.disabled) show(b.dataset.tab); }));
  $('logout').addEventListener('click', async () => { await api('POST', '/api/logout'); await refresh(); });
  $('login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    $('login-err').textContent = ''; $('login-btn').disabled = true;
    try {
      adopt(await api('POST', '/api/login', {
        url: $('login-url').value.trim(), username: $('login-user').value, password: $('login-pass').value }));
      $('login-pass').value = '';
      localStorage.setItem('fleetAgent.url', $('login-url').value.trim());
      show('commands');
    } catch (err) { $('login-err').textContent = err.message; }
    finally { $('login-btn').disabled = false; }
  });

  Object.assign(tabs, {
    commands: initCommands(ctx), voice: initVoice(ctx), joystick: initJoystick(ctx), settings: initSettings(ctx), register: initRegister(ctx),
  });
  await refresh();
  $('login-url').value = localStorage.getItem('fleetAgent.url') || state.status.captain.url || '';
  show(state.status.captain.logged_in ? 'commands' : 'login');
  connect();
}
boot().catch((err) => { console.error(err); toast(err.message, 'bad'); });