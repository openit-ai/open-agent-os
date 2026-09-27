// cdp-act.mjs — trusted input actions on a local CDP page target
// usage:
//   node cdp-act.mjs click <x> <y>
//   node cdp-act.mjs dblclick <x> <y>
//   node cdp-act.mjs move <x> <y>
//   node cdp-act.mjs drag <x1> <y1> <x2> <y2> [steps]
//   node cdp-act.mjs key <KeyName>            e.g. key r | key Enter | key Escape
//   node cdp-act.mjs type <text>
//   node cdp-act.mjs wheel <x> <y> <deltaY>
// env: CDP_URL (default http://127.0.0.1:9222), CDP_TARGET=<tabId> pins the exact tab
const args = process.argv.slice(2);
const cmd = args[0];
const CDP_URL = process.env.CDP_URL || 'http://127.0.0.1:9222';

const list = await fetch(CDP_URL + '/json/list').then(r => r.json());
const pages = list.filter(t => t.type === 'page' && t.url.startsWith('http'));
let target = null;
if (process.env.CDP_TARGET) target = pages.find(p => p.id === process.env.CDP_TARGET);
if (!target) target = pages.find(p => p.url.includes('app.sketchup.com')) || pages[0];
if (!target) { console.error('NO_TARGET'); process.exit(2); }

function num(v) { return parseFloat(v); }
const punctMap = { ',': ['Comma', 188], '.': ['Period', 190], ';': ['Semicolon', 186], '-': ['Minus', 189], '/': ['Slash', 191], '=': ['Equal', 187], "'": ['Quote', 222], '[': ['BracketLeft', 219], ']': ['BracketRight', 221] };
function charSpec(ch) {
  if (/[a-z]/i.test(ch)) return { key: ch, code: 'Key' + ch.toUpperCase(), vk: ch.toUpperCase().charCodeAt(0), text: ch };
  if (/[0-9]/.test(ch)) return { key: ch, code: 'Digit' + ch, vk: 48 + parseInt(ch, 10), text: ch };
  if (punctMap[ch]) return { key: ch, code: punctMap[ch][0], vk: punctMap[ch][1], text: ch };
  return { key: ch, code: 'Char', vk: ch.toUpperCase().charCodeAt(0), text: ch };
}
const keyMap = {
  Enter:  { key: 'Enter', code: 'Enter', vk: 13, text: '\r' },
  Escape: { key: 'Escape', code: 'Escape', vk: 27 },
  Tab:    { key: 'Tab', code: 'Tab', vk: 9 },
  Backspace: { key: 'Backspace', code: 'Backspace', vk: 8 },
  Delete: { key: 'Delete', code: 'Delete', vk: 46 },
  Space:  { key: ' ', code: 'Space', vk: 32, text: ' ' },
  ArrowUp: { key: 'ArrowUp', code: 'ArrowUp', vk: 38 },
  ArrowDown: { key: 'ArrowDown', code: 'ArrowDown', vk: 40 },
  ArrowLeft: { key: 'ArrowLeft', code: 'ArrowLeft', vk: 37 },
  ArrowRight: { key: 'ArrowRight', code: 'ArrowRight', vk: 39 },
};

let actions = [];
if (cmd === 'click' || cmd === 'dblclick') {
  actions.push(['mouse', 'click', num(args[1]), num(args[2]), cmd === 'dblclick' ? 2 : 1]);
} else if (cmd === 'move') {
  actions.push(['mouse', 'move', num(args[1]), num(args[2])]);
} else if (cmd === 'drag') {
  const steps = args[5] && !isNaN(parseInt(args[5])) ? parseInt(args[5]) : 12;
  actions.push(['mouse', 'drag', num(args[1]), num(args[2]), num(args[3]), num(args[4]), steps]);
} else if (cmd === 'key') {
  const name = args[1];
  const spec = keyMap[name] || (name.length === 1 ? charSpec(name) : null);
  if (!spec) { console.error('BAD_KEY', name); process.exit(2); }
  actions.push(['key', spec]);
} else if (cmd === 'type') {
  for (const ch of args[1]) actions.push(['key', keyMap[ch] || charSpec(ch)]);
} else if (cmd === 'wheel') {
  actions.push(['wheel', num(args[1]), num(args[2]), num(args[3])]);
} else { console.error('UNKNOWN_CMD', cmd); process.exit(2); }

// ---- connect ----
const sock = new WebSocket(target.webSocketDebuggerUrl);
let id = 0;
function send(method, params = {}) {
  return new Promise((res, rej) => {
    const mid = ++id;
    const onmsg = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id === mid) {
        sock.removeEventListener('message', onmsg);
        if (msg.error) rej(new Error(JSON.stringify(msg.error))); else res(msg.result);
      }
    };
    sock.addEventListener('message', onmsg);
    sock.send(JSON.stringify({ id: mid, method, params }));
  });
}
await new Promise((res, rej) => { sock.addEventListener('open', res); sock.addEventListener('error', rej); });

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// activate this tab — headless multi-tab: hidden tabs break UI interaction (menus won't open)
try { await send('Page.bringToFront'); } catch (e) {}
try { await send('Target.activateTarget', { targetId: target.id }); } catch (e) {}
await sleep(200);

// consistent viewport for coordinate work
await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
await sleep(250);

for (const a of actions) {
  if (a[0] === 'mouse') {
    const kind = a[1];
    if (kind === 'move') {
      await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: a[2], y: a[3], button: 'none' });
    } else if (kind === 'click') {
      const [x, y, count] = [a[2], a[3], a[4]];
      await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y, button: 'none' });
      for (let i = 1; i <= count; i++) {
        await send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: i, buttons: 1 });
        await sleep(30);
        await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: i, buttons: 0 });
        await sleep(60);
      }
    } else if (kind === 'drag') {
      const [x1, y1, x2, y2, steps] = [a[2], a[3], a[4], a[5], a[6]];
      await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: x1, y: y1, button: 'none' });
      await sleep(80);
      await send('Input.dispatchMouseEvent', { type: 'mousePressed', x: x1, y: y1, button: 'left', clickCount: 1, buttons: 1 });
      await sleep(60);
      for (let s = 1; s <= steps; s++) {
        await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: x1 + (x2 - x1) * s / steps, y: y1 + (y2 - y1) * s / steps, button: 'left', buttons: 1 });
        await sleep(25);
      }
      await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: x2, y: y2, button: 'left', clickCount: 1, buttons: 0 });
    }
  } else if (a[0] === 'key') {
    const spec = a[1];
    const base = { key: spec.key, code: spec.code, windowsVirtualKeyCode: spec.vk, nativeVirtualKeyCode: spec.vk };
    await send('Input.dispatchKeyEvent', { type: 'keyDown', ...base, text: spec.text || '' });
    await sleep(20);
    if (spec.text) {
      await send('Input.dispatchKeyEvent', { type: 'char', ...base, text: spec.text, unmodifiedText: spec.text });
      await sleep(20);
    }
    await send('Input.dispatchKeyEvent', { type: 'keyUp', ...base });
    await sleep(40);
  } else if (a[0] === 'wheel') {
    await send('Input.dispatchMouseEvent', { type: 'mouseWheel', x: a[1], y: a[2], deltaX: 0, deltaY: a[3] });
  }
}
console.log('DONE cmd=' + cmd + ' target=' + target.url.slice(0, 60));
sock.close();
