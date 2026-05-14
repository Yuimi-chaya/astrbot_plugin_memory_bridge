// v0.5.5 integrated WebUI patch
// 修复点：会话/记忆包头像解析、已回滚隐藏按钮、用户白名单页、避免引用不存在的 v054/v504 js。
(function(){
  'use strict';
  const $id = (id)=>document.getElementById(id);
  const esc = (s)=>String(s ?? '').replace(/[&<>"']/g, m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
  const bytes = (n)=>{ try { return fmtBytes(n); } catch(e){ n=Number(n||0); return n<1024?n+' B':n<1048576?(n/1024).toFixed(1)+' KB':(n/1048576).toFixed(2)+' MB'; } };
  const files = ()=>{ try { return Array.isArray(allFiles)?allFiles:[]; } catch(e){ return []; } };
  const sessions = ()=>{ try { return Array.isArray(usableSessions)?usableSessions:[]; } catch(e){ return []; } };
  const rawSessions = ()=>{ try { return Array.isArray(allRawSessions)?allRawSessions:[]; } catch(e){ return []; } };
  const allSessions = ()=>[].concat(sessions(), rawSessions()).filter(Boolean);
  let aliasState={users:{},group_members:{},origins:{}};
  function aliasText(entry){
    if(!entry) return '';
    const text=typeof entry==='object' ? (entry.alias || entry.name || entry.nickname || '') : entry;
    return String(text||'').trim();
  }
  function aliasFor(s, origin, groupId, userId){
    const direct=aliasText(s && (s.manual_alias || s.alias));
    if(direct) return direct;
    const aliases=aliasState||{};
    if(origin){
      const byOrigin=aliasText((aliases.origins||{})[origin]);
      if(byOrigin) return byOrigin;
    }
    if(groupId && userId){
      const byMember=aliasText((aliases.group_members||{})[`${groupId}:${userId}`]);
      if(byMember) return byMember;
    }
    if(userId){
      const byUser=aliasText((aliases.users||{})[userId]);
      if(byUser) return byUser;
    }
    return '';
  }
  async function loadAliases(){
    try{
      const data=await api('/api/user-aliases');
      aliasState=data.aliases||{users:{},group_members:{},origins:{}};
    }catch(e){
      aliasState=aliasState||{users:{},group_members:{},origins:{}};
    }
  }
  function tail(origin){ const p=String(origin||'').split(':'); return p[p.length-1] || ''; }
  function parseOrigin(origin){
    const text=String(origin||''), t=tail(text);
    let isGroup=/GroupMessage/i.test(text), isFriend=/FriendMessage/i.test(text), userId='', groupId='';
    const m=t.match(/^(\d+)_(\d+)$/);
    if(m){ userId=m[1]; groupId=m[2]; isGroup=true; }
    else if(/^\d+$/.test(t)){ if(isGroup) groupId=t; else { userId=t; isFriend=true; } }
    return {origin:text, tail:t, isGroup, isFriend, userId, groupId};
  }
  function qUser(id){ return id ? `https://q1.qlogo.cn/g?b=qq&nk=${encodeURIComponent(id)}&s=100` : ''; }
  function qGroup(id){ return id ? `https://p.qlogo.cn/gh/${encodeURIComponent(id)}/${encodeURIComponent(id)}/100` : ''; }
  function goodName(s){
    s=String(s||'').trim();
    if(!s || /^群聊\s*[\d_]*$/.test(s) || /^私聊\s*\d+$/.test(s) || /^\d+$/.test(s) || /^\d+_\d+$/.test(s)) return false;
    return true;
  }
  function groupNames(){
    const map={};
    allSessions().forEach(s=>{
      const p=parseOrigin(s.origin); const gid=s.group_id || p.groupId; if(!gid) return;
      let name=s.group_name || '';
      if(!name && goodName(s.display_name || s.name)) name=String(s.display_name || s.name).split(' · ')[0];
      if(goodName(name) && (!map[gid] || String(name).length > String(map[gid]).length)) map[gid]=name;
    });
    return map;
  }
  function byOrigin(origin){ return allSessions().find(s=>s && s.origin===origin) || null; }
  function view(obj){
    const s = typeof obj === 'string' ? (byOrigin(obj) || {origin: obj}) : (obj || {});
    const origin=s.origin || s.source_origin || ''; const p=parseOrigin(origin); const gmap=groupNames();
    const groupId=String(s.group_id || p.groupId || ''); const userId=String(s.sender_id || p.userId || '');
    const manualAlias=aliasFor(s, origin, groupId, userId);
    const sender=String(manualAlias || s.sender_name || s.nickname || s.card || '');
    let groupTitle='', memberTitle='', title='', kind=p.isGroup||groupId?'群聊':p.isFriend?'私聊':'会话';
    if(p.isGroup || groupId){
      groupTitle = s.group_name || gmap[groupId] || (groupId ? `群聊 ${groupId}` : '群聊');
      memberTitle = sender || userId || '未知成员';
      title = groupTitle + (memberTitle ? ` · ${memberTitle}` : '');
    } else {
      title = s.display_name || s.name || sender || userId || tail(origin) || '未知会话';
      if(/^私聊\s*\d+$/.test(title) && userId) title=userId;
    }
    const mainAvatar = (p.isGroup||groupId) ? (s.group_avatar_url || qGroup(groupId) || s.avatar_url || '') : (s.avatar_url || qUser(userId) || '');
    const memberAvatar = (p.isGroup||groupId) ? (s.member_avatar_url || qUser(userId) || '') : '';
    const fallback = (p.isGroup||groupId) ? '群聊' : String(title||'会').slice(0,2);
    return {s, origin, p, groupId, userId, sender, manualAlias, groupTitle, memberTitle, title, kind, mainAvatar, memberAvatar, fallback};
  }
  function avatar(v, cls='avatar'){
    const composite=!!(v.memberAvatar && (v.p.isGroup || v.groupId));
    return `<span class="${esc(cls)} ${composite?'avatar-composite':''}"><span class="avatar-fallback">${esc(v.fallback)}</span>${v.mainAvatar?`<img class="avatar-img" src="${esc(v.mainAvatar)}" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()"/>`:''}${composite?`<span class="member-avatar"><span>${esc((v.memberTitle||v.userId||'员').slice(0,1))}</span><img src="${esc(v.memberAvatar)}" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()"/></span>`:''}</span>`;
  }
  function matchSession(s, kw){ kw=String(kw||'').toLowerCase().trim(); if(!kw) return true; const v=view(s); return [s.origin,v.title,v.groupTitle,v.memberTitle,s.last_message_preview,s.category,s.group_name,s.sender_name,v.groupId,v.userId].some(x=>String(x||'').toLowerCase().includes(kw)); }
  function getSel(role){ try { return role==='trigger' ? (selectedTriggerSession || ($id('sessionSelect')&&$id('sessionSelect').value) || '') : (selectedImportSession || ($id('importSessionSelect')&&$id('importSessionSelect').value) || ''); } catch(e){ return ''; } }
  function setSel(role, origin){
    if(role==='trigger'){ try{selectedTriggerSession=origin;}catch(e){} const s=$id('sessionSelect'); if(s) s.value=origin; const m=$id('manualSession'); if(m) m.value=''; }
    else { try{selectedImportSession=origin;}catch(e){} const s=$id('importSessionSelect'); if(s) s.value=origin; const m=$id('importManualSession'); if(m) m.value=''; }
    try{ rememberSelections(); }catch(e){}
  }
  function fillSelect(sel, list, selected){
    if(!sel) return;
    sel.innerHTML=(list||[]).map(s=>{const v=view(s); return `<option value="${esc(s.origin)}">${esc(v.title)} · ${esc(s.origin)}</option>`}).join('') || '<option value="">暂无可用会话，请手动输入 origin</option>';
    if(selected && Array.from(sel.options).some(o=>o.value===selected)) sel.value=selected;
    sel.removeAttribute('size'); sel.classList.add('compact-select');
  }
  function listFor(role){ const input=role==='trigger'?$id('sessionSearch'):$id('importSessionSearch'); const kw=input?input.value:''; return sessions().filter(s=>matchSession(s, kw)).slice(0,36); }
  function card(s, role, stat){
    const v=view(s), active=!stat && getSel(role)===s.origin;
    const body = `${avatar(v,'avatar')}<span class="session-main"><b>${esc(v.title)}</b><em>${esc(v.kind)}${v.manualAlias?' · 备注 '+esc(v.manualAlias):''}${v.groupId?' · 群号 '+esc(v.groupId):''}${v.userId?' · QQ '+esc(v.userId):''} · ${esc(s.last_seen||'-')}</em><small>${esc(s.origin)}</small><p>${esc(s.last_message_preview||'暂无预览')}</p>${stat?`<span class="item-actions"><button class="link-btn" onclick="event.stopPropagation();editSessionAliasV618('${esc(encodeURIComponent(s.origin))}')">编辑昵称</button><button class="link-btn" onclick="event.stopPropagation();useSession('${esc(s.origin)}')">用于触发</button><button class="link-btn" onclick="event.stopPropagation();previewHistory('${esc(s.origin)}')">预览历史</button><button class="link-btn" onclick="event.stopPropagation();useImportSession('${esc(s.origin)}')">迁移到此</button></span>`:''}</span>`;
    const classes = `session-card ${active?'active':''} ${stat?'big static-card':''} ${(v.p.isGroup||v.groupId)?'group-session-card':'friend-session-card'}`;
    return stat ? `<div class="${classes}">${body}</div>` : `<button class="${classes}" onclick="chooseSessionV055('${esc(role)}','${esc(s.origin)}')">${body}</button>`;
  }
  function renderCards(role){ const box=role==='trigger'?$id('triggerSessionCards'):$id('importSessionCards'); if(box) box.innerHTML=listFor(role).map(s=>card(s,role,false)).join('') || '<div class="hint">暂无匹配会话。</div>'; }
  try{ renderSessionCards = renderCards; }catch(e){} window.renderSessionCards = renderCards;
  window.chooseSessionV055=function(role, origin){ setSel(role, origin); renderCards(role); if(typeof switchPage==='function') switchPage(role==='trigger'?'trigger':'migrate'); };
  window.chooseSession=window.chooseSessionV055;
  window.useSession=(origin)=>window.chooseSessionV055('trigger',origin);
  window.useImportSession=(origin)=>window.chooseSessionV055('import',origin);
  window.editSessionAliasV618=async function(encodedOrigin){
    const origin=decodeURIComponent(String(encodedOrigin||''));
    const s=byOrigin(origin) || {origin};
    const v=view(s);
    const current=v.manualAlias || (goodName(v.memberTitle||v.title) ? (v.memberTitle||v.title) : '');
    const label=v.groupId ? `群 ${v.groupId} / QQ ${v.userId||''}` : (v.userId ? `QQ ${v.userId}` : origin);
    const value=prompt(`设置会话卡片昵称/备注：\n${label}\n\n留空并确认可清除备注。`, current);
    if(value===null) return;
    try{
      const data=await api('/api/user-aliases',{method:'POST',body:JSON.stringify({origin:v.origin,user_id:v.userId,group_id:v.groupId,alias:String(value||'').trim()})});
      aliasState=data.aliases||aliasState;
      if(typeof refreshAll==='function') await refreshAll();
      else renderSessionsV055(sessions(), rawSessions());
    }catch(e){
      alert('保存昵称失败：'+e.message);
    }
  };
  function renderSessionsV055(list, raw){
    const usable=Array.isArray(list)?list:sessions();
    fillSelect($id('sessionSelect'), usable, getSel('trigger'));
    fillSelect($id('importSessionSelect'), usable, getSel('import'));
    renderCards('trigger'); renderCards('import');
    const filtered=Math.max(0, ((raw||rawSessions()).length||0)-usable.length);
    const notice=filtered?`<div class="hint soft-note full-span">已隐藏 ${filtered} 个空会话：aiocqhttp/napcat 适配器会话、纯数字会话、数字_数字 会话。</div>`:'';
    const box=$id('sessionsList'); if(box) box.innerHTML=notice+(usable.map(s=>card(s,'trigger',true)).join('') || '<div class="hint">暂无可用会话记录。</div>');
  }
  try{ renderSessions = renderSessionsV055; }catch(e){} window.renderSessions=renderSessionsV055;
  function sourceOrigin(file){ return (file && (file.origin || file.source_origin || (file.manifest&&file.manifest.origin))) || ''; }
  function sourceLabel(file){ const o=sourceOrigin(file); const raw=file && (file.source_label||file.origin_label); if(raw && !/^群聊\s*\d+_\d+$/.test(String(raw))) return raw; return o ? view(o).title : '未知来源'; }
  function cleanFiles(){ return files().filter(f=>f && (f.migration_available || String(f.name||'').startsWith('memory_bridge_clean_'))); }
  function stat(file){ const s=file.stats||{}; if(s.raw_count!==undefined || s.kept_count!==undefined) return `原始/保留：${s.raw_count??'?'} / ${s.kept_count??'?'}`; if(s.message_count!==undefined) return `消息数：${s.message_count}`; return '数量未知'; }
  function selectedFile(){ try { return selectedImportFile || ($id('importFileSelect')&&$id('importFileSelect').value) || ''; } catch(e){ return ''; } }
  function setSelectedFile(n){ try{selectedImportFile=n;}catch(e){} try{rememberSelections();}catch(e){} }
  function renderFileCards(){
    const box=$id('importFileCards'); if(!box) return; const sel=selectedFile(); const list=cleanFiles();
    box.innerHTML=list.map(f=>{ const o=sourceOrigin(f), v=view(o), active=sel===f.name; const meta=o?`${(v.p.isGroup||v.groupId)?'群聊记忆包':'私聊记忆包'}${v.groupId?' · 群号 '+v.groupId:''}${v.userId?' · QQ '+v.userId:''}`:'origin 未记录；可点击预览导入计划读取包内 manifest。'; return `<button class="file-card ${active?'active':''}" onclick="selectImportFileV055('${esc(f.name)}')">${o?avatar(v,'file-icon file-avatar'):'<span class="file-icon file-avatar"><span class="avatar-fallback">忆</span></span>'}<span class="file-main"><b>${esc(f.name)}</b><em>${esc(sourceLabel(f))} · ${esc(f.kind||'clean')} · ${bytes(f.size)}</em><small>${esc(stat(f))} · ${esc(f.mtime||f.created_at||'-')}</small><p>${esc(meta)}${o?'<br>'+esc(o):''}</p></span></button>`; }).join('') || '<div class="hint">暂无可迁移 clean zip。</div>';
  }
  window.selectImportFileV055=function(name){ setSelectedFile(name); const s=$id('importFileSelect'); if(s) s.value=name; renderFileCards(); if(typeof switchPage==='function') switchPage('migrate'); if(typeof previewImport==='function') previewImport(); };
  window.selectImportFile=window.selectImportFileV055;
  function renderImportOptionsV055(){ const sel=$id('importFileSelect'); if(!sel) return; const list=cleanFiles(); sel.innerHTML=list.map(f=>`<option value="${esc(f.name)}">${esc(f.name+' · '+sourceLabel(f))}</option>`).join('') || '<option value="">暂无 clean zip，请先执行 /续忆 clean_export balanced</option>'; if(selectedFile() && Array.from(sel.options).some(o=>o.value===selectedFile())) sel.value=selectedFile(); sel.removeAttribute('size'); sel.classList.add('compact-select'); renderFileCards(); }
  try{ renderImportOptions = renderImportOptionsV055; }catch(e){} window.renderImportOptions=renderImportOptionsV055;
  function renderFilesV055(list){ const body=$id('filesBody'); if(!body) return; body.innerHTML=(list||[]).map(f=>{ const o=sourceOrigin(f), src=sourceLabel(f), st=f.stats||{}, stLine=st.raw_count?`原始/保留 ${st.raw_count}/${st.kept_count??'?'}`:''; return `<tr><td class="mono file-name">${esc(f.name)}</td><td>${esc(f.kind||'-')}${f.external_import?' · 外部导入':''}</td><td>${bytes(f.size)}</td><td>${esc(f.mtime||f.created_at||'-')}</td><td class="source-cell"><b>${esc(src)}</b><span>${esc(o||'')}</span>${stLine?`<em>${esc(stLine)}</em>`:''}</td><td class="op-cell"><a class="download" href="${encodeURI(f.download_url)}" target="_blank">下载</a><button class="link-btn" onclick="inspectFile('${esc(f.name)}')">检查</button>${f.migration_available?`<button class="link-btn" onclick="selectImportFileV055('${esc(f.name)}')">迁移</button>`:''}${(typeof fileDeleteEnabled!=='undefined'&&fileDeleteEnabled)?`<button class="link-btn danger" onclick="deleteFile('${esc(f.name)}')">删除</button>`:''}</td></tr>`; }).join('') || '<tr><td colspan="6" class="hint">暂无 zip 文件</td></tr>'; }
  try{ renderFiles=renderFilesV055; }catch(e){} window.renderFiles=renderFilesV055;
  function renderImportsV055(imports){ const box=$id('importsList'); if(!box) return; box.innerHTML=(imports||[]).map(item=>{ const conv=item.conversation||{}, pm=item.plugin_memory||{}, st=item.stats||{}, status=String(item.status||'-'); const rb=['rolled_back','rollback','disabled','已回滚'].includes(status), bad=status==='failed'; const text=rb?'已回滚':bad?'失败':status==='active'?'生效中':status; const target=item.target_label || (item.target_origin?view(item.target_origin).title:'未知目标'); const src=item.source_label || (item.source_origin?view(item.source_origin).title:'未知来源'); return `<div class="list-item import-record ${rb?'rolled-back':''} ${bad?'failed':''}"><strong>${esc(item.import_id||'-')} <span class="status-badge ${rb?'done':bad?'bad':'active'}">${esc(text)}</span></strong><span>模式：${esc(item.mode||'-')} · 目标：${esc(target)}</span><span>来源：${esc(src)} · 文件：${esc(item.source_file||'')}${item.external_import?' · 外部导入':''}</span>${item.chain_preview?`<span>链路：${esc(item.chain_preview)}</span>`:''}<span>conversation：${esc(conv.conversation_id||'无')} · chunks：${esc(pm.chunks||0)} · 原始/保留：${esc(st.raw_count??'?')} / ${esc(st.kept_count??'?')}</span><span>时间：${esc(item.created_at||'')}${rb&&item.rolled_back_at?' · 回滚时间：'+esc(item.rolled_back_at):''}</span><div class="item-actions"><button class="link-btn" onclick="copyText('${esc(item.import_id||'')}')">复制ID</button>${rb?'<span class="rolled-text">已回滚，不可重复回滚</span>':`<button class="link-btn danger" onclick="rollbackImport('${esc(item.import_id||'')}')">回滚</button>`}</div></div>`; }).join('') || '<div class="hint">暂无导入记录。</div>'; }
  try{ renderImports=renderImportsV055; }catch(e){} window.renderImports=renderImportsV055;

  let whitelist={enabled:false, users:[]};
  async function wlApi(path, opt){ return api(path,opt); }
  function renderWhitelist(){ const list=$id('whitelistList'), msg=$id('whitelistMsg'), tog=$id('whitelistStrictToggle'); if(tog) tog.checked=!!whitelist.enabled; if(msg) msg.textContent=whitelist.enabled?'白名单强校验已启用。未列入的会话将无法使用敏感命令。':'白名单强校验未启用。当前不限制命令用户。'; if(!list) return; list.innerHTML=(whitelist.users||[]).map(u=>`<div class="list-item whitelist-item"><strong>${esc(u)}</strong><span>${/^\d+$/.test(u)?'QQ / 群号':'origin / 自定义标识'}</span><div class="item-actions"><button class="link-btn danger" onclick="removeWhitelistUser('${esc(u)}')">移除</button></div></div>`).join('') || '<div class="hint">暂无白名单条目。可添加完整 origin、用户 QQ 或群号。</div>'; }
  async function loadWhitelist(){ try{ const data=await wlApi('/api/allowed-users'); whitelist={enabled:!!data.enabled, users:data.users||[]}; } catch(e){ whitelist={enabled: JSON.parse(localStorage.getItem('memory_bridge_whitelist_enabled')||'false'), users: JSON.parse(localStorage.getItem('memory_bridge_whitelist_users')||'[]')}; const m=$id('whitelistMsg'); if(m) m.textContent='后端白名单 API 不可用，当前仅本地显示：'+e.message; } renderWhitelist(); }
  async function saveWhitelist(){ try{ const tog=$id('whitelistStrictToggle'); whitelist.enabled=!!(tog&&tog.checked); const data=await wlApi('/api/allowed-users',{method:'POST',body:JSON.stringify(whitelist)}); whitelist={enabled:!!data.enabled, users:data.users||[]}; renderWhitelist(); alert('白名单已保存'); } catch(e){ localStorage.setItem('memory_bridge_whitelist_enabled', JSON.stringify(whitelist.enabled)); localStorage.setItem('memory_bridge_whitelist_users', JSON.stringify(whitelist.users)); renderWhitelist(); alert('保存到后端失败，已临时保存到浏览器：'+e.message); } }
  window.removeWhitelistUser=function(u){ whitelist.users=(whitelist.users||[]).filter(x=>x!==u); renderWhitelist(); };
  function bind(){
    ['sessionSelect','importSessionSelect','importFileSelect'].forEach(id=>{ const el=$id(id); if(el){ el.removeAttribute('size'); el.classList.add('compact-select'); } });
    const s=$id('sessionSearch'); if(s&&!s.dataset.v055){s.dataset.v055='1';s.addEventListener('input',()=>{fillSelect($id('sessionSelect'),listFor('trigger'),getSel('trigger'));renderCards('trigger');});}
    const is=$id('importSessionSearch'); if(is&&!is.dataset.v055){is.dataset.v055='1';is.addEventListener('input',()=>{fillSelect($id('importSessionSelect'),listFor('import'),getSel('import'));renderCards('import');});}
    const f=$id('importFileSelect'); if(f&&!f.dataset.v055){f.dataset.v055='1';f.addEventListener('change',()=>{setSelectedFile(f.value);renderFileCards();});}
    const add=$id('whitelistAddBtn'); if(add&&!add.dataset.v055){add.dataset.v055='1'; add.addEventListener('click',()=>{const input=$id('whitelistInput'); const v=(input&&input.value||'').trim(); if(!v)return; if(!whitelist.users.includes(v)) whitelist.users.push(v); if(input) input.value=''; renderWhitelist();});}
    const save=$id('whitelistSaveBtn'); if(save&&!save.dataset.v055){save.dataset.v055='1'; save.addEventListener('click',saveWhitelist);}
    const clear=$id('whitelistClearBtn'); if(clear&&!clear.dataset.v055){clear.dataset.v055='1'; clear.addEventListener('click',()=>{if(confirm('确认清空白名单？')){whitelist.users=[];renderWhitelist();}});}
    const tog=$id('whitelistStrictToggle'); if(tog&&!tog.dataset.v055){tog.dataset.v055='1'; tog.addEventListener('change',()=>{whitelist.enabled=!!tog.checked;renderWhitelist();});}
  }
  const oldRefresh = window.refreshAll;
  window.refreshAll = async function(){ const r = oldRefresh ? await oldRefresh.apply(this, arguments) : undefined; bind(); try{await loadAliases();}catch(e){} try{renderSessionsV055(sessions(), rawSessions());}catch(e){} try{renderImportOptionsV055();}catch(e){} try{loadWhitelist();}catch(e){} return r; };
  bind(); setTimeout(async ()=>{bind(); try{await loadAliases();}catch(e){} try{renderSessionsV055(sessions(), rawSessions());}catch(e){} try{renderImportOptionsV055();}catch(e){} loadWhitelist();}, 300);
})();
