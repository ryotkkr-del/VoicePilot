/* VoicePilot v0.14: fresh document references for safe follow-up instructions. */
let socket = null, keepalive = null, retry = null, authenticated = false;
let snapshotTabId = null, snapshotPageKey = null;
let snapshotTargets = new Map();  // exact labels/types from most recent snapshot
const URL = 'ws://127.0.0.1:8765/ws';

async function connect() {
  const {token} = await chrome.storage.local.get('token');
  if (!token || (socket && [WebSocket.OPEN, WebSocket.CONNECTING].includes(socket.readyState))) return;
  const ws = new WebSocket(URL); socket = ws;
  ws.onopen = () => {
    ws.send(JSON.stringify({type:'hello', token}));
    if (keepalive) clearInterval(keepalive);
    keepalive = setInterval(() => { if (ws.readyState === WebSocket.OPEN) ws.send('ping'); }, 20000);
  };
  ws.onmessage = async event => {
    if (event.data === 'pong') return;
    let message;
    try { message = JSON.parse(event.data); } catch { return; }
    if (message.type === 'ready') { authenticated = true; return; }
    if (message.type !== 'request' || !authenticated) return;
    try {
      const result = await perform(message.action, message.args || {});
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type:'response', id:message.id, ok:true, result}));
    } catch (e) {
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type:'response', id:message.id, ok:false, error:String(e.message || e).slice(0,250)}));
    }
  };
  ws.onclose = () => {
    if (socket !== ws) return;
    socket = null;
    authenticated = false;
    if (keepalive) { clearInterval(keepalive); keepalive = null; }
    if (retry) clearTimeout(retry);
    retry = setTimeout(connect, 2000);
  };
  ws.onerror = () => {};
}

chrome.runtime.onInstalled.addListener(connect);
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === 'status') sendResponse({connected:authenticated});
  else if (message.type === 'reconnect') {
    authenticated = false;
    if (keepalive) { clearInterval(keepalive); keepalive = null; }
    if (socket) socket.close();
    socket = null;
    connect(); sendResponse({ok:true});
  }
});

async function activeTab() {
  let tabs = await chrome.tabs.query({active:true, lastFocusedWindow:true});
  if (!tabs.length) tabs = await chrome.tabs.query({active:true, currentWindow:true});
  if (!tabs.length) throw new Error('Chromeのアクティブなタブがありません');
  return tabs[0];
}

// Runs inside the tab's isolated world. Never sends form field values or page text
// wholesale. IDs are short-lived element handles in the current document.
function pageOperation(operation, params) {
  const selector = 'a[href],button,input,textarea,select,[role="button"],[role="link"],[role="textbox"],[role="searchbox"],[role="tab"],[role="menuitem"],[role="checkbox"],[contenteditable="true"],[tabindex]';
  function sensitive(el) {
    // Accessible names can carry a sensitive meaning even if field metadata is generic.
    // The name is a label, never a value typed by the user.
    const metadata = (['type','name','id','autocomplete','aria-label','placeholder']
      .map(key => el.getAttribute?.(key) || '').join(' ') + ' ' +
      (inputLike(el) ? label(el) : '')).toLowerCase();
    return /password|passwd|passcode|secret|api[_ -]?key|access[_ -]?token|private[_ -]?key|otp|2fa|one.time|verification.code|cc.number|cc.csc|cvv|cvc|card.number|パスワード|暗証|認証コード|クレジット|秘密鍵|シークレット/.test(metadata);
  }
  function inputLike(el) {
    return ['INPUT','TEXTAREA'].includes(el.tagName) || el.isContentEditable ||
      ['textbox','searchbox'].includes(el.getAttribute?.('role'));
  }
  function hasValue(el) {
    if (!inputLike(el)) return false;
    return Boolean(String(el.isContentEditable ? (el.innerText || el.textContent || '') : (el.value || '')).trim());
  }
  function linkSignature(el) {
    const link = el.closest?.('a[href]') || (el.tagName === 'A' ? el : null);
    if (!link) return '';
    const href = String(link.getAttribute?.('href') || '');
    // Only a small stable fingerprint goes to the model, never sensitive query tokens.
    let hash = 2166136261;
    for (let i=0; i<href.length; i++) hash = Math.imul(hash ^ href.charCodeAt(i), 16777619);
    return String(hash >>> 0);
  }
  // A short page fingerprint stays inside the extension. It includes URL query
  // and fragment, but the URL itself is never sent to an AI model or log.
  function pageKey() {
    const href = String(location.href || (location.origin + location.pathname));
    let hash = 2166136261;
    for (let i = 0; i < href.length; i++) hash = Math.imul(hash ^ href.charCodeAt(i), 16777619);
    return String(hash >>> 0);
  }
  function externalOrigin(el) {
    const link = el.closest?.('a[href]') || (el.tagName === 'A' ? el : null);
    if (!link) return '';
    try {
      const dest = new URL(link.getAttribute('href'), location.href);
      if (!['http:','https:'].includes(dest.protocol)) return '特殊なリンク';
      return dest.origin !== location.origin ? dest.origin : '';
    } catch { return '不明なリンク'; }
  }
  function inForm(el) {
    const type = String(el.getAttribute?.('type') || '').toLowerCase();
    const role = String(el.getAttribute?.('role') || '').toLowerCase();
    // Focusing a text editor is not a submission; toggles and buttons can
    // mutate app state. A button may belong to a remote form via form="id".
    const textEditor = el.tagName === 'TEXTAREA' || el.isContentEditable ||
      ['textbox','searchbox'].includes(role) ||
      (el.tagName === 'INPUT' && !['checkbox','radio','submit','button','reset','image'].includes(type));
    return !textEditor && Boolean(el.form || el.getAttribute?.('form') || el.closest?.('form'));
  }
  function inDialog(el) { return Boolean(el.closest?.('[role="dialog"],dialog,[aria-modal="true"]')); }
  function candidates() {
    return [...document.querySelectorAll(selector)].filter(el => {
      if (el.closest('[aria-hidden="true"], [inert]') || el.disabled) return false;
      if (el.matches('input[type="password"],input[type="hidden"],input[type="file"]') || sensitive(el)) return false;
      const rect = el.getBoundingClientRect();
      const css = getComputedStyle(el);
      return rect.width > 1 && rect.height > 1 && css.display !== 'none' && css.visibility !== 'hidden';
    });
  }
  function label(el) {
    const linked = (el.getAttribute('aria-labelledby') || '').split(/\s+/).slice(0, 4)
      .map(id => document.getElementById?.(id)?.innerText || '').filter(Boolean).join(' ');
    const nativeLabels = Array.from(el.labels || []).slice(0, 3)
      .map(item => item.innerText || item.textContent || '').join(' ');
    const associated = el.id && typeof CSS !== 'undefined'
      ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
    return (el.getAttribute('aria-label') || linked || nativeLabels ||
      associated?.innerText || el.getAttribute('title') ||
      el.getAttribute('placeholder') || (inputLike(el) ? '' : (el.innerText || '').trim()) ||
      (el.tagName === 'INPUT' ? el.getAttribute('name') : '') || '').replace(/\s+/g, ' ').trim().slice(0,110);
  }
  function visible(el) {
    const rect = el.getBoundingClientRect();
    return rect.bottom >= 0 && rect.top < window.innerHeight && rect.right >= 0 && rect.left < window.innerWidth;
  }
  function elementForId(id) { return candidates().find(el => el.dataset.voicepilotId === id); }
  function assertSameTarget(el, expected) {
    if (!expected || !el) throw new Error('操作対象が不明です。画面を再確認してください');
    if (label(el) !== expected.label ||
        el.tagName.toLowerCase() !== expected.tag ||
        (el.getAttribute('role') || '').slice(0,30) !== expected.role ||
        (el.getAttribute('type') || '').slice(0,25) !== expected.type ||
        (expected.link_key !== undefined && linkSignature(el) !== expected.link_key) ||
        (expected.external_origin !== undefined && externalOrigin(el) !== expected.external_origin) ||
        (expected.has_value !== undefined && hasValue(el) !== expected.has_value) ||
        (expected.in_form !== undefined && inForm(el) !== expected.in_form) ||
        (expected.in_dialog !== undefined && inDialog(el) !== expected.in_dialog)) {
      throw new Error('ボタンや入力欄の内容が変わりました。もう一度画面を確認してください');
    }
  }
  function assertNotCovered(el) {
    if (typeof document.elementFromPoint !== 'function') return;
    const rect = el.getBoundingClientRect();
    const x = Math.max(0, Math.min(window.innerWidth - 1, rect.left + rect.width / 2));
    const y = Math.max(0, Math.min(window.innerHeight - 1, rect.top + rect.height / 2));
    const hit = document.elementFromPoint(x, y);
    if (hit && hit !== el && !el.contains(hit) && !hit.contains(el)) {
      throw new Error('操作対象が別の画面要素で隠れています。画面を再確認してください');
    }
  }
  // Dictation is a separate opt-in route: pin the *focused* editor, not a
  // snapshot label. This private token stays in the extension's isolated world.
  function focusedDictationEditor() {
    const focused = document.activeElement;
    if (!focused) throw new Error('入力欄にカーソルを置いてください');
    const el = focused.isContentEditable
      ? (focused.closest?.('[contenteditable="true"]') || focused)
      : focused;
    const kind = el.tagName;
    const inputType = (el.getAttribute?.('type') || 'text').toLowerCase();
    if (!(el.isContentEditable || kind === 'TEXTAREA' ||
          (kind === 'INPUT' && ['text','search','email','url','tel'].includes(inputType)))) {
      throw new Error('文字入力可能な欄にカーソルを置いてください');
    }
    if (sensitive(el) || el.disabled || el.readOnly || el.getAttribute?.('aria-readonly') === 'true' ||
        el.closest?.('[inert],[aria-hidden="true"]')) {
      throw new Error('機密情報欄・読み取り専用欄には入力できません');
    }
    if (!visible(el)) throw new Error('入力欄が画面に表示されていません');
    return el;
  }
  if (operation === 'dictation_probe') {
    const el = focusedDictationEditor();
    const token = crypto.randomUUID();
    window.__voicepilotDictationTarget = {element:el, token, page:location.href};
    return {token};  // No editor text or page contents leave Chrome.
  }
  if (operation === 'dictation_insert') {
    const text = params.text;
    if (typeof text !== 'string' || !text || text.length > 2000 ||
        /[\x00-\x09\x0b-\x1f\x7f]/.test(text)) throw new Error('入力文字が不正です');
    const state = window.__voicepilotDictationTarget;
    const el = focusedDictationEditor();
    if (!state || state.token !== params.token || state.element !== el || state.page !== location.href)
      throw new Error('ページか入力欄が変わりました。文章入力モードを開始し直してください');
    if (el.isContentEditable) {
      const selection = window.getSelection();
      if (!selection || selection.rangeCount === 0 || !selection.isCollapsed ||
          !el.contains(selection.anchorNode))
        throw new Error('カーソル位置を確認できません。文章の選択を解除してください');
      // execCommand inserts at the caret, preserving the existing rich text.
      // Never replace textContent wholesale if the editor rejects it.
      const command = text === '\n' ? 'insertLineBreak' : 'insertText';
      if (!document.execCommand(command, false, command === 'insertText' ? text : null))
        throw new Error('この入力欄は音声タイピングに対応していません');
      return {inserted:true, verified:false}; // Rich editors can transform text.
    }
    if (typeof el.selectionStart !== 'number' || typeof el.selectionEnd !== 'number' ||
        el.selectionStart !== el.selectionEnd)
      throw new Error('選択中の文章を上書きしません。選択を解除してください');
    if (el.tagName === 'INPUT' && text.includes('\n'))
      throw new Error('この入力欄での改行は送信になるおそれがあるため禁止しています');
    if (typeof el.setRangeText !== 'function')
      throw new Error('この入力欄への追記に対応していません');
    const caret = el.selectionStart;
    // setRangeText performs insertion without simulating Enter or submit.
    el.setRangeText(text, caret, caret, 'end');
    el.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:text}));
    return {inserted:true, verified:el.value.slice(caret, caret+text.length) === text};
  }
  if (operation === 'read_visible') {
    // On-demand extraction of currently visible *document* text. Never read
    // form values, editors, passwords, hidden content or scripts.
    const selector = 'main p,article p,[role="main"] p,main li,article li,[role="main"] li,h1,h2,h3,main pre,article pre';
    const blocks = [];
    let total = 0;
    for (const el of [...document.querySelectorAll(selector)].slice(0, 120)) {
      if (el.closest?.('form,[contenteditable],textarea,input,[role="textbox"],[aria-hidden="true"],[inert],script,style,nav')) continue;
      if (!visible(el)) continue;
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      if (rect.width <= 1 || rect.height <= 1 || style.display === 'none' || style.visibility === 'hidden') continue;
      const raw = String(el.innerText || el.textContent || '').replace(/\s+/g,' ').trim();
      if (raw.length < 10) continue;
      const piece = raw.slice(0, 340);
      if (total + piece.length > 3500) break;
      blocks.push(piece); total += piece.length;
      if (blocks.length >= 16) break;
    }
    return {blocks};
  }
  if (operation === 'snapshot') {
    const list = candidates().slice(0, 220);
    const ranked = [...list.filter(visible), ...list.filter(el => !visible(el))].slice(0, 110);
    return {
      title:document.title.slice(0,150),
      page_key:pageKey(), // removed by the service worker before returning the snapshot
      url:location.origin + location.pathname.slice(0,170),
      headings:[...document.querySelectorAll('h1,h2,h3')].filter(visible)
        .slice(0,12).map(el => (el.innerText||'').replace(/\s+/g,' ').trim().slice(0,100)),
      elements:ranked.map(el => {
        if (!el.dataset.voicepilotId) el.dataset.voicepilotId = crypto.randomUUID();
        const rect = el.getBoundingClientRect();
        return {id:el.dataset.voicepilotId, tag:el.tagName.toLowerCase(),
          role:(el.getAttribute('role') || '').slice(0,30),
          type:(el.getAttribute('type') || '').slice(0,25),
          label:label(el), in_view:visible(el),
          has_value:hasValue(el), in_form:inForm(el), in_dialog:inDialog(el),
          external_origin:externalOrigin(el), link_key:linkSignature(el),
          // Coordinates are approximate hints only; clicks still require an element handle.
          pos:[Math.round(rect.x),Math.round(rect.y)]};
      })
    };
  }
  if (operation === 'browser_scroll') {
    const amount = Math.min(1000, window.innerHeight * 0.75);
    const beforeY = window.scrollY;
    if (params.direction === 'top' || params.direction === 'bottom') {
      const target = params.direction === 'top' ? 0 : document.documentElement.scrollHeight;
      window.scrollTo({top:target, behavior:'instant'});
    } else if (params.direction === 'up' || params.direction === 'down') {
      window.scrollBy({top:params.direction === 'up' ? -amount : amount, behavior:'instant'});
    } else {
      throw new Error('スクロール方向が不正です');
    }
    if (window.scrollY !== beforeY) return {direction:params.direction, verified:true};
    // Many modern web apps scroll inside a pane (chat, IDE, modal) while
    // window.scrollY remains zero. Only inspect a small candidate set when
    // the cheap document scroll did not move at all.
    const canScroll = node => {
      if (!node || !(node.scrollHeight > node.clientHeight + 4)) return false;
      const overflow = getComputedStyle(node).overflowY || '';
      return /^(auto|scroll|overlay)$/.test(overflow);
    };
    const focusChain = [];
    for (let el = document.activeElement, depth = 0; el && depth < 8; el = el.parentElement, depth++) {
      if (canScroll(el)) focusChain.push(el);
    }
    const candidates = focusChain.length ? focusChain :
      [...(document.querySelectorAll?.('main,[role="main"],[class*="scroll"]') || [])]
        .slice(0, 60).filter(canScroll).filter(el => visible(el))
        .sort((a,b) => {
          const ar = a.getBoundingClientRect(), br = b.getBoundingClientRect();
          return (br.width * br.height) - (ar.width * ar.height);
        });
    for (const pane of candidates.slice(0, 3)) {
      const before = pane.scrollTop;
      const top = params.direction === 'top' ? 0 : params.direction === 'bottom'
        ? pane.scrollHeight : before + (params.direction === 'up' ? -amount : amount);
      pane.scrollTo({top,behavior:'instant'});
      if (pane.scrollTop !== before) return {direction:params.direction, verified:true};
    }
    return {direction:params.direction, verified:false};
  }
  if (operation === 'browser_click' || operation === 'browser_fill') {
    if (params.expected_page_key !== undefined && pageKey() !== params.expected_page_key)
      throw new Error('画面のURLが変わりました。操作対象を再確認してください');
    if (typeof params.id !== 'string') throw new Error('要素IDがありません');
    const el = elementForId(params.id);
    if (!el) throw new Error('画面が変わりました。対象が見つかりません');
    // IDs alone are insufficient: a reactive app can reuse a DOM node for a
    // different action between observation and click. Bind to its semantics.
    assertSameTarget(el, params.expected);
    // Reactive UIs can replace input metadata without changing tag/label.
    if (sensitive(el)) throw new Error('機密情報の入力欄は操作できません');
    if (!visible(el)) el.scrollIntoView({block:'center', behavior:'instant'});
    if (!visible(el)) throw new Error('操作対象が画面上に表示できません');
    assertNotCovered(el);
    if (operation === 'browser_click') {
      el.focus(); el.click(); return {clicked:label(el), verified:false};
    }
    if (!(el.matches('input:not([type="checkbox"]):not([type="radio"]),textarea,[contenteditable="true"]')))
      throw new Error('文字を入力できる要素ではありません');
    if (el.matches('input[type="password"],input[type="hidden"],input[type="file"]'))
      throw new Error('機密入力欄は操作できません');
    if (el.readOnly || el.getAttribute?.('aria-readonly') === 'true')
      throw new Error('読み取り専用の欄には入力できません');
    if (String(params.text||'').length > 3000) throw new Error('入力が長すぎます');
    el.focus();
    if (el.isContentEditable) {
      // Rich editors may require native events; native insertText works on many sites.
      const selection = window.getSelection();
      const range = document.createRange(); range.selectNodeContents(el);
      selection.removeAllRanges(); selection.addRange(range);
      if (!document.execCommand('insertText', false, String(params.text))) el.textContent = params.text;
    } else {
      const prototype = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(prototype, 'value').set.call(el, String(params.text));
    }
    el.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:String(params.text)}));
    el.dispatchEvent(new Event('change', {bubbles:true}));
    const actual = el.isContentEditable ? (el.textContent || '') : el.value;
    return {filled:label(el), verified:actual === String(params.text)};
  }
  throw new Error('未対応のページ操作');
}

async function inject(tabId, operation, args) {
  const tab = await chrome.tabs.get(tabId);
  if (!tab.url || !/^https?:\/\//.test(tab.url)) throw new Error('Chrome内部ページや特殊なタブは操作できません');
  const result = await chrome.scripting.executeScript({target:{tabId}, func:pageOperation, args:[operation,args]});
  return result[0]?.result || {};
}

// ChatGPT handoff intentionally touches the *composer only*. It neither
// submits the user's text nor extracts assistant responses. This browser-UI
// shortcut does NOT invoke Work, Codex, or the OpenAI API.
function insertChatGPTDraft(prompt) {
  if (location.origin !== 'https://chatgpt.com') return {prepared:false, reason:'not_chatgpt'};
  const editor = document.querySelector('#prompt-textarea') ||
    document.querySelector('[data-testid="composer-input"]') ||
    document.querySelector('[contenteditable="true"][role="textbox"]');
  if (!editor) return {prepared:false, reason:'composer_unavailable'};
  const existing = (editor.isContentEditable ? editor.innerText : editor.value || '').trim();
  if (existing) {
    return {prepared:existing === prompt.trim(), reason:existing === prompt.trim() ? 'already_prepared' : 'existing_draft'};
  }
  if (!prompt) return {prepared:true, reason:'opened_only'};
  editor.focus();
  // Only insert into an empty composer. Never erase the user's current draft.
  if (editor.isContentEditable) {
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(editor);
    range.collapse(false);
    selection.removeAllRanges();
    selection.addRange(range);
    const inserted = document.execCommand('insertText', false, prompt);
    if (!inserted) editor.textContent = prompt;
    editor.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:prompt}));
  } else if (editor.matches('textarea,input[type="text"]')) {
    const prototype = editor.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, 'value').set.call(editor, prompt);
    editor.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:prompt}));
  } else {
    return {prepared:false, reason:'unsupported_composer'};
  }
  const actual = (editor.isContentEditable ? editor.innerText : editor.value || '').trim();
  return {prepared:actual === prompt.trim(), reason:actual === prompt.trim() ? 'draft' : 'insertion_unverified'};
}

async function chatgptPrepare(prompt) {
  if (typeof prompt !== 'string' || prompt.length > 4000) throw new Error('質問は4000文字以内にしてください');
  const tabs = await chrome.tabs.query({url:['https://chatgpt.com/*']});
  const existing = tabs.find(t => t.active) || tabs[0];
  const tab = existing || await chrome.tabs.create({url:'https://chatgpt.com/'});
  if (existing) await chrome.tabs.update(tab.id, {active:true});
  await chrome.windows.update(tab.windowId, {focused:true});
  if (!prompt) return {opened:true, prepared:true};
  // Newly opened pages and login redirects need time to expose the composer.
  for (let i=0; i<7; i++) {
    const current = await chrome.tabs.get(tab.id);
    if (current.url && /^https:\/\/chatgpt\.com\//.test(current.url)) {
      try {
        const results = await chrome.scripting.executeScript({
          target:{tabId:tab.id}, func:insertChatGPTDraft, args:[prompt]
        });
        const result = results[0]?.result || {};
        if (result.prepared) return {opened:true, prepared:true};
        if (result.reason === 'existing_draft')
          return {opened:true, prepared:false, reason:'existing_draft'};
      } catch (_) { /* e.g. document still loading; use safe clipboard fallback */ }
    }
    if (i<6) await new Promise(resolve => setTimeout(resolve, 450));
  }
  return {opened:true, prepared:false, reason:'composer_unavailable'};
}

async function perform(action, args) {
  if (action === 'extension_version') return {version: chrome.runtime.getManifest().version};
  if (action === 'extension_reload') {
    setTimeout(() => chrome.runtime.reload(), 350);
    return {scheduled:true};
  }
  if (action === 'chatgpt_prepare') return chatgptPrepare(args.prompt);
  if (action === 'dictation_probe') {
    const tab = await activeTab();
    const probe = await inject(tab.id, 'dictation_probe', {});
    if (typeof probe.token !== 'string' || !probe.token) throw new Error('入力欄を確認できませんでした');
    return {token:probe.token, tab_id:tab.id};
  }
  if (action === 'dictation_insert') {
    const tab = await activeTab();
    if (tab.id !== args.tab_id || typeof args.token !== 'string')
      throw new Error('タブが変わりました。文章入力を開始し直してください');
    if (typeof args.text !== 'string' || !args.text || args.text.length > 2000)
      throw new Error('入力する文字数を確認してください');
    return inject(tab.id, 'dictation_insert', {token:args.token, text:args.text});
  }
  if (action === 'read_visible') {
    // Read-only, and only for the foreground Chrome tab.
    const tab = await activeTab();
    return inject(tab.id, 'read_visible', {});
  }
  if (action === 'snapshot') {
    const tab = await activeTab();
    const tabs = (await chrome.tabs.query({currentWindow:true})).slice(0,24)
      .map(t => ({title:(t.title||'').slice(0,90), id:t.id}));
    snapshotTabId = tab.id;
    snapshotPageKey = null;
    snapshotTargets = new Map();
    try {
      const page = await inject(tab.id,'snapshot',{});
      snapshotPageKey = page.page_key || null;
      snapshotTargets = new Map((page.elements || []).map(el => [el.id, {
        label:el.label, tag:el.tag, role:el.role, type:el.type,
        has_value:el.has_value, in_form:el.in_form, in_dialog:el.in_dialog,
        link_key:el.link_key,
        external_origin:el.external_origin
      }]));
      const {page_key, ...publicPage} = page;
      return {...publicPage, tabs, document_ref:`${tab.id}:${snapshotPageKey}`};
    } catch(e) {
      return {title:tab.title||'', url:tab.url?.split('?')[0]||'', tabs, error:String(e.message||e)};
    }
  }
  if (['browser_click','browser_fill','browser_scroll'].includes(action)) {
    const tab = await activeTab();
    if (action !== 'browser_scroll') {
      if (snapshotTabId !== tab.id)
        throw new Error('別のタブに移動しました。操作前に画面を再確認してください');
      const expected = snapshotTargets.get(args.id);
      if (!expected) throw new Error('最新の画面にない要素です。もう一度確認してください');
      if (!snapshotPageKey) throw new Error('操作対象の画面情報がありません。もう一度確認してください');
      return inject(tab.id, action, {...args, expected, expected_page_key:snapshotPageKey});
    }
    return inject(tab.id, action, args);
  }
  if (action === 'browser_search') {
    if (typeof args.query !== 'string' || args.query.trim().length > 150 || !args.query.trim()) throw new Error('検索語が不正です');
    const tab = await chrome.tabs.create({url:'https://www.google.com/search?q=' + encodeURIComponent(args.query)});
    return {searched:args.query, verified:Number.isInteger(tab.id)};
  }
  if (action === 'browser_open' || action === 'browser_new_tab') {
    const url = new URL(args.url);
    if (!['http:','https:'].includes(url.protocol) || url.username || url.password) throw new Error('HTTP/HTTPSのみ開けます');
    const tab = action === 'browser_new_tab'
      ? await chrome.tabs.create({url:url.href})
      : await chrome.tabs.update((await activeTab()).id, {url:url.href});
    // This confirms that Chrome accepted navigation, not that the page loaded.
    return {opened:url.origin + url.pathname, verified:Number.isInteger(tab.id)};
  }
  if (action === 'browser_back' || action === 'browser_forward') {
    const tab = await activeTab();
    if (action === 'browser_back') await chrome.tabs.goBack(tab.id);
    else await chrome.tabs.goForward(tab.id);
    return {navigation:action, verified:false};
  }
  if (action === 'browser_switch_tab') {
    const title = (args.title||'').toLowerCase();
    if (title.length < 2) throw new Error('タブ名が短すぎます');
    const tabs = await chrome.tabs.query({});
    const exact = tabs.filter(t => (t.title||'').toLowerCase() === title);
    const matches = exact.length ? exact : tabs.filter(t => (t.title||'').toLowerCase().includes(title));
    if (!matches.length) throw new Error('タブが見つかりません');
    if (matches.length > 1) throw new Error('似た名前のタブが複数あります。対象を詳しく指定してください');
    const found = matches[0];
    await chrome.windows.update(found.windowId, {focused:true});
    await chrome.tabs.update(found.id, {active:true});
    const activated = await chrome.tabs.get(found.id);
    return {selected:found.title, verified:activated.active === true};
  }
  throw new Error('未対応の操作: ' + action);
}
connect();