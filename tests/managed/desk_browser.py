"""Bounded headless Chromium interaction check using local, secret-free fixtures.

Usage: python desk_browser.py CHROMIUM [SCREENSHOT_PATH]
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from html import unescape

root = Path(__file__).resolve().parents[2]
assets = root / 'src'
fixture = r"""
const states = Object.fromEntries(['landscape','portrait'].map(id => [id, {
  phase:'idle', ingest_alive:true, current_delay_ms:0, stats:{bitrate_kbps:6000},
  destinations:['Twitch','YouTube','Kick','Rumble'].map((name,index) => ({
    id:name,name,platform:name.toLowerCase(),enabled:true,alive:index!==2,
    video_res:id==='portrait'?'1080x1920':'1920x1080',bytes_sent:1000,bitrate_kbps:6000,reconnects:index===2?2:0
  }))
}]));
let rejectAction=true, portraitOffline=false;
window.confirm=()=>true;
window.fetch=async(path,options)=>{
  if(path==='/desk/info') return {ok:true,json:async()=>({landscape:{enabled:true,ingestPort:1935},portrait:{enabled:true,ingestPort:1936}})};
  const [, , id, action]=path.split('/');
  if(portraitOffline && id==='portrait') throw new Error('Relay unavailable');
  if(options.method==='POST') {
    if(rejectAction) return {ok:false,status:409,json:async()=>({error:'Fixture rejection'})};
    states[id].phase='preparing';
  }
  for(const d of states[id].destinations) d.bytes_sent+=100;
  return {ok:true,json:async()=>JSON.parse(JSON.stringify(action==='destinations'?states[id].destinations:states[id]))};
};
window.addEventListener('error',event=>{document.documentElement.dataset.error=event.message});
"""
checks = r"""
setTimeout(async()=>{
  const assert=(condition,message)=>{if(!condition) throw new Error(message)};
  try {
    assert(document.querySelectorAll('#destinations tr').length===4,'Missing destinations');
    assert(document.querySelector('#programs').textContent.includes('1080x1920'),'Missing portrait dimensions');
    const button=document.querySelector('[data-action="arm"]');
    button.click(); await new Promise(resolve=>setTimeout(resolve,50));
    assert(document.querySelector('#feedback').textContent.includes('Fixture rejection'),'Rejected action hidden');
    assert(!document.querySelector('#feedback').textContent.includes('accepted'),'False success');
    rejectAction=false; button.click(); await new Promise(resolve=>setTimeout(resolve,50));
    assert(states.landscape.phase==='preparing' && states.portrait.phase==='idle','Action scope crossed programs');
    portraitOffline=true;
    await new Promise(resolve=>setTimeout(resolve,1500));
    document.querySelector('#scope').value='portrait';
    document.querySelector('#scope').dispatchEvent(new Event('change'));
    assert([...document.querySelectorAll('[data-action]')].every(button=>button.disabled),'Offline controls enabled');
    assert(document.querySelector('#attention').textContent.includes('Portrait: Relay unavailable'),'Offline state hidden');
    assert(!document.documentElement.dataset.error,'Browser exception');
    document.documentElement.dataset.test='PASS';
  } catch(error) {document.documentElement.dataset.test=error.message;}
},1500);
"""
with tempfile.TemporaryDirectory(prefix='desk-browser-') as tmp:
    page = Path(tmp) / 'desk.html'
    html = (assets / 'managed-desk.html').read_text().replace(
        '<script src="/desk/app.js"></script>',
        '<script>' + fixture + '</script><script>' + (assets / 'managed-desk.js').read_text()
        + '</script><script>' + checks + '</script>')
    page.write_text(html)
    # Nix builders have HOME=/homeless-shelter. Chromium's crashpad needs a
    # writable HOME/XDG configuration tree even with a separate browser profile.
    environment = os.environ.copy()
    for name, directory in [('HOME', 'home'), ('XDG_CONFIG_HOME', 'config'),
                            ('XDG_CACHE_HOME', 'cache')]:
        location = Path(tmp) / directory
        location.mkdir(mode=0o700)
        environment[name] = str(location)
    command = [sys.argv[1], '--headless', '--no-sandbox', '--disable-gpu',
               '--no-first-run', '--disable-background-networking',
               '--user-data-dir=' + str(Path(tmp) / 'profile'),
               '--window-size=360,900', '--virtual-time-budget=5000', '--dump-dom']
    if len(sys.argv) > 2:
        command.append('--screenshot=' + str(Path(sys.argv[2]).resolve()))
    result = subprocess.run(command + [page.as_uri()], env=environment,
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr[-2000:]
    outcome = re.search(r'data-test="([^"]*)"', result.stdout)
    message = unescape(outcome[1]) if outcome else 'Browser checks did not finish'
    assert message == 'PASS', message + '\n' + result.stdout[:2000]
    print('PASS: Chromium dual-program rendering, rejected actions, scope isolation and offline controls')
