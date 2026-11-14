// Real page-operation contract: never include user-entered text in snapshots.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const src = fs.readFileSync('chrome-extension/background.js', 'utf8');
const block = src.slice(src.indexOf('function pageOperation('), src.indexOf('async function inject('));
let counter = 0;
function el(tag, attr={}, other={}) {
  const item = {
    tagName:tag, id:'', disabled:false, dataset:{}, innerText:'', value:'', isContentEditable:false,
    getAttribute: name => attr[name] || null,
    getBoundingClientRect: () => ({x:5,y:5,left:5,top:5,right:100,bottom:45,width:95,height:40}),
    closest: s => null, matches: s => s.includes('input[type="password"]') && attr.type==='password',
    focus:()=>{}, click:()=>{item.clicked=(item.clicked||0)+1;}, contains:()=>false,
    ...other,
  };
  return item;
}
const rich=el('DIV',{role:'textbox'},{isContentEditable:true,innerText:'PRIVATE DRAFT DO NOT LEAK'});
const password=el('INPUT',{type:'password',name:'password'},{value:'SUPERSECRET'});
const credit=el('INPUT',{type:'text',autocomplete:'cc-number'},{value:'4111111111111111'});
const token=el('INPUT',{type:'text',name:'api_key'},{value:'PRIVATE_API_TOKEN'});
const normal=el('INPUT',{type:'text',placeholder:'Search'},{value:'previous query'});
const formButton=el('BUTTON',{}, {innerText:'Go', closest:s=>s==='form'?{}:null});
let href='/first?sig=secret';
const external=el('A',{}, {innerText:'Go somewhere',getAttribute:k=>k==='href'?href:null,
    closest:s=>s==='a[href]'?external:null});
let elements=[rich,password,credit,token,normal,formButton,external];
const document={title:'test',documentElement:{scrollHeight:300},
    querySelectorAll:s=>s.includes('h1,h2,h3')?[]:elements,
    elementFromPoint:()=>formButton};
const sandbox={document,window:{innerWidth:500,innerHeight:400},
    location:{origin:'https://safe.example',href:'https://safe.example/home',pathname:'/home'},
    getComputedStyle:()=>({display:'block',visibility:'visible'}),
    crypto:{randomUUID:()=>String(++counter)},URL};
vm.runInNewContext(block, sandbox);
const snap=sandbox.pageOperation('snapshot',{});
const serialized=JSON.stringify(snap);
for (const sensitive of ['PRIVATE DRAFT','SUPERSECRET','4111111111111111','PRIVATE_API_TOKEN','previous query','sig=secret']) {
  assert.equal(serialized.includes(sensitive),false,`Leaked ${sensitive}`);
}
assert.equal(snap.elements.length,4); // rich editor, normal text field, button, link
const field=snap.elements.find(e=>e.label==='Search');
assert.equal(field.has_value,true);
const button=snap.elements.find(e=>e.label==='Go');
assert.equal(button.in_form,true);
const link=snap.elements.find(e=>e.label==='Go somewhere');
assert.equal(link.external_origin,''); // same-origin relative link
const expected={...link};
href='/second?sig=secret';
assert.throws(()=>sandbox.pageOperation('browser_click',{id:link.id,expected}), /変わりました/);
console.log('Sensitive-field boundaries: 10 checks passed');