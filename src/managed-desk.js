'use strict';
const PROGRAMS = ['landscape', 'portrait'];
const label = id => id === 'landscape' ? 'Landscape' : 'Portrait';
const FRESH_MS = 5000;
function fresh(program, now) { return !!program.state && !program.error && now - program.updated < FRESH_MS; }
function orientation(res, id) {
  const match = /^(\d+)x(\d+)$/.exec(res || '');
  return match ? (id === 'portrait' ? +match[2] > +match[1] : +match[1] > +match[2]) : null;
}
function authError(program) { return /authenticat|credential/i.test(program.error || ''); }
function programStatus(program, now) {
  if (!program.enabled) return 'Not configured';
  if (authError(program)) return 'Authentication required';
  if (!fresh(program,now)) return 'Offline';
  return program.state.ingest_alive ? 'Receiving' : 'No program';
}
function ingestInstructions(state) {
  if (!state || typeof state.ingest_key_set !== 'boolean') return 'ingest authentication unknown; refresh telemetry before configuring OBS';
  return state.ingest_key_set ? 'use this program’s provisioned ingest key (not displayed)' : 'local stream key: any non-empty value (ingest authentication is disabled)';
}
function destinationStatus(program, destination, previous, now) {
  if (!program.enabled) return ['Not configured', 'muted'];
  if (!fresh(program, now)) return [authError(program) ? 'Unknown · authentication required' : 'Unknown · relay offline', 'bad'];
  if (!destination) return ['Not configured', 'muted'];
  if (!destination.enabled) return ['Disabled', 'muted'];
  if (!program.state.ingest_alive) return ['Waiting for program', 'warn'];
  if (!destination.alive) return ['Disconnected · retrying', 'warn'];
  if (previous && program.updated > program.previousUpdated && destination.bytes_sent > previous.bytes_sent) return ['Sending', 'good'];
  return ['Connected · no recent media', 'warn'];
}
function destinationRows(programs) {
  const groups = new Map(), rows = [];
  for (const id of PROGRAMS) for (const d of programs[id].destinations) {
    const key = JSON.stringify([d.platform,d.name]);
    if (!groups.has(key)) groups.set(key,{name:d.name,platform:d.platform,landscape:[],portrait:[]});
    groups.get(key)[id].push(d);
  }
  for (const group of groups.values()) {
    if (PROGRAMS.every(id=>group[id].length<=1)) {
      rows.push({name:group.name,platform:group.platform,landscape:group.landscape[0],portrait:group.portrait[0]});
    } else {
      // Ambiguous labels never merge: retain each program's stable ID.
      for (const id of PROGRAMS) for (const d of group[id]) rows.push({name:`${d.name} · ${d.id}`,platform:d.platform,[id]:d});
    }
  }
  return rows;
}
if (typeof module !== 'undefined') module.exports = {fresh, orientation, destinationStatus, destinationRows, programStatus, ingestInstructions};
if (typeof document !== 'undefined') {
  const $ = id => document.getElementById(id);
  const programs = Object.fromEntries(PROGRAMS.map(id => [id, {enabled:false, state:null, updated:0, destinations:[]} ]));
  let info = null, busy = false;
  function node(tag, text, className) { const el = document.createElement(tag); el.textContent = text; if (className) el.className = className; return el; }
  async function request(path, body) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 4000);
    try {
      const response = await fetch(path, {method:body === undefined ? 'GET' : 'POST', body, signal:controller.signal, cache:'no-store', headers:body === undefined ? {} : {'Content-Type':'application/x-www-form-urlencoded'}});
      const data = await response.json();
      if (!response.ok || data.ok === false) throw new Error(data.error || `Request rejected (${response.status})`);
      return data;
    } finally { clearTimeout(timeout); }
  }
  function render() {
    const now = Date.now(), alerts = [], rows = destinationRows(programs);
    $('programs').replaceChildren();
    for (const id of PROGRAMS) {
      const p = programs[id], live = fresh(p, now), card = node('section', '', 'program');
      const head = node('div', '', 'program-head'); head.append(node('h3',label(id)));
      head.append(node('span', !info ? 'Unknown' : programStatus(p,now), live && p.state.ingest_alive ? 'status good' : 'status warn')); card.append(head);
      const resolutions = [...new Set((p.state?.destinations || []).map(d => d.video_res).filter(Boolean))];
      card.append(node('p', live ? `${resolutions.join(' · ') || 'Dimensions unknown'} · ${((p.state.stats?.bitrate_kbps || 0)/1000).toFixed(1)} Mb/s` : p.updated ? `Last response ${Math.floor((now-p.updated)/1000)} seconds ago` : 'No telemetry', 'muted'));
      $('programs').append(card);
      if (info && p.enabled && !live) alerts.push(`${label(id)}: ${p.error || 'Telemetry expired; refresh or inspect its user service.'} Last state is not current.`);
      if (live && !p.state.ingest_alive) alerts.push(`${label(id)}: no program received. Check this program’s OBS output.`);
      if (live && resolutions.some(res => orientation(res,id) === false)) alerts.push(`${label(id)}: unexpected dimensions (${resolutions.join(', ')}). Check the OBS canvas and output.`);
      if (live && p.state.backpressure) alerts.push(`${label(id)}: relay backpressure. Check upload capacity and delay buffer.`);
    }
    $('destinations').replaceChildren();
    for (const row of rows) {
      const tr = document.createElement('tr'), name = node('th',row.name); name.scope='row';
      name.append(node('small', row.platform === 'custom' ? 'Custom provider' : row.platform)); tr.append(name);
      for (const id of PROGRAMS) {
        const p=programs[id], configured=row[id], d=p.state?.destinations?.find(x=>x.id===configured?.id);
        const previous=p.previous?.destinations?.find(x=>x.id===configured?.id);
        const [text,tone]=destinationStatus(p,d,previous,now), td=node('td',text,tone);
        if (d && fresh(p,now)) td.append(node('small',`${((d.bitrate_kbps || 0)/1000).toFixed(1)} Mb/s · ${d.reconnects || 0} reconnects`));
        tr.append(td);
        if (configured && tone === 'warn') alerts.push(`${label(id)} / ${row.name}: ${text.toLowerCase()}.`);
      }
      $('destinations').append(tr);
    }
    if (!rows.length) { const tr=document.createElement('tr'), td=node('td','No destinations available. Open setup & diagnostics.','muted'); td.colSpan=3; tr.append(td); $('destinations').append(tr); }
    $('attention').replaceChildren(...alerts.map(text=>node('p',text,'notice')));
    $('summary').textContent = !info ? 'Desk configuration unavailable · retrying' : alerts.length ? `${alerts.length} items need attention` : 'Local relay telemetry current';
    if (info) PROGRAMS.forEach((id,index)=>{
      const text=`${label(id)}: ${programs[id].enabled ? `OBS custom output rtmp://127.0.0.1:${info[id].ingestPort}/live · ${ingestInstructions(fresh(programs[id],now) ? programs[id].state : null)}` : 'relay not enabled'}`;
      const line=$('setup').children[index];
      if (!line) $('setup').append(node('p',text));
      else if (line.textContent !== text) line.textContent=text;
    });
    const p=programs[$('scope').value], live=fresh(p,now), phase=p.state?.phase;
    $('delay').textContent=live ? `${label($('scope').value)} · ${(p.state.current_delay_ms/1000).toFixed(1)} s behind real time · ${phase}` : 'Current state unavailable · controls paused';
    if (live && p.state.safe_cut_pending) $('delay').textContent+=` · returning to real time in approximately ${Math.ceil(p.state.safe_cut_remaining_ms/1000)} s`;
    for (const button of document.querySelectorAll('[data-action]')) {
      const action=button.dataset.action;
      button.hidden = !live || (action==='arm' && phase!=='idle') || (action==='activate' && phase!=='ready') || (action==='disarm' && !['ready','preparing'].includes(phase)) || (['stop','cut-after'].includes(action) && phase!=='active');
      if (action==='cancel-cut') button.hidden=!live || !p.state.safe_cut_pending;
      if (action==='cut-after' && p.state?.safe_cut_pending) button.hidden=true;
      button.disabled = busy || !live || (['arm','activate','cut-after'].includes(action) && !p.state.ingest_alive) || (action==='activate' && phase!=='ready') || (action==='disarm' && !['ready','preparing'].includes(phase)) || (['stop','cut-after'].includes(action) && phase!=='active') || (action==='arm' && phase!=='idle');
    }
  }
  async function poll() {
    try {
      const nextInfo=await request('/desk/info');
      if (PROGRAMS.some(id=>!nextInfo[id] || typeof nextInfo[id].enabled!=='boolean')) throw new Error('Invalid desk configuration');
      for (const id of PROGRAMS) {
        const p=programs[id];
        if (JSON.stringify(info?.[id])!==JSON.stringify(nextInfo[id])) {
          p.version=(p.version || 0)+1;
          p.state=null; p.previous=null; p.destinations=[];
          p.updated=0; p.previousUpdated=0; p.error=null;
        }
        p.enabled=nextInfo[id].enabled;
      }
      info=nextInfo;
      await Promise.all(PROGRAMS.filter(id=>programs[id].enabled).map(async id=>{
        const p=programs[id], version=p.version;
        try {
          const [state,destinations]=await Promise.all([request(`/desk/${id}/state`),request(`/desk/${id}/destinations`)]);
          if (busy || version !== p.version) return;
          if (!Array.isArray(destinations) || !Array.isArray(state.destinations)) throw new Error('Invalid relay telemetry');
          p.previous=p.error ? null : p.state; p.previousUpdated=p.updated;
          p.state=state; p.destinations=destinations; p.updated=Date.now(); p.error=null;
        } catch(error) { if (!busy && version === p.version) p.error=error.message; }
      }));
    } catch(error) { $('feedback').textContent=error.message; }
    render(); setTimeout(poll,1000);
  }
  $('scope').addEventListener('change',render);
  for (const button of document.querySelectorAll('[data-action]')) button.addEventListener('click',async()=>{
    if (button.disabled || busy) return;
    const id=$('scope').value, action=button.dataset.action;
    if (action==='stop' && !confirm(`Return ${label(id)} to real time now? Buffered footage will be skipped. The other program is unaffected.`)) return;
    const seconds=Number($('seconds').value);
    if (action==='arm' && (!Number.isInteger(seconds) || seconds<1 || seconds>600)) { $('feedback').textContent='Enter a whole number from 1 to 600 seconds.'; return; }
    programs[id].version=(programs[id].version || 0)+1;
    busy=true; render(); $('feedback').textContent=`${label(id)}: requesting ${button.textContent.toLowerCase()}…`;
    try {
      const state=await request(`/desk/${id}/${action}`,action==='arm' ? `ms=${seconds*1000}` : '');
      if (typeof state.phase!=='string') throw new Error('Action response had no observed state. Refresh before retrying.');
      programs[id].previous=null; programs[id].state=state; programs[id].updated=Date.now(); programs[id].error=null;
      $('feedback').textContent=`${label(id)}: relay accepted the action · observed state: ${state.phase}.`;
    } catch(error) { $('feedback').textContent=`${label(id)}: ${error.message} Outcome may be unknown; inspect refreshed state before retrying.`; }
    finally { busy=false; render(); }
  });
  if (location.pathname==='/') $('diagnostics').open=true;
  render(); poll(); setInterval(render,1000);
}
