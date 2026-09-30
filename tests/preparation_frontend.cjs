const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

(async () => {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) {
      const classes = new Map(), attributes = new Map();
      elements.set(id, {hidden:false, disabled:false, textContent:'', classes, attributes,
        classList:{toggle:(key, active)=>classes.set(key, active)},
        setAttribute:(key, value)=>attributes.set(key, value), removeAttribute:key=>attributes.delete(key)});
    }
    return elements.get(id);
  };
  const timers = new Map();
  let nextTimer = 0, token = 0;
  const calls = [];
  let revealed;
  const scope = {window:{BorasukiWait:{render:()=>{}}, BorasukiUI:{reveal:target=>{revealed=target;}}}, document:{getElementById:element},
    setTimeout:callback => { timers.set(++nextTimer, callback); return nextTimer; },
    clearTimeout:id => timers.delete(id)};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../frontend/preparation.js'), 'utf8'), scope);
  let payload = {source:'first.mp4', gpu_id:0, upscale:{scale:2}, denoise:{enabled:true, strength:3}};
  const control = scope.window.BorasukiPreparation({
    values:() => payload, translate:key => key, error:problem => {throw problem;},
    call:async (name, ...args) => { calls.push([name, ...args]); return name === 'request_preparation' ? String(++token) : null; }
  });
  const snapshot = {setup:{status:'ready'}, runtime:{gpus:[{id:0,name:'GPU',driver:'driver'}]}, preparation:null};
  const tick = async () => {
    for (const [id, callback] of timers) { timers.delete(id); callback(); }
    for (let i = 0; i < 8; i++) await Promise.resolve();
  };
  control.render(snapshot);
  assert.equal(control.ready(), false, 'engine preparation blocks until ready');
  assert.equal(element('enginePreparation').hidden, false, 'preparation status remains visible');
  assert.equal(calls.length, 0);
  await tick();
  assert.equal(calls.length, 1);
  snapshot.preparation = {id:'1',status:'preparing',progress:{stage:'engine_building'}};
  control.render(snapshot);
  assert.equal(element('engineStepCheck').classes.get('is-complete'), true);
  assert.equal(element('engineStepGPU').attributes.get('aria-current'), 'step');
  assert.equal(element('engineStepVerify').classes.get('is-complete'), false, 'pending validation must not look completed');
  snapshot.preparation.progress.stage = 'verification';
  control.render(snapshot);
  assert.equal(element('engineStepGPU').classes.get('is-complete'), true);
  assert.equal(element('engineStepGPU').attributes.has('aria-current'), false);
  assert.equal(element('engineStepVerify').attributes.get('aria-current'), 'step');
  snapshot.preparation = {id:'1',status:'ready'};
  element('start').disabled = false;
  assert.equal(control.render(snapshot), true);
  assert.equal(control.ready(), true);
  assert.equal(element('enginePreparation').hidden, true);
  await tick();
  assert.equal(calls.length, 1, 'unchanged settings must not prepare again');
  payload = {...payload, denoise:{enabled:true,strength:2}};
  assert.equal(control.ready(), false, 'changed settings block immediately before polling');
  assert.equal(control.render(snapshot), false, 'old ready response must not enable changed settings');
  await tick();
  assert.deepEqual(calls.map(row => row[0]), ['request_preparation','cancel_preparation','request_preparation']);
  snapshot.preparation = {id:'2',status:'failed',failure:{code:'error.gpu_memory',detail:'test'}};
  control.render(snapshot);
  assert.equal(element('enginePreparationStatus').textContent, 'error.gpu_memory');
  assert.equal(element('enginePreparationRetry').hidden, false);
  assert.equal(element('enginePreparationDetails').hidden, false);
  assert.equal(control.ready(), false);
  element('enginePreparationDetails').onclick();
  assert.equal(element('enginePreparationFailure').open, true);
  assert.equal(revealed, element('enginePreparationTechnical'));
  assert.equal(revealed.textContent, 'test');
  await tick();
  assert.equal(calls.length, 3, 'failure must not loop automatically');
  element('enginePreparationRetry').onclick();
  assert.equal(element('enginePreparationFailure').open, false);
  await tick();
  assert.equal(calls.at(-1)[0], 'request_preparation');
  payload = null;
  control.render(snapshot);
  assert.equal(element('enginePreparation').hidden, true);
  assert.equal(control.ready(), false, 'missing source is never ready');
  console.log('Preparation UI contract passed');
})().catch(problem => { console.error(problem); process.exitCode = 1; });
