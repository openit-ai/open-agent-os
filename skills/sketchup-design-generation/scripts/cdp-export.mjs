// cdp-export.mjs — SketchUp for Web: menu → Download → SKP|PNG|STL, captured to a local folder
// usage: node cdp-export.mjs <SKP|PNG|STL> [downloadDir]     (default ./downloads)
// env: CDP_URL (default http://127.0.0.1:9222), CDP_TARGET=<tabId> pins the exact tab
//
// Why one connection: Browser.setDownloadBehavior applies only while the CDP client that set
// it stays connected. This script keeps one connection open across (set behavior → clicks), so
// the file lands in downloadDir. A later download from another connection goes to the browser's
// default download directory.
import fs from 'node:fs';
import path from 'node:path';

const FORMAT = (process.argv[2] || 'SKP').toUpperCase();
const DL = path.resolve(process.argv[3] || process.env.CDP_DL || './downloads');
fs.mkdirSync(DL, { recursive: true });
const CDP_URL = process.env.CDP_URL || 'http://127.0.0.1:9222';

const version = await fetch(CDP_URL + '/json/version').then(r => r.json());
const list = await fetch(CDP_URL + '/json/list').then(r => r.json());
let target = null;
if (process.env.CDP_TARGET) target = list.find(t => t.id === process.env.CDP_TARGET);
if (!target) target = list.find(t => t.type === 'page' && t.url.includes('app.sketchup.com'));
if (!target) { console.error('NO_TARGET'); process.exit(2); }

function makeConn(url) {
  const sock = new WebSocket(url);
  let id = 0; const evs = [];
  function send(method, params = {}) {
    return new Promise((res, rej) => {
      const mid = ++id;
      const onmsg = (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.id === mid) { sock.removeEventListener('message', onmsg); msg.error ? rej(new Error(JSON.stringify(msg.error))) : res(msg.result); }
      };
      sock.addEventListener('message', onmsg);
      sock.send(JSON.stringify({ id: mid, method, params }));
    });
  }
  const opened = new Promise((res, rej) => { sock.addEventListener('open', res); sock.addEventListener('error', rej); });
  sock.addEventListener('message', (ev) => { const m = JSON.parse(ev.data); if (m.method) evs.push(m); });
  return { sock, send, evs, opened };
}

const B = makeConn(version.webSocketDebuggerUrl);      // browser-level (downloads)
const P = makeConn(target.webSocketDebuggerUrl);       // page-level (clicks, DOM)
await B.opened; await P.opened;

await B.send('Browser.setDownloadBehavior', { behavior: 'allow', downloadPath: DL, eventsEnabled: true });
await P.send('Page.bringToFront').catch(() => {});
await P.send('Network.enable');
await P.send('Page.enable');
await P.send('Runtime.enable');
await P.send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });

const sleep = (ms) => new Promise(r => setTimeout(r, ms));
async function click(x, y) {
  await P.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y, button: 'none' });
  await P.send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1, buttons: 1 });
  await sleep(50);
  await P.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1, buttons: 0 });
}
async function evalIn(expr) {
  const r = await P.send('Runtime.evaluate', { expression: `(() => { try { return JSON.stringify({ok:true, v: (${expr})}); } catch(e) { return JSON.stringify({ok:false, e: String(e)}); } })()`, returnByValue: true });
  return r.result.value;
}
async function rectOf(expr) {
  const out = JSON.parse(await evalIn(`(() => { const e = ${expr}; if (!e) return null; const r = e.getBoundingClientRect(); return [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)]; })()`));
  return out.ok ? out.v : null;
}

// --- open menu → Download → <FORMAT> ---
await click(22, 22); await sleep(800);
if (!JSON.parse(await evalIn('/Download/.test(document.body.innerText)')).v) {
  console.error('MENU_DID_NOT_OPEN — close any overlay/banner first (e.g. promo banner close at 1415,29)');
  process.exit(3);
}
await click(95, 356); await sleep(800);                              // Download item
const itemRect = await rectOf(`[...document.querySelectorAll('li')].find(e => (e.innerText||'').trim() === '${FORMAT}')`);
if (!itemRect) { console.error('FORMAT_ITEM_NOT_FOUND ' + FORMAT); process.exit(4); }
await click(Math.round(itemRect[0] + itemRect[2] / 2), Math.round(itemRect[1] + itemRect[3] / 2));
await sleep(3500);

// PNG opens the EXPORT IMAGE dialog — confirm it in this same connection
if (FORMAT === 'PNG') {
  const btn = await rectOf(`[...document.querySelectorAll('button')].find(x => /Export as PNG/.test(x.innerText||''))`);
  if (btn) {
    await click(Math.round(btn[0] + btn[2] / 2), Math.round(btn[1] + btn[3] / 2));
    await sleep(9000);
  }
} else {
  await sleep(5000);
}

const dlEvents = B.evs.filter(e => /download/i.test(e.method)).map(e => ({ m: e.method, p: e.params }));
const shot = await P.send('Page.captureScreenshot', { format: 'png' });
fs.writeFileSync('export_after_' + FORMAT + '.png', Buffer.from(shot.data, 'base64'));
console.log(JSON.stringify({
  format: FORMAT,
  downloadDir: DL,
  files: fs.readdirSync(DL),
  downloadEvents: dlEvents,
  screenshot: 'export_after_' + FORMAT + '.png'
}, null, 1));
B.sock.close(); P.sock.close();
