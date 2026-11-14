const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('chrome-extension/background.js', 'utf8');
const code = source.slice(source.indexOf('function pageOperation('), source.indexOf('async function inject('));
let sequence=0;
function make(tag, attrs={}, extra={}) {
  const node={
    tagName:tag, dataset:{}, disabled:false, id:'', innerText:'', value:'', isContentEditable:false,
    getAttribute:key=>attrs[key] || null, labels:[],
    getBoundingClientRect:()=>({x:8,y:8,left:8,top:8,right:100,bottom:36,width:92,height:28}),
    closest:()=>null, matches:()=>false,
    focus:()=>{}, click:()=>{node.clicks++}, clicks:0,
    ...extra,
  };
  return node;
}
const linkedSubmit=make('BUTTON', {form:'checkout-form'}, {innerText:'次へ'});
const formCheckbox=make('INPUT', {type:'checkbox'}, {closest:s=>s==='form'?{}:null});
const privateLabel=make('INPUT', {type:'text','aria-labelledby':'otp-caption'});
const readOnly=make('INPUT', {type:'text',placeholder:'名前'}, {readOnly:true, matches:s=>s.startsWith('input:not(')});
const nodes=[linkedSubmit,formCheckbox,privateLabel,readOnly];
const doc={title:'Demo',documentElement:{scrollHeight:1000},
  querySelectorAll:q=>q.includes('h1,h2,h3')?[]:nodes,
  getElementById:id=>id==='otp-caption'?{innerText:'ワンタイム認証コード'}:null,
  elementFromPoint:()=>readOnly};
const sandbox={document:doc,window:{innerWidth:500,innerHeight:500},
  location:{origin:'https://example.org',href:'https://example.org/forms',pathname:'/forms'},
  crypto:{randomUUID:()=>String(++sequence)},
  getComputedStyle:()=>({display:'block',visibility:'visible'}), URL};
vm.runInNewContext(code,sandbox);
const snap=sandbox.pageOperation('snapshot',{});
assert.equal(snap.elements.length,3); // credential field never becomes a target
assert.equal(snap.elements[0].in_form,true);  // button associated with a remote form
assert.equal(snap.elements[1].in_form,true);  // checkbox is a state-changing control
const editable=snap.elements.find(x=>x.label==='名前');
assert.ok(editable);
assert.throws(()=>sandbox.pageOperation('browser_fill',{
  id:editable.id, text:'test', expected:editable, expected_page_key:snap.page_key,
}),/読み取り専用/);
assert.equal(readOnly.value,'');
console.log('Form and private fields: 6 checks passed');