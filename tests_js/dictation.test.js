const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const src = fs.readFileSync('chrome-extension/background.js', 'utf8');
const code = src.slice(src.indexOf('function pageOperation('), src.indexOf('async function inject('));
let serial = 0;
function makeInput(type='text', value='ABC', index=3) {
  const el = {
    tagName: 'INPUT', isContentEditable: false, readOnly:false, disabled:false,
    value, selectionStart:index, selectionEnd:index, labels:[], id:'',
    getAttribute(name) {return name === 'type' ? type : null;},
    closest(){return null;},
    getBoundingClientRect(){return {left:0,top:0,right:150,bottom:40,width:150,height:40};},
    dispatchEvent() {},
    setRangeText(text,start,end){this.value=this.value.slice(0,start)+text+this.value.slice(end);this.selectionStart=this.selectionEnd=start+text.length;},
  };
  return el;
}
const input=makeInput();
const doc={activeElement:input};
const world={document:doc,window:{innerWidth:300,innerHeight:300},
  location:{href:'https://example.org/editor',origin:'https://example.org'},
  crypto:{randomUUID:()=>String(++serial)},
  InputEvent:class {constructor(type,props){this.type=type;this.props=props;}},
  URL};
vm.runInNewContext(code,world);
const probe=world.pageOperation('dictation_probe',{});
assert.equal(typeof probe.token,'string');
let result=world.pageOperation('dictation_insert',{token:probe.token,text:'-こんにちは'});
assert.equal(result.verified,true);
assert.equal(input.value,'ABC-こんにちは');
assert.equal(input.selectionStart,input.value.length);
assert.throws(()=>world.pageOperation('dictation_insert',{token:probe.token,text:'\n'}),/改行/);
assert.equal(input.value,'ABC-こんにちは');
assert.throws(()=>world.pageOperation('dictation_insert',{token:'other',text:'x'}),/変わりました/);
// Selection must never be silently overwritten.
input.selectionStart=0;input.selectionEnd=2;
assert.throws(()=>world.pageOperation('dictation_insert',{token:probe.token,text:'x'}),/選択/);
assert.equal(input.value,'ABC-こんにちは');
input.selectionStart=input.selectionEnd;
const other=makeInput();doc.activeElement=other;
assert.throws(()=>world.pageOperation('dictation_insert',{token:probe.token,text:'X'}),/変わりました/);
assert.equal(other.value,'ABC');
doc.activeElement=input;
world.location.href='https://example.org/another';
assert.throws(()=>world.pageOperation('dictation_insert',{token:probe.token,text:'x'}),/変わりました/);
// Credential fields fail before any text can be written.
for (const type of ['password','hidden','number']) {
  doc.activeElement=makeInput(type);
  assert.throws(()=>world.pageOperation('dictation_probe',{}),/入力/);
}
doc.activeElement=makeInput('text');doc.activeElement.readOnly=true;
assert.throws(()=>world.pageOperation('dictation_probe',{}),/読み取り専用/);
// Untrusted text must never be interpreted as keyboard hotkeys.
world.location.href='https://example.org/editor';
doc.activeElement=makeInput('text','',0);
const pinned=world.pageOperation('dictation_probe',{});
result=world.pageOperation('dictation_insert',{token:pinned.token,text:'^s{ENTER}%'});
assert.equal(doc.activeElement.value,'^s{ENTER}%');
console.log('Voice dictation: caret, focus, privacy, newline, literal text checks passed');