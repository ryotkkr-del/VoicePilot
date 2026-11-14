const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const src=fs.readFileSync('chrome-extension/background.js','utf8');
const source=src.slice(src.indexOf('function pageOperation('),src.indexOf('async function inject('));
const control={
 id:'',labels:[],dataset:{},tagName:'INPUT',disabled:false,
 getAttribute: key=>key==='aria-labelledby'?'searchlabel':key==='type'?'text':null,
 getBoundingClientRect:()=>({x:10,y:10,left:10,top:10,width:200,height:40,bottom:50,right:210}),
 closest:()=>null,matches:()=>false,
};
const document={title:'Test',querySelectorAll: s=>s.includes('h1,h2,h3')?[]:[control],
 getElementById: id=>id==='searchlabel'?{innerText:'論文を検索'}:null};
const sandbox={document,window:{innerWidth:500,innerHeight:500},location:{origin:'https://example.com',pathname:'/'},
 getComputedStyle:()=>({display:'block',visibility:'visible'}),crypto:{randomUUID:()=> 'field-01'}};
vm.runInNewContext(source,sandbox);
const snap=sandbox.pageOperation('snapshot',{});
assert.equal(snap.elements[0].label,'論文を検索');
console.log('Accessible-name parsing: 1 check passed');