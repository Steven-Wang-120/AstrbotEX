/* Authenticated EX chat workbench. Binary bodies stay inside the peer ciphertext. */
window.ChatMediaPage = (() => {
  const $ = (id) => document.getElementById(id);
  const base = '/api/v1/ex/chat/account';
  const emoji = Array.from('😀😃😄😁😆😂🤣😊🙂😉😍🥰😘😋😎🤓🥳🤩🤔🫡🤗🤭😴🥱😭🥺😤😡🤯😱😳🫠🤫🙄😅😇😈👻💀🤖😺🙈🙉🙊').concat(['❤️','💕','💖','👍','👎','👏','🙌','🫶','🤝','🙏','💪','✌️','🤞','👋','👌','🔥','✨','🎉','🎂','🎁','🌹','🌸','🌈','☀️','🌙','⭐','☕','🍻','🎵','🚀','✅','❌','💯']);
  let initialized = false, peer = '', draft = null, pending = null, signature = '', generation = 0, loading = false, sending = false;
  const media = new Map(), urls = new Map();
  let statusTimer;
  const status = (message) => {
    clearTimeout(statusTimer);
    $('chatComposeStatus').textContent = message;
    $('chatComposeStatus').hidden = !message;
    if (message) statusTimer=setTimeout(() => status(''), 6000);
  };
  const post = (path, value) => apiJson(base + path, {method:'POST', body:JSON.stringify(value)});
  const task = async (work) => { try { await work(); } catch (e) { status(e.message || '操作未完成，请重试'); } };
  const button = (label, handler) => { const b = document.createElement('button'); b.className='btn btn-ghost btn-sm'; b.textContent=label; b.addEventListener('click', () => task(handler)); return b; };
  const icons = {download:'M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5', favorite:'m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9Z', close:'m6 6 12 12M6 18 18 6', add:'M12 4v16M4 12h16'};
  const iconButton = (label, icon, handler) => {
    const b=button('',handler); b.title=label; b.setAttribute('aria-label',label);
    const svg=document.createElementNS('http://www.w3.org/2000/svg','svg'), path=document.createElementNS(svg.namespaceURI,'path');
    svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('class','chat-icon');svg.setAttribute('aria-hidden','true');path.setAttribute('d',icons[icon]);svg.append(path);b.append(svg);return b;
  };
  const uuid = () => globalThis.crypto?.randomUUID?.() || '10000000-1000-4000-8000-100000000000'.replace(/[018]/g, c => (Number(c) ^ crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> Number(c) / 4).toString(16));
  const bytes = (value) => Uint8Array.from(atob(value), c => c.charCodeAt(0));
  const fileSize = (value) => value >= 1048576 ? (value/1048576).toFixed(1)+' MiB' : Math.ceil(value/1024)+' KiB';
  function objectUrl(item) {
    const key=item.sha256+':'+item.kind;
    if(urls.has(key))return urls.get(key);
    const url = URL.createObjectURL(new Blob([bytes(item.data)], {type:item.kind === 'file' ? 'application/octet-stream' : item.mime}));
    urls.set(key,url); return url;
  }
  function clearMedia() { for (const url of urls.values()) URL.revokeObjectURL(url); urls.clear(); media.clear(); }
  let pruneTimer;
  function pruneUrls() {
    clearTimeout(pruneTimer);
    pruneTimer=setTimeout(() => {
      const used=new Set(Array.from(document.querySelectorAll('img[src]'), img => img.src));
      for(const [key,url] of urls) if(!used.has(url)) { URL.revokeObjectURL(url);urls.delete(key); }
    },0);
  }
  async function getAttachment(id) {
    if (!media.has(id)) media.set(id, apiJson(base+'/media/'+encodeURIComponent(id)));
    try { return await media.get(id); } catch (e) { media.delete(id); throw e; }
  }
  function download(item) {
    const url=URL.createObjectURL(new Blob([bytes(item.data)],{type:'application/octet-stream'}));
    const a=document.createElement('a'); a.href=url; a.download=item.name; a.click();
    setTimeout(()=>URL.revokeObjectURL(url),10000);
  }
  function preview(url) {
    const dialog=document.createElement('dialog'); dialog.className='chat-lightbox';
    const img=document.createElement('img'); img.src=url; img.alt='图片预览';
    dialog.append(img,iconButton('关闭', 'close', () => dialog.close()));
    dialog.addEventListener('close', () => dialog.remove()); document.body.append(dialog); dialog.showModal();
  }
  function toggle(value) {
    const attachment=$('chatAttachmentPanel'), smile=$('chatEmojiPanel');
    const opening=(value==='attachment' ? attachment : smile).hidden;
    attachment.hidden=true; smile.hidden=true;
    if (opening) (value==='attachment' ? attachment : smile).hidden=false;
    $('chatAttachmentToggle').setAttribute('aria-expanded', String(!attachment.hidden));
    $('chatEmojiToggle').setAttribute('aria-expanded', String(!smile.hidden));
  }
  async function stickerTab(show) {
    $('chatEmojiGrid').hidden=show; $('chatStickerGrid').hidden=!show;
    if (!show) return;
    const value=await apiJson(base+'/stickers'), grid=$('chatStickerGrid'); grid.replaceChildren();
    if (!value.items.length) grid.append(iconButton('导入表情包', 'add', () => $('chatStickerPicker').click()));
    for (const item of value.items) {
      const box=document.createElement('div'), img=document.createElement('img'); img.src=objectUrl(item); img.alt=item.name; img.tabIndex=0; img.setAttribute('role','button');
      img.addEventListener('click', () => task(() => send(item)));
      img.addEventListener('keydown', e => { if(e.key==='Enter'||e.key===' ') {e.preventDefault();task(() => send(item));} });
      box.append(img, iconButton('移除收藏', 'close', async () => { await post('/stickers/remove', {sha256:item.sha256}); await stickerTab(true); })); grid.append(box);
    }
    pruneUrls();
  }
  function renderDraft() {
    const box=$('chatDraft'); box.replaceChildren(); box.hidden=!draft; if(!draft){pruneUrls();return;}
    if(draft.kind!=='file') { const img=document.createElement('img'); img.src=objectUrl(draft); img.alt=draft.name; box.append(img); }
    const text=document.createElement('span'); text.textContent=`${draft.name} · ${fileSize(draft.size)}`;
    box.append(text,iconButton('移除附件', 'close', () => {draft=null;renderDraft();}));
    pruneUrls();
  }
  async function selectedFile(input, kind, favorite=false) {
    const file=input.files[0]; input.value=''; if(!file)return;
    if(!file.size || file.size>8388608) throw new Error('附件须为 1 字节至 8 MiB');
    const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=reject;reader.readAsDataURL(file);});
    const item=await post('/attachments/prepare',{kind,name:file.name,mime:kind==='file'?'application/octet-stream':file.type,data});
    if(favorite) { await post('/stickers',{attachment:item}); $('chatEmojiPanel').hidden=false; $('chatAttachmentPanel').hidden=true; await stickerTab(true); status('已收藏表情包'); }
    else {draft=item;renderDraft();status('');}
  }
  async function send(sticker=null) {
    if(sending)return;
    if(!peer)throw new Error('请先选择已接受的好友');
    const input=$('chatMessageInput'), text=input.value.trim(), item=sticker||draft;
    if(!text&&!item)return;
    const content={text,...(item?{attachments:[item]}:{})};
    if(!pending||pending.peer_id!==peer||JSON.stringify(pending.content)!==JSON.stringify(content)) pending={peer_id:peer,content,client_message_id:uuid()};
    const sentPeer=peer, sentText=input.value, sentDraft=draft;
    sending=true;$('chatSend').disabled=true;
    try { await post('/messages',pending); pending=null;
      if(peer===sentPeer) { if(input.value===sentText)input.value='';if(!sticker&&draft===sentDraft)draft=null;renderDraft(); signature=''; await load(); }
      status('');
    } finally {sending=false;$('chatSend').disabled=false;}
  }
  async function load() {
    if(!peer||loading)return; loading=true; const expected=generation, selected=peer;
    try {
      const value=await apiJson(base+'/messages/'+encodeURIComponent(selected));
      if(expected!==generation||selected!==peer)return;
      const next=JSON.stringify(value); if(signature===next)return; signature=next;
      const current=++generation;
      const activeIds=new Set(value.items.flatMap(item=>item.segments.filter(segment=>['image','file','sticker'].includes(segment.type)).map(segment=>segment.data.media_id)));
      for(const id of media.keys())if(!activeIds.has(id))media.delete(id);
      const container=$('chatMessages'), stick=container.scrollHeight-container.scrollTop-container.clientHeight<80;
      container.replaceChildren(); let day='';
      for(const item of value.items) {
        const date=new Date(item.created_at), label=Number.isNaN(date.getTime())?'时间未知':date.toLocaleDateString();
        if(label!==day){const separator=document.createElement('div');separator.className='chat-day';separator.textContent=label;container.append(separator);day=label;}
        const line=document.createElement('div'), bubble=document.createElement('div'), mine=item.sender_id===value.self_id;
        line.className='chat-line'+(mine?' mine':'');bubble.className='chat-bubble';
        if(item.segments.length===1&&item.segments[0].type==='sticker')bubble.classList.add('sticker');
        for(const segment of item.segments) {
          if(segment.type==='text'){const text=document.createElement('p');text.textContent=segment.data.text;bubble.append(text);}
          else if(['image','file','sticker'].includes(segment.type)) {
            const slot=document.createElement('div'), text=document.createElement('p');text.textContent=`${segment.data.name} · ${fileSize(segment.data.size_bytes)}`;slot.append(text);bubble.append(slot);
            getAttachment(segment.data.media_id).then(attachment=>{
              if(!slot.isConnected||current!==generation)return;
              if(attachment.kind!=='file'){const img=document.createElement('img');img.className='chat-media-preview';img.alt=attachment.name;img.src=objectUrl(attachment);img.addEventListener('click',()=>preview(img.src));slot.prepend(img);}
              const tools=document.createElement('div');tools.className='chat-media-tools';tools.append(iconButton('下载', 'download',()=>download(attachment)));
              if(attachment.kind!=='file')tools.append(iconButton('收藏表情', 'favorite',async()=>{await post('/stickers',{media_id:segment.data.media_id});status('已收藏');}));slot.append(tools);
              if(stick)container.scrollTop=container.scrollHeight;
            }).catch(()=>{text.textContent='附件加载失败，刷新后重试';});
          }
        }
        const meta=document.createElement('div'), time=document.createElement('time');meta.className='chat-meta';time.dateTime=item.created_at;time.title=date.toLocaleString();time.textContent=Number.isNaN(date.getTime())?'时间未知':date.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',hour12:false});meta.append(time);
        if(mine){const state=document.createElement('span'), accepted=item.delivery==='accepted';state.textContent=accepted?'✓':'◷';state.title=accepted?'已入邮箱':'等待投递';state.setAttribute('aria-label',state.title);meta.append(state);}
        bubble.append(meta);line.append(bubble);container.append(line);
      }
      if(stick||!value.items.length)container.scrollTop=container.scrollHeight;
      pruneUrls();
    } finally {loading=false;if(peer&&selected!==peer)task(load);}
  }
  function contacts(value) {
    if(!initialized)init(); const select=$('chatContactSelect'), previous=peer;
    select.replaceChildren();const placeholder=document.createElement('option');placeholder.value='';placeholder.textContent='选择好友';select.append(placeholder);
    for(const contact of value.contacts||[]){if(contact.state!=='accepted')continue;const option=document.createElement('option');option.value=contact.id;option.textContent=contact.name;select.append(option);}
    select.value=previous;peer=select.value;
    if(peer)task(load);
  }
  function init() {
    if(initialized)return;initialized=true;
    $('chatContactSelect').addEventListener('change',()=>{peer=$('chatContactSelect').value;generation++;signature='';pending=null;clearMedia();renderDraft();if(!$('chatStickerGrid').hidden)task(()=>stickerTab(true));$('chatMessages').replaceChildren();task(load);});
    $('chatAttachmentToggle').addEventListener('click',()=>toggle('attachment'));$('chatEmojiToggle').addEventListener('click',()=>toggle('emoji'));
    $('chatPickImage').addEventListener('click',()=>$('chatImagePicker').click());$('chatPickCamera').addEventListener('click',()=>$('chatCameraPicker').click());$('chatPickFile').addEventListener('click',()=>$('chatFilePicker').click());
    $('chatShowStickers').addEventListener('click',()=>task(async()=>{$('chatAttachmentPanel').hidden=true;$('chatEmojiPanel').hidden=false;await stickerTab(true);}));
    $('chatEmojiTab').addEventListener('click',()=>task(()=>stickerTab(false)));$('chatStickerTab').addEventListener('click',()=>task(()=>stickerTab(true)));$('chatImportSticker').addEventListener('click',()=>$('chatStickerPicker').click());
    for(const [id,kind,favorite] of [['chatImagePicker','image',false],['chatCameraPicker','image',false],['chatFilePicker','file',false],['chatStickerPicker','sticker',true]])$(id).addEventListener('change',()=>task(()=>selectedFile($(id),kind,favorite)));
    for(const value of emoji){const b=button(value,()=>{const input=$('chatMessageInput'), start=input.selectionStart, end=input.selectionEnd;input.setRangeText(value,start,end,'end');});b.setAttribute('aria-label',value);$('chatEmojiGrid').append(b);}
    $('chatSend').addEventListener('click',()=>task(()=>send()));$('chatMessageInput').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();task(()=>send());}});
    setInterval(()=>{if(document.visibilityState==='visible'&&$('page-chat').getClientRects().length&&peer)task(load);},3000);
    window.addEventListener('beforeunload',clearMedia);
  }
  return {init,contacts};
})();
