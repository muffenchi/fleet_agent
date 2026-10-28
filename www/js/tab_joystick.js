const $ = (id) => document.getElementById(id);
const R = 52;

export function initJoystick({ api, state, bus, toast, watchJoystick }) {
  let pad = null, draft = {}, cells = [], builtFor = -1, holdTimer = 0;

  const js         = () => state.status.settings.joystick;
  const saveBtn    = $('js-save');
  const savedSpan  = $('js-saved');
  const modeSpan   = $('js-mode');
  const dpad       = $('dpad');

  /* dropdown options come from status.actions + status.pseudo_actions */
  function buildOptions(sel) {
    sel.add(new Option('— nothing —', ''));
    const real = Object.entries(state.status.actions || {});
    if (real.length) {
      const gReal = document.createElement('optgroup');
      gReal.label = 'Drone commands';
      real.forEach(([name, a]) =>
        gReal.appendChild(new Option(a.label + (a.confirm ? ' (hold)' : ''), name)));
      sel.appendChild(gReal);
    }
    const pseudo = Object.entries(state.status.pseudo_actions || {});
    if (pseudo.length) {
      const gUi = document.createElement('optgroup');
      gUi.label = 'UI / navigation';
      pseudo.forEach(([name, a]) => gUi.appendChild(new Option(a.label, name)));
      sel.appendChild(gUi);
    }
  }

  function buildButtons(n) {
    const box = $('buttons');
    box.replaceChildren();
    cells = [];
    for (let i = 0; i < n; i++) {
      const cell = document.createElement('div'); cell.className = 'btn-cell';
      const top = document.createElement('div'); top.className = 'top';
      top.textContent = `Button ${i}`;
      const sel = document.createElement('select');
      sel.setAttribute('aria-label', `Action for button ${i}`);
      buildOptions(sel);
      sel.value = draft[String(i)] || '';
      sel.addEventListener('change', () => {
        if (sel.value) draft[String(i)] = sel.value;
        else delete draft[String(i)];
      });
      cell.append(top, sel); box.appendChild(cell); cells.push({ cell, sel });
    }
    builtFor = n;
  }

  function moveStick(id, x, y) {
    const k = $(id).querySelector('.knob');
    const m = Math.hypot(x, y) || 1, f = Math.min(1, m) / m;
    k.setAttribute('cx', String(x * f * R));
    k.setAttribute('cy', String(y * f * R));
  }

  function setDpad(x, y) {
    if (!dpad) return;
    dpad.querySelectorAll('.dp-up, .dp-down, .dp-left, .dp-right')
        .forEach(el => el.classList.remove('on'));
    if (y >  0) dpad.querySelector('.dp-up').classList.add('on');
    if (y <  0) dpad.querySelector('.dp-down').classList.add('on');
    if (x <  0) dpad.querySelector('.dp-left').classList.add('on');
    if (x >  0) dpad.querySelector('.dp-right').classList.add('on');
  }

  function renderMode() {
    const nav = !!(state.status && state.status.joystick && state.status.joystick.nav_mode);
    if (modeSpan) modeSpan.textContent = nav
      ? 'Navigation ON — hat pans map'
      : 'Navigation off — hat does its bound action';
    const icon = $('nav-mode-status');
    if (icon) {
      icon.querySelector('path').setAttribute('stroke', nav ? '#00FF00' : '#666666');
      icon.title = nav ? 'Joystick navigation: ON' : 'Joystick navigation: off';
    }
  }

  function renderStatic() {
    const s = state.status, info = s.joystick;
    renderMode();
    $('pad-name').textContent = info.present
      ? `${info.name}${info.count > 1 ? ` (+${info.count - 1} more)` : ''}`
      : info.error ? `Joystick support is not available: ${info.error}`
                   : 'No joystick detected. Plug one in and it appears here.';
    const nBtn = pad ? pad.buttons.length : Math.max(builtFor, 0);
    if (nBtn && nBtn !== builtFor) buildButtons(nBtn);
    document.querySelectorAll('#pad .dz')
        .forEach((c) => c.setAttribute('r', String(R * (js().deadzone ?? 0.15))));
    if (savedSpan) savedSpan.textContent = s.saved_ts.joystick
      ? `Saved ${new Date(s.saved_ts.joystick / 1000).toLocaleString()}`
      : 'Not saved yet — using defaults';
  }

  /* ── live events ─────────────────────────────────────────────────── */
  bus.on('joystick.state', (ev) => {
    pad = ev.pads[0] || null;
    if (!pad) return;
    if (pad.buttons.length !== builtFor) {
      draft = { ...js().bindings };
      buildButtons(pad.buttons.length);
    }
    const a = pad.axes;
    moveStick('stick-l', a[0] || 0, a[1] || 0);
    moveStick('stick-r', a[2] || 0, a[3] || 0);
    pad.buttons.forEach((down, i) => cells[i] && cells[i].cell.classList.toggle('down', down));
    const h = pad.hats && pad.hats[0];
    if (h) setDpad(Number(h[0]) || 0, Number(h[1]) || 0);
  });

  bus.on('joystick.hold', (ev) => {
    const wrap = $('hold-wrap'), fill = $('hold-fill');
    if (ev.state === 'start') {
      wrap.classList.remove('hidden');
      $('hold-label').textContent =
        `Hold to ${(state.status.actions[ev.action] || {}).label || ev.action}…`;
      fill.style.transition = 'none'; fill.style.width = '0';
      void fill.offsetWidth;
      fill.style.transition = `width ${ev.ms}ms linear`; fill.style.width = '100%';
    } else {
      clearTimeout(holdTimer);
      holdTimer = setTimeout(() => wrap.classList.add('hidden'),
                             ev.state === 'fired' ? 400 : 0);
    }
  });

  bus.on('joystick.nav_mode', (ev) => {
    if (state.status && state.status.joystick) state.status.joystick.nav_mode = ev.on;
    renderMode();
  });
  bus.on('joystick.nav_fired', (ev) => {
    const icon = $('nav-mode-status');
    if (!icon) return;
    const path = icon.querySelector('path');
    const prev = path.getAttribute('fill') || '#666666';
    path.setAttribute('fill', '#7fd1ff');
    setTimeout(() => path.setAttribute('fill', prev), 160);
  });
  bus.on('joystick.error', (ev) => toast(ev.detail, 'bad'));

  /* ── Save button ─────────────────────────────────────────────────── */
  if (saveBtn) {
    saveBtn.addEventListener('click', async () => {
      try {
        await api('PUT', '/api/settings/joystick', { value: {
          bindings: draft,
          hold_ms:  js().hold_ms,             // kept at the default; not user-editable
          deadzone: js().deadzone,
        }});
        toast('Joystick settings saved to Captain', 'good');
      } catch (e) { toast(e.message, 'bad'); }
    });
  }

  bus.on('settings', () => { draft = { ...js().bindings }; builtFor = -1; renderStatic(); });
  bus.on('status',   () => renderMode());

  return {
    onShow() { draft = { ...js().bindings }; builtFor = -1; renderStatic(); watchJoystick(true); },
    onHide() { watchJoystick(false); },
  };
}