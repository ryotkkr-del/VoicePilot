// On-demand screen reading must not expose editor values or hidden page text.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const src = fs.readFileSync('chrome-extension/background.js','utf8');
const block = src.slice(src.indexOf('function pageOperation('), src.indexOf('async function inject('));
function item(text, config={}) {
  const rect = {left:3,top:8,right:120,bottom:35,width:117,height:27};
  return {tagName:'P', innerText:text, textContent:text,
    closest:s => config.hidden && s.includes('[aria-hidden') ? {} : config.editor && s.includes('[contenteditable]') ? {} :
       config.form && s.includes('form') ? {} : null,
    getBoundingClientRect:()=> config.offscreen ? {...rect,top:900,bottom:930} : rect,
    ...config};
}
const paragraphs = [item('Public visible documentation on the screen.'),
  item('PRIVATE TEXT from editor', {editor:true}),
  item('HIDDEN PASSWORD from page', {hidden:true}),
  item('PRIVATE FORM CONTENT', {form:true}),
  item('Not currently visible to user.', {offscreen:true}),
  item('CSS-HIDDEN CONTENT', {hiddenStyle:true})];
const sandbox = {
  document: {querySelectorAll:()=> paragraphs},
  window:{innerWidth:800,innerHeight:500},
  getComputedStyle:el=> ({display:el.hiddenStyle?'none':'block',visibility:'visible'})
};
vm.runInNewContext(block,sandbox);
const result=sandbox.pageOperation('read_visible',{});
const wire=JSON.stringify(result);
assert.equal(result.blocks.length,1);
assert.ok(wire.includes('Public visible'));
for(const text of ['PRIVATE','PASSWORD','HIDDEN','Not currently visible'])
  assert.ok(!wire.includes(text),`Private or hidden text leaked: ${text}`);
const huge=sandbox.pageOperation('read_visible',{});
assert.ok(JSON.stringify(huge).length<3600);
console.log('Read-only visible document text tested');