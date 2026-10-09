// cdp-eval.mjs — evaluate JS in a local CDP page target (consistent 1440x900 viewport)
// usage: node cdp-eval.mjs '<js expression>' [urlSub]   (env CDP_TARGET=<id> overrides target)
// The expression is wrapped: result = (expr). JSON-stringified back to stdout.
const expr = process.argv[2];
const urlSub = process.argv[3] || 'app.sketchup.com';

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

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// activate this tab — headless multi-tab sessions otherwise leave the page unfocused
try { await send('Page.bringToFront'); } catch (e) {}
try { await send('Target.activateTarget', { targetId: target.id }); } catch (e) {}
await sleep(200);

// consistent viewport for all coordinate work
await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
await sleep(250);

const r = await send('Runtime.evaluate', {
  expression: `(() => { try { const v = (${expr}); return JSON.stringify({ok:true, v}); } catch(e) { return JSON.stringify({ok:false, e: String(e)}); } })()`,
  returnByValue: true, awaitPromise: true
});
console.log(r.result.value);
sock.close();
