// Real Chromium + real relay HTTP. No fetch stubs or credential reads from JS.
const assert = require('node:assert/strict');
const {writeFileSync}=require('node:fs');
const [endpoint,base,token,screenshot,peerFile,peerToken] = process.argv.slice(2);
const socket = new WebSocket(endpoint);
let sequence=0, sessionId;
const pending=new Map();
const exceptions=[], responses=[];
socket.addEventListener('message',event=>{
  const message=JSON.parse(event.data);
  if (message.method==='Runtime.exceptionThrown') exceptions.push(message.params.exceptionDetails.text);
  if (message.method==='Network.responseReceived') {
    const {url,status}=message.params.response;
    if (url.startsWith(base+'/desk/')) responses.push({path:new URL(url).pathname,status});
  }
  if (!pending.has(message.id)) return;
  const {resolve,reject}=pending.get(message.id); pending.delete(message.id);
  if (message.error) reject(new Error(message.error.message)); else resolve(message.result);
});
function command(method,params={},session=sessionId) {
  return new Promise((resolve,reject)=>{
    const id=++sequence; pending.set(id,{resolve,reject});
    socket.send(JSON.stringify({id,method,params,...(session?{sessionId:session}:{})}));
  });
}
async function evaluate(expression) {
  const response=await command('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});
  if (response.exceptionDetails) throw new Error(response.exceptionDetails.text);
  return response.result.value;
}
async function until(expression) {
  const end=Date.now()+12000;
  while (Date.now()<end) {
    if (await evaluate(expression)) return;
    await new Promise(resolve=>setTimeout(resolve,100));
  }
  throw new Error('Browser condition timed out: '+expression);
}
async function run() {
  await new Promise((resolve,reject)=>{socket.addEventListener('open',resolve,{once:true});socket.addEventListener('error',reject,{once:true});});
  const {targetId}=await command('Target.createTarget',{url:'about:blank'},null);
  ({sessionId}=await command('Target.attachToTarget',{targetId,flatten:true},null));
  await command('Page.enable');
  await command('Runtime.enable');
  await command('Network.enable');
  await command('Page.navigate',{url:base+'/login'});
  await until("!!document.getElementById('pw')");
  await evaluate("document.getElementById('pw').value='disposable-login'; document.querySelector('form').requestSubmit(); true");
  await until("document.querySelector('h1')?.textContent==='Broadcast Desk' && document.querySelectorAll('#destinations tr').length===4");
  assert.ok(responses.some(r=>r.path==='/desk/app.js' && r.status===200));
  assert.equal(await evaluate("fetch('/logout',{method:'POST',body:''}).then(r=>r.status)"),200);
  assert.equal(await evaluate("fetch('/desk/info').then(r=>r.status)"),401);
  responses.length=0;
  await command('Page.navigate',{url:base+'/dock?token='+token});
  await until("document.querySelectorAll('#destinations tr').length===4 && [...document.querySelectorAll('#programs .status')].every(e=>e.textContent==='Receiving')");
  await evaluate("document.getElementById('scope').value='portrait'; document.getElementById('scope').dispatchEvent(new Event('change')); document.getElementById('seconds').value=5; document.querySelector('[data-action=arm]').click(); true");
  await until("document.getElementById('feedback').textContent.includes('accepted')");
  assert.equal(await evaluate("fetch('/desk/portrait/state').then(r=>r.json()).then(s=>s.armed_delay_ms)"),5000);
  assert.equal(await evaluate("fetch('/desk/landscape/state').then(r=>r.json()).then(s=>s.armed_delay_ms)"),0);
  assert.ok(responses.some(r=>r.path==='/desk/portrait/arm' && r.status===200));
  assert.ok(responses.every(r=>r.status===200),'Dock loaded a failed desk response');
  assert.deepEqual(exceptions,[],'Unhandled browser exceptions');
  assert.match(await evaluate("document.getElementById('setup').textContent"),/provisioned ingest key/);
  assert.equal(await evaluate("fetch('/desk/portrait/state').then(r=>r.json()).then(s=>s.ingest_key_set)"),true);
  writeFileSync(peerFile,'0'.repeat(peerToken.length),{mode:0o600});
  await until("document.getElementById('attention').textContent.includes('authentication required') && [...document.querySelectorAll('#programs .status')].some(e=>e.textContent==='Authentication required')");
  assert.equal(await evaluate("document.querySelector('[data-action=arm]').disabled"),true);
  writeFileSync(peerFile,peerToken,{mode:0o600});
  await until("[...document.querySelectorAll('#programs .status')].every(e=>e.textContent==='Receiving')");
  if (screenshot) {
    const {data}=await command('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});
    writeFileSync(screenshot,Buffer.from(data,'base64'));
  }
  assert.equal(await evaluate("fetch('/config',{method:'POST',body:''}).then(r=>r.status)"),403);
  await command('Target.closeTarget',{targetId});
  console.log('PASS: real-server Chromium login, dock-token asset loading, duplicate destinations and portrait-only action');
}
const deadline=setTimeout(()=>{console.error('Browser test deadline exceeded');process.exit(1);},40000);
run().then(()=>{clearTimeout(deadline);socket.close();}).catch(error=>{clearTimeout(deadline);console.error(error.message);socket.close();process.exitCode=1;});
