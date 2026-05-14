// v0.5.6 WebUI patch + v0.6.3 adapter-origin hotfix
// 核心修复：不要把 napcat/aiocqhttp/自定义 adapter_id 当噪声会话过滤。
(function(){
  'use strict';
  const VERSION='0.6.4';
  const $id=(id)=>document.getElementById(id);

  function safeNoisy(origin){
    const text=String(origin||'').trim();
    if(!text || text==='unknown') return true;
    if(/^\d+$/.test(text)) return true;
    if(/^\d+_\d+$/.test(text)) return true;
    return false;
  }
  function safeFilter(list){ return (list||[]).filter(s=>s&&s.origin&&!safeNoisy(s.origin)); }

  try{ isNoisyAdapterSession=safeNoisy; }catch(e){ window.isNoisyAdapterSession=safeNoisy; }
  try{ filterUsableSessions=safeFilter; }catch(e){ window.filterUsableSessions=safeFilter; }

  function originTail(origin){ const parts=String(origin||'').split(':'); return parts[parts.length-1]||''; }
  function parseOrigin(origin){
    const text=String(origin||''), tail=originTail(text);
    let isGroup=/GroupMessage/i.test(text), isFriend=/FriendMessage/i.test(text), userId='', groupId='';
    const m=tail.match(/^(\d+)_(\d+)$/);
    if(m){ userId=m[1]; groupId=m[2]; isGroup=true; }
    else if(/^\d+$/.test(tail)){ if(isGroup) groupId=tail; else { userId=tail; isFriend=true; } }
    return {text, tail, isGroup, isFriend, userId, groupId};
  }
  function goodName(s){
    s=String(s||'').trim();
    return !!s && !/^\d+$/.test(s) && !/^\d+_\d+$/.test(s) && !/^(私聊|群聊)\s*[\d_]*$/.test(s) && !['未知','未知成员','未知会话'].includes(s);
  }
  function allSess(){
    const a=[];
    try{ if(Array.isArray(usableSessions)) a.push(...usableSessions); }catch(e){}
    try{ if(Array.isArray(allRawSessions)) a.push(...allRawSessions); }catch(e){}
    return a.filter(Boolean);
  }
  function profileMap(){
    const map={};
    allSess().forEach(s=>{
      const p=parseOrigin(s.origin); const uid=String(s.sender_id||p.userId||''); if(!uid) return;
      const candidates=[s.sender_name,s.nickname,s.card];
      const dn=String(s.display_name||'');
      if(dn.includes(' · ')) candidates.push(dn.split(' · ').pop()); else candidates.push(dn);
      const name=candidates.find(goodName);
      if(name){ const old=map[uid]&&map[uid].name||''; if(!old || String(name).length>=old.length) map[uid]=Object.assign(map[uid]||{}, {name:String(name)}); }
      if(s.member_avatar_url) map[uid]=Object.assign(map[uid]||{}, {avatar:s.member_avatar_url});
      else if(s.avatar_url && !(s.category==='group'||p.isGroup)) map[uid]=Object.assign(map[uid]||{}, {avatar:s.avatar_url});
      if(!map[uid]) map[uid]={}; map[uid].userId=uid;
    });
    return map;
  }
  function syncOne(s, profiles){
    s=Object.assign({}, s||{}); const p=parseOrigin(s.origin); const uid=String(s.sender_id||p.userId||''); if(!uid) return s;
    const prof=profiles[uid]||{};
    if(goodName(prof.name)){
      if(!goodName(s.sender_name)) s.sender_name=prof.name;
      if(p.isFriend && !goodName(s.display_name)) s.display_name=prof.name;
      if(p.isGroup && (!goodName(s.display_name)||String(s.display_name).endsWith('未知成员'))){
        const group=s.group_name || (p.groupId ? '群聊 '+p.groupId : '群聊');
        s.display_name=group+' · '+prof.name;
      }
    }
    if(prof.avatar){ if(p.isGroup) s.member_avatar_url=s.member_avatar_url||prof.avatar; else s.avatar_url=s.avatar_url||prof.avatar; }
    if(uid){ s.sender_id=s.sender_id||uid; if(!p.isGroup) s.avatar_url=s.avatar_url||`https://q1.qlogo.cn/g?b=qq&nk=${uid}&s=100`; else s.member_avatar_url=s.member_avatar_url||`https://q1.qlogo.cn/g?b=qq&nk=${uid}&s=100`; }
    if(p.groupId){ s.group_id=s.group_id||p.groupId; s.group_avatar_url=s.group_avatar_url||`https://p.qlogo.cn/gh/${p.groupId}/${p.groupId}/100`; if(p.isGroup) s.avatar_url=s.avatar_url||s.group_avatar_url; }
    return s;
  }
  function applyProfileSync(){ try{ const profiles=profileMap(); if(Array.isArray(usableSessions)) usableSessions=usableSessions.map(s=>syncOne(s,profiles)); if(Array.isArray(allRawSessions)) allRawSessions=allRawSessions.map(s=>syncOne(s,profiles)); }catch(e){} }
  function fixAvatarCorners(){
    document.querySelectorAll('.avatar, .file-icon.file-avatar').forEach(el=>{
      el.style.borderRadius='22px';
      if(!el.classList.contains('avatar-composite')) el.style.overflow='hidden'; else el.style.overflow='visible';
      el.querySelectorAll(':scope > img, :scope > .avatar-img').forEach(img=>{ img.style.borderRadius='inherit'; img.style.objectFit='cover'; img.style.width='100%'; img.style.height='100%'; });
    });
  }
  function patchText(){
    try{
      const foot=document.querySelector('.side-foot span'); if(foot) foot.textContent='WebUI v'+VERSION;
      const ver=$id('version'); if(ver && (!ver.textContent || ver.textContent==='v0.6.0')) ver.textContent='v'+VERSION;
      ['manualSession','importManualSession'].forEach(id=>{ const el=$id(id); if(el) el.placeholder='例如 mybot:FriendMessage:3555187509 或 napcat:GroupMessage:用户QQ_群号'; });
      const p=document.querySelector('[data-page-panel="sessions"] .pill'); if(p) p.textContent='仅过滤纯数字/数字_数字空会话，不过滤 napcat/aiocqhttp';
    }catch(e){}
  }

  async function uploadExternalPackage(){
    const input=$id('externalPackageInput'), msg=$id('externalImportMsg'), btn=$id('externalPackageBtn');
    if(!input || !input.files || !input.files[0]){ if(msg) msg.textContent='请先选择一个 zip 记忆包。'; return; }
    const file=input.files[0]; if(!/\.zip$/i.test(file.name)){ if(msg) msg.textContent='只支持 zip 文件。'; return; }
    const fd=new FormData(); fd.append('file', file); if(btn) btn.disabled=true; if(msg) msg.textContent='正在上传并校验记忆包结构...';
    try{
      const res=await fetch('/api/external-import',{method:'POST',headers:(typeof authHeaders==='function'?authHeaders():{}),body:fd});
      const data=await res.json().catch(()=>({}));
      if(!res.ok || !data.ok) throw new Error(data.error || (data.details&&JSON.stringify(data.details.errors||data.details)) || '上传失败');
      const val=data.validation||{};
      if(msg) msg.textContent=`导入成功：${data.file}\n原文件：${data.original_filename||file.name}\n结构：${val.migration_available?'migration 可用':'旧版兼容'}\n警告：${(val.warnings||[]).join('；')||'无'}\n已加入“已生成文件”，并标记为外部导入。`;
      if(typeof refreshAll==='function') await refreshAll();
      try{ if(typeof selectImportFileV055==='function') selectImportFileV055(data.file); else if(typeof selectImportFile==='function') selectImportFile(data.file); }catch(e){}
    }catch(e){ if(msg) msg.textContent='导入失败：'+e.message; } finally{ if(btn) btn.disabled=false; }
  }

  function bind(){
    const btn=$id('externalPackageBtn'); if(btn && !btn.dataset.v056){ btn.dataset.v056='1'; btn.addEventListener('click', uploadExternalPackage); }
    const input=$id('externalPackageInput'); if(input && !input.dataset.v056){ input.dataset.v056='1'; input.addEventListener('change',()=>{ const msg=$id('externalImportMsg'); if(msg) msg.textContent=input.files&&input.files[0]?'已选择：'+input.files[0].name:'请选择一个记忆包 zip。'; }); }
    applyProfileSync(); fixAvatarCorners(); patchText();
  }

  const oldRefresh=window.refreshAll;
  if(typeof oldRefresh==='function' && !oldRefresh.__v063){
    const wrapped=async function(){
      const r=await oldRefresh.apply(this, arguments);
      try{ if(Array.isArray(allRawSessions)) usableSessions=safeFilter(allRawSessions); if(typeof renderSessions==='function') renderSessions(usableSessions, allRawSessions||usableSessions); }catch(e){}
      bind(); return r;
    };
    wrapped.__v063=true; window.refreshAll=wrapped;
  }
  const oldRenderStatus=window.renderStatus;
  if(typeof oldRenderStatus==='function' && !oldRenderStatus.__v063){
    const wrapped=function(status){ status=Object.assign({}, status||{}, {version:(status&&status.version&&status.version!=='0.6.0')?status.version:VERSION}); const r=oldRenderStatus.apply(this,[status]); patchText(); return r; };
    wrapped.__v063=true; window.renderStatus=wrapped;
  }
  try{ const mo=new MutationObserver(()=>fixAvatarCorners()); setTimeout(()=>{mo.observe(document.body,{childList:true,subtree:true}); fixAvatarCorners();},300); }catch(e){}
  bind(); setTimeout(bind,500); setTimeout(bind,1500);
})();
