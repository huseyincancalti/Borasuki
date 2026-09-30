const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const events = {}, windowEvents = {}, classes = new Set(), timers = new Map();
const overlay = {classList:{add:value=>classes.add(value),remove:value=>classes.delete(value)}};
const status = {hidden:true};
let reason = null, next = 0;
const errors = [];
const window = {BorasukiWait:{render:()=>{}}, addEventListener:(name,fn)=>windowEvents[name]=fn};
const document = {getElementById:id=>id==='fileDropOverlay'?overlay:status, addEventListener:(name,fn)=>events[name]=fn};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../frontend/file-drop.js'),'utf8'), {
  window, document, setTimeout:fn=>{timers.set(++next,fn);return next;}, clearTimeout:id=>timers.delete(id)
});
const drop = window.BorasukiFileDrop({blocked:()=>reason,error:value=>errors.push(value)});
function event(files=true) {
  return {dataTransfer:{types:files?['Files']:['text/plain']},relatedTarget:{},preventDefault(){this.prevented=true;},stopImmediatePropagation(){this.stopped=true;}};
}
const row = event(false); events.dragenter(row); events.drop(row);
assert.equal(classes.size,0); assert.equal(row.prevented,undefined); assert.equal(timers.size,0);
events.dragenter(event()); events.dragenter(event()); events.dragleave(event());
assert(classes.has('visible')); // Child transitions do not flicker.
events.dragleave(event()); assert.equal(classes.size,0);
events.dragenter(event()); events.keydown({key:'Escape'}); assert.equal(classes.size,0);
events.dragenter(event()); windowEvents.blur(); assert.equal(classes.size,0);
const copy=event(); events.dragover(copy); assert.equal(copy.dataTransfer.dropEffect,'copy');
events.drop(event()); assert.equal(status.hidden,false); assert(drop.waiting());
drop.loading(); assert.equal(timers.size,0); assert.equal(status.hidden,false);
drop.settled(); assert.equal(status.hidden,true);
events.drop(event()); const timeout=[...timers.values()][0]; timeout();
assert.equal(status.hidden,true); assert.equal(errors.at(-1),'error.drop_path');
reason='error.finish_edit'; const rejected=event(); events.drop(rejected);
assert.equal(rejected.stopped,true); assert.equal(errors.at(-1),reason); assert.equal(status.hidden,true);
events.dragenter(event()); assert.equal(classes.size,0);
events.dragover(rejected); assert.equal(rejected.dataTransfer.dropEffect,'none');
console.log('File drop feedback tests passed');
