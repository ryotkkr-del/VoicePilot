const tokenInput = document.getElementById('token');
const status = document.getElementById('status');
const connectButton = document.getElementById('connect');

function say(message, color = '#475569') {
  status.textContent = message;
  status.style.color = color;
}

chrome.storage.local.get(['token'], data => {
  tokenInput.value = data.token || '';
});

async function refresh() {
  try {
    const response = await chrome.runtime.sendMessage({type:'status'});
    say(response?.connected ? '● Windowsアプリに接続中' : '○ 未接続 — Windowsアプリを起動してください',
        response?.connected ? '#15803d' : '#64748b');
  } catch {
    say('接続状態を取得できません。拡張機能を再読み込みしてください。', '#b91c1c');
  }
}

connectButton.addEventListener('click', async () => {
  const token = tokenInput.value.trim();
  if (!token) {
    say('Windowsアプリでコピーした接続コードを貼り付けてください。', '#b91c1c');
    tokenInput.focus();
    return;
  }
  connectButton.disabled = true;
  say('接続しています…');
  try {
    await chrome.storage.local.set({token});
    await chrome.runtime.sendMessage({type:'reconnect'});
    // Checking once after reconnect can race with the WebSocket handshake.
    await new Promise(resolve => setTimeout(resolve, 800));
    await refresh();
  } catch {
    say('接続処理に失敗しました。Windowsアプリの起動を確認してください。', '#b91c1c');
  } finally {
    connectButton.disabled = false;
  }
});

refresh();
// While the popup is open, connection changes update without extra clicks.
setInterval(refresh, 2500);