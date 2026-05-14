// v0.6.17 WebUI coordinator
// 最后加载，统一刷新后的小型 UI 收口，减少历史补丁之间互相覆盖。
(function(){
  'use strict';

  const $id = (id) => document.getElementById(id);

  function currentSelected(id, fallback){
    const el = $id(id);
    return (el && el.value) || fallback || '';
  }

  function restoreSelect(id, value){
    const el = $id(id);
    if(!el || !value) return;
    const exists = Array.from(el.options || []).some(option => option.value === value);
    if(exists) el.value = value;
  }

  function normalizeSelects(){
    ['sessionSelect','importSessionSelect','importFileSelect'].forEach(id => {
      const el = $id(id);
      if(!el) return;
      el.removeAttribute('size');
      el.classList.add('compact-select');
    });
  }

  function keepCompositeAvatarsVisible(){
    document.querySelectorAll('.avatar-composite').forEach(el => {
      el.style.overflow = 'visible';
    });
    document.querySelectorAll('.member-avatar').forEach(el => {
      el.style.display = 'grid';
      el.style.visibility = 'visible';
      el.style.opacity = '1';
    });
    document.querySelectorAll('.avatar:not(.avatar-composite), .file-icon.file-avatar:not(.avatar-composite)').forEach(el => {
      el.style.overflow = 'hidden';
    });
  }

  function syncVersionText(){
    const version = ($id('version') && $id('version').textContent) || '';
    const foot = document.querySelector('.side-foot span');
    if(foot && version && /^v/.test(version.trim())) foot.textContent = 'WebUI ' + version.trim();
  }

  function rerenderSessionSurfaces(){
    try{
      if(typeof renderSessionCards === 'function'){
        renderSessionCards('trigger');
        renderSessionCards('import');
      }
      if(typeof renderSessions === 'function' && Array.isArray(window.usableSessions || usableSessions)){
        renderSessions(window.usableSessions || usableSessions, window.allRawSessions || allRawSessions || []);
      }
    }catch(e){}
  }

  async function postRender(options){
    options = options || {};
    const trigger = currentSelected('sessionSelect', window.selectedTriggerSession);
    const importSession = currentSelected('importSessionSelect', window.selectedImportSession);
    const importFile = currentSelected('importFileSelect', window.selectedImportFile);
    normalizeSelects();
    if(options.rerenderSessions) rerenderSessionSurfaces();
    restoreSelect('sessionSelect', trigger);
    restoreSelect('importSessionSelect', importSession);
    restoreSelect('importFileSelect', importFile);
    keepCompositeAvatarsVisible();
    syncVersionText();
    if(options.refreshMemory && typeof window.memoryBridgeLoadMemoryImports === 'function'){
      try{ await window.memoryBridgeLoadMemoryImports(); }catch(e){}
    }
  }

  window.memoryBridgeUiPostRender = postRender;

  if(typeof window.refreshAll === 'function' && !window.refreshAll.__v617_unified){
    const oldRefresh = window.refreshAll;
    const wrapped = async function(){
      const result = await oldRefresh.apply(this, arguments);
      await postRender({rerenderSessions: false, refreshMemory: true});
      return result;
    };
    wrapped.__v617_unified = true;
    window.refreshAll = wrapped;
  }

  document.addEventListener('click', event => {
    const nav = event.target && event.target.closest ? event.target.closest('.nav-item') : null;
    if(nav) setTimeout(() => postRender({rerenderSessions: false, refreshMemory: nav.dataset.page === 'memory'}), 60);
  });

  try{
    const observer = new MutationObserver(() => keepCompositeAvatarsVisible());
    setTimeout(() => observer.observe(document.body, {childList: true, subtree: true}), 300);
  }catch(e){}

  setTimeout(() => postRender({rerenderSessions: false, refreshMemory: false}), 300);
})();
