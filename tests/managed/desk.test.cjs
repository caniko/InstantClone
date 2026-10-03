const {test} = require('node:test');
const assert = require('node:assert/strict');
const {fresh, orientation, destinationStatus, destinationRows, programStatus, ingestInstructions} = require(process.env.DESK_JS || '../../src/managed-desk.js');
const now = 10000;
const program = {enabled:true, updated:now, previousUpdated:9000, state:{ingest_alive:true}};
test('offline or stale telemetry never claims sending', () => {
  const dest = {enabled:true, alive:true, bytes_sent:200};
  for (const p of [{...program, error:'offline'}, {...program, updated:4000}, {...program,state:null}]) {
    assert.equal(fresh(p,now),false);
    assert.equal(destinationStatus(p,dest,{bytes_sent:100},now)[0],'Unknown · relay offline');
  }
});
test('duplicate display names preserve each independent destination', () => {
  const dest = {platform:'youtube',name:'YouTube'};
  const rows = destinationRows({
    landscape:{destinations:[{...dest,id:'a'},{...dest,id:'b',enabled:false}]},
    portrait:{destinations:[{...dest,id:'c'}]},
  });
  assert.equal(rows.length,3);
  assert.deepEqual(rows.flatMap(row => ['landscape','portrait'].flatMap(id => row[id] ? [row[id].id] : [])), ['a','b','c']);
  assert.equal(rows[1].landscape.enabled,false);
});
test('unique provider labels pair across programs without treating names as IDs', () => {
  const dest = {platform:'custom',name:'Rumble'};
  const rows = destinationRows({landscape:{destinations:[{...dest,id:'h'}]},portrait:{destinations:[{...dest,id:'v'}]}});
  assert.equal(rows.length,1);
  assert.equal(rows[0].landscape.id,'h');
  assert.equal(rows[0].portrait.id,'v');
});
test('connection is distinct from observed media delivery', () => {
  const dest = {enabled:true, alive:true, bytes_sent:200};
  assert.equal(destinationStatus(program,dest,null,now)[0],'Connected · no recent media');
  assert.equal(destinationStatus(program,dest,{bytes_sent:200},now)[0],'Connected · no recent media');
  assert.equal(destinationStatus(program,dest,{bytes_sent:100},now)[0],'Sending');
  assert.equal(destinationStatus(program,{...dest,alive:false},null,now)[0],'Disconnected · retrying');
  assert.equal(destinationStatus({...program,state:{ingest_alive:false}},dest,null,now)[0],'Waiting for program');
});
test('dimensions identify portrait independently of primary-track routing', () => {
  assert.equal(orientation('1080x1920','portrait'),true);
  assert.equal(orientation('1920x1080','portrait'),false);
  assert.equal(orientation('1920x1080','landscape'),true);
  assert.equal(orientation('','portrait'),null);
});
test('credential errors remain visible and distinct from offline telemetry', () => {
  const p = {...program,error:'Program authentication required; check its control credential.'};
  assert.equal(programStatus(p,now),'Authentication required');
  assert.equal(destinationStatus(p,{enabled:true},null,now)[0],'Unknown · authentication required');
  assert.equal(programStatus({...program,error:'Relay unavailable; check its user service.'},now),'Offline');
});
test('setup never invents a provisioned ingest key', () => {
  assert.match(ingestInstructions({ingest_key_set:true}),/provisioned ingest key/);
  assert.match(ingestInstructions({ingest_key_set:false}),/any non-empty/);
  assert.match(ingestInstructions(null),/unknown/);
});
