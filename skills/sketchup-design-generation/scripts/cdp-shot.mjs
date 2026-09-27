// cdp-shot.mjs — take a screenshot of a local CDP page target
// usage: node cdp-shot.mjs <outfile.png> [urlSubstring] [width] [height]   (env CDP_TARGET=<id> overrides target)
const outfile = process.argv[2];
const urlSub = process.argv[3] !== undefined ? process.argv[3] : 'app.sketchup.com';
const W = parseInt(process.argv[4] || '1440', 10);
const H = parseInt(process.argv[5] || '900', 10);
const fs = await import('node:fs');

const list = await fetch('http://127.0.0.1:9222/json/list').then(r => r.json());
const pages = list.filter(t => t.type === 'page' && t.url.startsWith('http'));
let target = null;
if (process.env.CDP_TARGET) target = pages.find(p => p.id === process.env.CDP_TARGET);
if (!target) target = pages.find(p => p.url.includes(urlSub));
if (!target) { console.error('NO_TARGET'); process.exit(2); }

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

// activate this tab — headless multi-tab sessions otherwise leave the page unfocused
try { await send('Page.bringToFront'); } catch (e) {}
try { await send('Target.activateTarget', { targetId: target.id }); } catch (e) {}
await new Promise(r => setTimeout(r, 200));

if (W > 0 && H > 0) {
  await send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: 1, mobile: false });
  await new Promise(r => setTimeout(r, 300));
}
const r = await send('Page.captureScreenshot', { format: 'png' });
fs.writeFileSync(outfile, Buffer.from(r.data, 'base64'));
console.log(`SAVED ${outfile} bytes=${fs.statSync(outfile).size} url=${target.url}`);
sock.close();
