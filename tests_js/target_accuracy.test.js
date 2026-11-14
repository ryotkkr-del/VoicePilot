// DOM tests for stale labels, invisible click interception, and exact targets.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const src = fs.readFileSync('chrome-extension/background.js', 'utf8');
const start = src.indexOf('function pageOperation(');
const end = src.indexOf('async function inject(', start);
assert.ok(start >= 0 && end > start);
const el = {
  id: '', dataset: {}, tagName: 'BUTTON', innerText: '次へ', disabled: false,
  getAttribute: (key) => key === 'type' ? 'button' : null,
  getBoundingClientRect: () => ({x:10, y:10, left:10, top:10, width:100, height:40, bottom:50, right:110}),
  closest: () => null, matches: () => false, contains: () => false,
  focus: () => {}, click: () => { el.clicked += 1; }, clicked:0,
};
const document = {
  title:'Test', documentElement:{scrollHeight:1000},
  querySelectorAll: (selector) => selector.includes('h1,h2,h3') ? [] : [el],
  elementFromPoint: () => el,
};
const window = {innerWidth:600, innerHeight:500};
const sandbox = { document, window, location:{origin:'https://example.com',pathname:'/test'},
  getComputedStyle: () => ({display:'block', visibility:'visible'}),
  crypto:{randomUUID:()=> 'id123'} };
vm.runInNewContext(src.slice(start,end), sandbox);
const snapshot = sandbox.pageOperation('snapshot',{});
assert.equal(snapshot.elements.length,1);
const desc = snapshot.elements[0];
const args = {id:desc.id, expected:{label:desc.label,tag:desc.tag,role:desc.role,type:desc.type}};
sandbox.pageOperation('browser_click',args);
assert.equal(el.clicked,1);
el.innerText = '削除';
assert.throws(() => sandbox.pageOperation('browser_click',args), /変わりました/);
assert.equal(el.clicked,1);
el.innerText = '次へ';
document.elementFromPoint = () => ({contains:()=>false});
assert.throws(() => sandbox.pageOperation('browser_click',args), /隠れています/);
assert.equal(el.clicked,1);
document.elementFromPoint = () => el;
assert.throws(() => sandbox.pageOperation('browser_click',{id:desc.id}), /操作対象が不明/);
assert.equal(el.clicked,1);
console.log('Chrome target binding: 5 checks passed');