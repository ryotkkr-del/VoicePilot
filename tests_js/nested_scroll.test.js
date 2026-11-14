const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const src = fs.readFileSync('chrome-extension/background.js','utf8');
const source = src.slice(src.indexOf('function pageOperation('),src.indexOf('async function inject('));
const pane = {
  scrollTop:0, scrollHeight:2800, clientHeight:400, parentElement:null,
  getBoundingClientRect: () => ({top:5,left:5,right:400,bottom:405,width:395,height:400})
};
pane.scrollTo = opts => { pane.scrollTop = Math.max(0,Math.min(opts.top, pane.scrollHeight-pane.clientHeight)); };
const window = {
  scrollY:0, innerHeight:720, innerWidth:500,
  scrollTo: () => {}, scrollBy:()=>{},
};
const document = {
  activeElement:pane,
  documentElement:{scrollHeight:720},
};
const sandbox={window,document,getComputedStyle: el=>({overflowY:el===pane?'auto':'hidden'})};
vm.runInNewContext(source,sandbox);
assert.equal(sandbox.pageOperation('browser_scroll',{direction:'down'}).verified,true);
assert.ok(pane.scrollTop>0);
assert.equal(sandbox.pageOperation('browser_scroll',{direction:'top'}).verified,true);
assert.equal(pane.scrollTop,0);
assert.equal(sandbox.pageOperation('browser_scroll',{direction:'bottom'}).verified,true);
assert.equal(pane.scrollTop,2400);
assert.equal(sandbox.pageOperation('browser_scroll',{direction:'bottom'}).verified,false);
console.log('Nested-scroll behavior: 4 checks passed');