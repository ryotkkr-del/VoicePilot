// Regression checks: consent for one page/target cannot be reused on another.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('chrome-extension/background.js', 'utf8');
const code = source.slice(source.indexOf('function pageOperation('), source.indexOf('async function inject('));
let inForm = false, inDialog = false, name = '詳細を見る';
const button = {
  dataset:{}, id:'', tagName:'BUTTON', disabled:false,
  getAttribute: key => key === 'type' ? 'button' : null,
  getBoundingClientRect: () => ({x:10,y:10,left:10,top:10,right:90,bottom:40,width:80,height:30}),
  closest: selector => selector === 'form' ? (inForm ? {} : null)
     : selector.includes('dialog') ? (inDialog ? {} : null) : null,
  matches: () => false,
  contains: () => false,
  focus:()=>{}, click:()=>{button.clicks++}, clicks:0,
};
Object.defineProperty(button, 'innerText', {get:()=>name});
const document={
  title:'Demo', documentElement:{scrollHeight:600},
  querySelectorAll:q=>q.includes('h1,h2,h3') ? [] : [button],
  elementFromPoint:()=>button,
};
const location={origin:'https://example.org',pathname:'/page', href:'https://example.org/page?task=1'};
const sandbox={document,location,window:{innerWidth:400,innerHeight:400},
  crypto:{randomUUID:()=> 'target-1'},getComputedStyle:()=>({display:'block',visibility:'visible'}), URL};
vm.runInNewContext(code,sandbox);
const page=sandbox.pageOperation('snapshot',{});
const target=page.elements[0];
assert.ok(page.page_key);
const args={id:target.id,expected:{...target},expected_page_key:page.page_key};
sandbox.pageOperation('browser_click',args);
assert.equal(button.clicks,1);

// The node is re-used by a reactive app, but it becomes a form submit control.
inForm=true;
assert.throws(()=>sandbox.pageOperation('browser_click',args),/変わりました/);
assert.equal(button.clicks,1);
inForm=false;
// The node is moved into a modal dialog after the user saw the approval text.
inDialog=true;
assert.throws(()=>sandbox.pageOperation('browser_click',args),/変わりました/);
assert.equal(button.clicks,1);
inDialog=false;
// Same DOM node/label, but SPA navigation changed the URL query.
location.href='https://example.org/page?task=2';
assert.throws(()=>sandbox.pageOperation('browser_click',args),/URLが変わりました/);
assert.equal(button.clicks,1);
location.href='https://example.org/page?task=1';
name='削除';
assert.throws(()=>sandbox.pageOperation('browser_click',args),/変わりました/);
assert.equal(button.clicks,1);
console.log('Approval binding: 9 assertions passed');