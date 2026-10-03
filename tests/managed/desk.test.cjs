const {test} = require('node:test');
const assert = require('node:assert/strict');
const {fresh, orientation, destinationStatus} = require(process.env.DESK_JS || '../../src/managed-desk.js');
const now = 10000;
const program = {enabled:true, updated:now, previousUpdated:9000, state:{ingest_alive:true}};
test('offline or stale telemetry never claims sending', () => {
  const dest = {enabled:true, alive:true, bytes_sent:200};
  for (const p of [{...program, error:'offline'}, {...program, updated:4000}, {...program,state:null}]) {
    assert.equal(fresh(p,now),false);
    assert.equal(destinationStatus(p,dest,{bytes_sent:100},now)[0],'Unknown · relay offline');
  }
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
