/* EX owns a normal network identity. Agent access remains behind OneBot. */
window.ChatAccountPage = (() => {
  let initialized = false;
  const el = (id) => document.getElementById(id);
  const action = async (button, work) => {
    button.disabled = true;
    try { await work(); } catch (error) { el('chatAccountStatus').textContent = error.message; }
    finally { button.disabled = false; }
  };
  async function refresh() {
    const value = await apiJson('/api/v1/ex/chat/account');
    el('chatAccountStatus').textContent = value.enabled
      ? `聊天网络${value.online ? '已连接' : '连接中'} · Agent ${value.onebot_connected ? '已连接' : '未连接'}${value.error ? ' · ' + value.error : ''}`
      : '尚未创建聊天身份；先启用 EX 聊天服务。';
    el('chatAccountCard').value = value.contact_code || '';
    el('chatAccountFingerprint').textContent = value.card?.id || '';
    if (value.enabled && !el('chatAccountName').matches(':focus') && !el('chatAccountRelay').matches(':focus')) {
      el('chatAccountName').value = value.card.name;
      el('chatAccountRelay').value = value.card.relay;
      el('chatAccountAutoAccept').checked = value.auto_accept;
    }
    const requests = el('chatAccountRequests');
    requests.replaceChildren();
    for (const request of value.requests || []) {
      if (request.state !== 'pending') continue;
      const row = document.createElement('div');
      row.className = 'panel';
      const title = document.createElement('p');
      title.textContent = `${request.card.name} · ${request.comment || '申请添加好友'}`;
      const fingerprint = document.createElement('p');
      fingerprint.textContent = request.card.id;
      fingerprint.style.overflowWrap = 'anywhere';
      row.append(title, fingerprint);
      for (const [label, approve] of [['接受', true], ['拒绝', false]]) {
        const button = document.createElement('button');
        button.className = 'btn btn-ghost btn-sm';
        button.textContent = label;
        button.addEventListener('click', () => action(button, async () => {
          await apiJson('/api/v1/ex/chat/account/requests', {method: 'POST', body: JSON.stringify({flag: request.flag, approve})});
          await refresh();
        }));
        row.append(button);
      }
      requests.append(row);
    }
  }
  function init() {
    if (initialized) return;
    initialized = true;
    el('chatAccountRefresh').addEventListener('click', (event) => action(event.currentTarget, refresh));
    el('chatAccountSave').addEventListener('click', (event) => action(event.currentTarget, async () => {
      await apiJson('/api/v1/ex/chat/account', {method:'POST', body: JSON.stringify({
        name: el('chatAccountName').value.trim(), relay: el('chatAccountRelay').value.trim(), auto_accept: el('chatAccountAutoAccept').checked,
      })});
      await refresh();
    }));
    el('chatAccountCopy').addEventListener('click', (event) => action(event.currentTarget, async () => {
      if (!el('chatAccountCard').value) throw new Error('请先创建身份并刷新名片。');
      await navigator.clipboard.writeText(el('chatAccountCard').value);
      el('chatAccountStatus').textContent = '名片已复制，可在 astrC 中粘贴添加。';
    }));
    el('chatAccountAdd').addEventListener('click', (event) => action(event.currentTarget, async () => {
      await apiJson('/api/v1/ex/chat/account/contacts', {method:'POST', body: JSON.stringify({code: el('chatAccountContact').value.trim()})});
      el('chatAccountContact').value = '';
      await refresh();
    }));
  }
  function enter() { init(); refresh().catch(error => { el('chatAccountStatus').textContent = error.message; }); }
  return {init, enter};
})();
