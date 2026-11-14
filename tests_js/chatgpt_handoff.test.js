// Pure DOM test: filling is draft-only, preserves existing text, never submits.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const src = fs.readFileSync('chrome-extension/background.js', 'utf8');
const start = src.indexOf('function insertChatGPTDraft(');
const end = src.indexOf('async function chatgptPrepare(', start);
assert.ok(start >= 0 && end > start);
const functionSource = src.slice(start, end);
function runDraft(existing, desired) {
  let submits = 0, inputEvents = 0;
  const editor = {
    isContentEditable:true,
    innerText:existing,
    focus(){},
    dispatchEvent(event){if(event.type === 'input') inputEvents++;},
  };
  const document = {
    querySelector(){return editor;},
    createRange(){return {selectNodeContents(){},collapse(){}};},
    execCommand(command, _show, input){
      if(command !== 'insertText') throw new Error('unexpected command');
      editor.innerText = input;
      return true;
    },
  };
  const window = {getSelection(){return {removeAllRanges(){},addRange(){}};}};
  const sandbox = {
    location:{origin:'https://chatgpt.com'}, document, window,
    InputEvent:class {constructor(type){this.type=type;}},
    submit(){submits++;},
  };
  vm.runInNewContext(functionSource, sandbox);
  return {result:sandbox.insertChatGPTDraft(desired), editor, inputEvents, submits};
}
const filled=runDraft('', '日本語の質問です');
assert.equal(filled.result.prepared, true);
assert.equal(filled.editor.innerText, '日本語の質問です');
assert.equal(filled.inputEvents,1);
assert.equal(filled.submits,0);
const existing=runDraft('自分の下書き', '上書きしない');
assert.equal(existing.result.prepared,false);
assert.equal(existing.result.reason,'existing_draft');
assert.equal(existing.editor.innerText,'自分の下書き');
assert.equal(existing.inputEvents,0);
const same=runDraft('同じ', '同じ');
assert.equal(same.result.prepared,true);
assert.equal(same.inputEvents,0);
console.log('ChatGPT draft safety: 9 checks passed');