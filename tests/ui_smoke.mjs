// Drives the real agent UI (index.html + the ES modules) in jsdom against the live rig. Run via tests/run_ui_smoke.sh
import { JSDOM, CookieJar } from 'jsdom';
import { build } from 'esbuild';
import fs from 'node:fs';
import path from 'node:path';

const { captain, agent, token } = JSON.parse(process.argv[2]);
const WWW = process.argv[3];
const base = `http://127.0.0.1:${agent}`, cap = `http://127.0.0.1:${captain}`;
const bundle = await build({ entryPoints: [path.join(WWW, 'js/app.js')], bundle: true, format: 'iife', write: false });
const html = fs.readFileSync(path.join(WWW, 'index.html'), 'utf8').replace(/<script[^>]*><\/script>/, '').replace(/<link[^>]*>/g, '');

const jar = new CookieJar(); jar.setCookieSync(`fleet_agent_k=${token}`, base);
const dom = new JSDOM(html, { url: base + '/', cookieJar: jar, runScripts: 'outside-only', pretendToBeVisual: true });
const w = dom.window;
w.fetch = (u, o = {}) => fetch(new URL(u, base), { ...o, headers: { ...(o.headers || {}), Cookie: `fleet_agent_k=${token}` } });
w.scrollTo = () => {};
const errors = []; w.addEventListener('error', (e) => errors.push(e.message)); process.on('unhandledRejection', (e) => errors.push(String(e)));
process.on('exit', () => { if (errors.length) console.log('PAGE ERRORS:', errors); });
w.eval(bundle.outputFiles[0].text);

const $ = (id) => w.document.getElementById(id);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(fn, what, ms = 4000) { const t = Date.now(); while (Date.now() - t < ms) { try { if (fn()) return; } catch (_) {} await sleep(40); } throw new Error('timed out: ' + what); }
const visible = (id) => !$(id).classList.contains('hidden');
const rig = (p) => fetch(cap + p).then((r) => r.json());
let n = 0; const ok = (c, m) => { if (!c) throw new Error('FAILED: ' + m); n++; console.log('  ok  ' + m); };

// 1. boots to the login view, prefilled with the Captain address from the agent's config
await until(() => visible('view-login') && $('login-url').value !== '', 'login view');
ok($('login-url').value === cap, 'login form prefilled with Captain address');
ok($('tab-btn-joystick').disabled && $('tab-btn-register').disabled, 'joystick + register tabs disabled before login');

// 2. wrong password shows the error; right one logs in
$('login-user').value = 'op1'; $('login-pass').value = 'bad';
$('login-form').dispatchEvent(new w.Event('submit', { cancelable: true }));
await until(() => $('login-err').textContent.includes('Invalid username or password'), 'login error');
ok(true, 'wrong password shows "Invalid username or password"');
$('login-pass').value = 'pw-op1';
$('login-form').dispatchEvent(new w.Event('submit', { cancelable: true }));
await until(() => visible('view-voice'), 'voice view after login');
await until(() => $('link-text').textContent.startsWith('Connected to'), 'presence up');
await until(() => $('vc-action').options.length > 0, 'voice command action options');
ok(true, 'voice command actions load from the current status');
ok($('who').textContent === 'op1 · operator', 'header shows operator and role');
ok($('link-state').querySelector('.lamp').classList.contains('go'), 'Captain link lamp is lit');
ok($('target').options.length === 3 && [...$('target').options].map((o) => o.value).includes('alpha'), 'target picker lists drones from Captain');
ok($('tab-btn-register').disabled, 'register tab stays disabled for a non-admin');
ok($('gate-title').textContent === 'Voice control is locked' && visible('unlock-form'), 'voice gate starts locked');
ok(!visible('uf-voice-row') && visible('uf-password-row'), 'default unlock factor is password only');

// 3. joystick tab follows the hardware
ok($('tab-btn-joystick').disabled, 'joystick tab disabled with no pad');
await rig('/_rig/plug?on=1&n=8');
await until(() => !$('tab-btn-joystick').disabled, 'joystick tab enabled');
ok($('lamp-joystick').classList.contains('go'), 'joystick lamp lit when a pad is plugged in');
$('target').value = '2'; $('target').dispatchEvent(new w.Event('change'));
$('tab-btn-joystick').click();
await until(() => visible('view-joystick') && $('buttons').children.length === 8, 'button grid (8 buttons from live pad state)');
ok($('pad-name').textContent === 'Test Pad', 'pad name shown');
const sel = $('buttons').children[3].querySelector('select');
ok(sel.value === '' && $('buttons').children[2].querySelector('select').value === 'land', 'default bindings loaded (2 = land), 3 unbound');
sel.value = 'rtl'; sel.dispatchEvent(new w.Event('change'));
$('js-hold').value = '400'; $('js-save').click();
await until(async () => true, 'save'); await sleep(400);
const saved = await rig('/_rig/settings?u=op1');
ok(saved.joystick && saved.joystick.value.bindings['3'] === 'rtl' && saved.joystick.value.hold_ms === 400, 'bindings saved in the Captain database');
await rig('/_rig/press?i=2&down=1'); await until(() => $('buttons').children[2].classList.contains('down'), 'button lights while held');
ok(true, 'pressed button lights up in the pad view');
const recv = async () => rig('/_rig/received');
await sleep(300);
const got = await recv();
ok(got.length === 1 && got[0].cmd.command === 21 && got[0].cmd.source_hint === 'joystick' && got[0].cmd.droneId === '2', 'pressing a bound button sent Land to the selected drone via /command');
await rig('/_rig/press?i=2&down=0');

// 4. voice: unlock through the form
$('tab-btn-joystick').click(); w.document.querySelector('.tab[data-tab="voice"]').click();
await until(() => visible('view-voice'), 'back to voice');
$('uf-password').value = 'wrong'; $('unlock-form').dispatchEvent(new w.Event('submit', { cancelable: true }));
await until(() => $('unlock-err').textContent.includes('wrong password'), 'unlock error');
ok(true, 'wrong password keeps voice locked and says why');
$('uf-password').value = 'pw-op1'; $('unlock-form').dispatchEvent(new w.Event('submit', { cancelable: true }));
await until(() => $('gate-title').textContent === 'Voice control is unlocked', 'unlocked');
ok($('lamp-voice').classList.contains('go') && visible('lock-btn') && !visible('unlock-form'), 'unlock lights the voice lamp and shows Lock now');
// speech engine is not in jsdom: the UI must say so rather than fail silently
$('listen-btn').click(); await sleep(100);
ok($('listen-note').textContent.includes('not available'), 'Start listening explains when the browser has no speech engine');
// a transcript posted by an engine shows up in the log and a motor command asks for confirmation
await fetch(base + '/api/voice/transcript', { method: 'POST', headers: { 'Content-Type': 'application/json', Cookie: `fleet_agent_k=${token}` }, body: JSON.stringify({ text: 'captain takeoff', final: true }) });
await until(() => visible('confirm-bar'), 'confirm bar');
ok($('heard').children.length === 1 && $('confirm-text').textContent.includes('confirm'), 'spoken takeoff appears in the log and asks to confirm');
$('confirm-yes').click();
await until(async () => true, 'x'); await sleep(400);
const got2 = await recv();
ok(got2.length === 2 && got2[1].cmd.command === 22 && got2[1].cmd.source_hint === 'voice', 'Confirm button sent Takeoff with source_hint voice');
ok(!visible('confirm-bar'), 'confirm bar closes');

// 5. settings tab shows saved copies and saves voice settings
w.document.querySelector('.tab[data-tab="settings"]').click();
await until(() => visible('view-settings') && $('saved-rows').children.length === 3, 'settings rows');
ok($('s-wake').value === 'captain' && $('s-ttl').value === '5', 'settings form shows defaults (wake word, 5 min)');
ok($('saved-rows').children[0].children[1].textContent.startsWith('2') || $('saved-rows').children[0].children[1].textContent.length > 8, 'joystick shows a saved timestamp');
ok(w.document.querySelector('fieldset.factors input[value="face"]').disabled === false && w.document.querySelector('fieldset.factors input[value="voiceprint"]').disabled === true, 'face factor available, voiceprint disabled with the engine missing');
ok($('fx-voice').textContent.includes('install resemblyzer'), 'voiceprint explains what to install');
$('s-wake').value = 'falcon'; $('s-save-voice').click(); await sleep(500);
ok((await rig('/_rig/settings?u=op1')).voice.value.wake_word === 'falcon', 'voice settings saved in the Captain database');

// 6. logging out returns to login and clears state
$('logout').click();
await until(() => visible('view-login'), 'back to login'); await sleep(200);
ok($('link-text').textContent === 'Not connected', 'logout shows Not connected');
ok(errors.length === 0, 'no uncaught errors in the page: ' + errors.join(' | '));
console.log(`\n${n} UI checks passed`);
process.exit(0);
