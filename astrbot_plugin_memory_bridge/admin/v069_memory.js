// v0.6.9 长期记忆注入管理页
(function(){
  'use strict';

  const $id = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
  let memoryImports = [];
  let selectedImportId = '';
  let chunkKeyword = '';
  let chunkStatus = 'all';
  let chunkSearchComposing = false;
  let chunkPreview = null;

  function headers(){
    try { return typeof authHeaders === 'function' ? authHeaders() : {}; } catch(e) { return {}; }
  }

  async function requestJson(path, options={}){
    const h = Object.assign({}, headers(), options.headers || {});
    if(options.body && !h['Content-Type']) h['Content-Type'] = 'application/json';
    const res = await fetch(path, Object.assign({}, options, {headers: h}));
    const data = await res.json().catch(() => ({}));
    if(!res.ok || data.ok === false) throw new Error(data.error || data.message || '请求失败');
    return data;
  }

  function importText(item){
    return [
      item.import_id, item.mode, item.status, item.source_file,
      item.source_origin, item.target_origin, item.source_label, item.target_label,
      (item.items || []).map(x => x.preview).join('\n')
    ].join('\n').toLowerCase();
  }

  function filteredImports(){
    const keyword = String($id('memorySearch')?.value || '').trim().toLowerCase();
    const filter = String($id('memoryStatusFilter')?.value || 'all');
    return (memoryImports || []).filter(item => {
      if(filter === 'enabled' && !item.enabled) return false;
      if(filter === 'disabled' && item.enabled) return false;
      if(keyword && !importText(item).includes(keyword)) return false;
      return true;
    });
  }

  function renderSummary(data){
    const box = $id('memoryInjectSummary');
    if(!box) return;
    const imports = data.imports || [];
    const enabled = imports.filter(x => x.enabled).length;
    const chunks = imports.reduce((sum, x) => sum + Number(x.chunks_total || 0), 0);
    const enabledChunks = imports.reduce((sum, x) => sum + Number(x.chunks_enabled || 0), 0);
    box.innerHTML = [
      `<span>导入记录 ${imports.length}</span>`,
      `<span>启用 ${enabled}</span>`,
      `<span>chunks ${enabledChunks}/${chunks}</span>`,
      `<span>最近注入 ${((data.recent_injections || [])[0] || {}).time || '无'}</span>`
    ].join('');
  }

  function renderImports(){
    const box = $id('memoryImportsList');
    if(!box) return;
    const list = filteredImports();
    if(!selectedImportId && list[0]) selectedImportId = list[0].import_id || '';
    box.innerHTML = list.map(item => {
      const active = selectedImportId === item.import_id;
      const enabled = !!item.enabled;
      const statusText = enabled ? '启用' : '禁用';
      const target = item.target_label || item.target_origin || '-';
      const source = item.source_label || item.source_origin || '-';
      const q = item.quality || {};
      return `<article class="memory-import-card ${enabled?'':'disabled'} ${active?'active':''}">
        <div class="memory-import-head">
          <div>
            <strong>${esc(item.import_id || '-')}</strong>
            <span class="memory-badge ${enabled?'enabled':'disabled'}">${statusText}</span>
          </div>
          <button class="link-btn toggle-btn" onclick="memoryBridgeSelectImport('${esc(item.import_id || '')}')">查看</button>
        </div>
        <p>目标：${esc(target)}<br>来源：${esc(source)}<br>文件：${esc(item.source_file || '')}</p>
        <div class="memory-stat-line">
          <span>${esc(item.mode || '-')}</span>
          <span>chunks ${esc(item.chunks_enabled ?? 0)}/${esc(item.chunks_total ?? 0)}${item.chunks_synthetic ? ' · card拆分' : ''}</span>
          <span>card ${item.card_enabled ? 'on' : 'off'}</span>
          <span>质量 ${esc(q.quality_level || '-')}</span>
          <span>${esc(item.skip_reason || 'ok')}</span>
        </div>
        <div class="item-actions">
          <button class="link-btn" onclick="memoryBridgeSelectImport('${esc(item.import_id || '')}')">分条管理</button>
          <button class="link-btn ${enabled?'danger':''}" onclick="memoryBridgeToggleImport('${esc(item.import_id || '')}', ${enabled ? 'false' : 'true'})">${enabled ? '禁用整条' : '启用整条'}</button>
        </div>
      </article>`;
    }).join('') || '<div class="hint">暂无可管理的注入记忆。</div>';
    renderDetail();
  }

  function renderDetail(){
    const box = $id('memoryDetailBox');
    if(!box) return;
    const item = (memoryImports || []).find(x => x.import_id === selectedImportId);
    if(!item){
      box.textContent = '选择一条导入记忆查看分条内容。';
      return;
    }
    const filteredItems = (item.items || []).filter(entry => {
      if(chunkStatus === 'enabled' && !entry.enabled) return false;
      if(chunkStatus === 'disabled' && entry.enabled) return false;
      const hay = [entry.id, entry.kind, entry.category, (entry.tags || []).join(' '), entry.preview].join('\n').toLowerCase();
      if(chunkKeyword && !hay.includes(chunkKeyword.toLowerCase())) return false;
      return true;
    });
    const previewBox = chunkPreview && chunkPreview.import_id === item.import_id ? renderChunkPreviewBox(chunkPreview) : '';
    const rows = filteredItems.map(entry => {
      const enabled = !!entry.enabled;
      const title = entry.kind === 'memory_card' ? 'memory_card 摘要' : `${entry.id} · ${entry.category || 'fact'}`;
      const tags = Array.isArray(entry.tags) && entry.tags.length ? ` · ${entry.tags.join(', ')}` : '';
      const synthetic = entry.synthetic ? ' · card拆分' : '';
      return `<div class="memory-item ${enabled?'':'off'}" onclick="memoryBridgePreviewChunk('${esc(item.import_id || '')}', '${esc(entry.id || '')}')">
        <div class="memory-item-head">
          <strong>${esc(title)}</strong>
          <span class="memory-item-actions">
            <button class="link-btn toggle-btn" onclick="event.stopPropagation();memoryBridgePreviewChunk('${esc(item.import_id || '')}', '${esc(entry.id || '')}')">预览</button>
            <button class="link-btn toggle-btn ${enabled?'danger':''}" onclick="event.stopPropagation();memoryBridgeToggleItem('${esc(item.import_id || '')}', '${esc(entry.id || '')}', ${enabled ? 'false' : 'true'})">${enabled ? '禁用' : '启用'}</button>
          </span>
        </div>
        <div class="memory-stat-line"><span>${esc(entry.kind)}${synthetic}</span><span>${esc(entry.chars || 0)} 字</span><span>${enabled ? '参与注入' : '已排除'}${esc(tags)}</span></div>
        <p>${esc(entry.preview || '')}</p>
      </div>`;
    }).join('') || '<div class="hint">没有匹配的记忆条目。</div>';
    box.innerHTML = `<h3>${esc(item.import_id)}</h3>
      <div class="kv"><b>状态</b><span>${item.enabled ? '启用' : '禁用'} · ${esc(item.status || '')}</span></div>
      <div class="kv"><b>chunks</b><span>${esc(item.chunks_enabled ?? 0)} / ${esc(item.chunks_total ?? 0)}${item.chunks_synthetic ? ' · 原包无 chunks，已从 memory_card 自动拆分' : ''}</span></div>
      <div class="kv"><b>质量</b><span>${esc((item.quality || {}).quality_level || '-')} · 原生 chunks ${esc((item.quality || {}).native_chunks_count ?? 0)} · card 拆分 ${esc((item.quality || {}).synthetic_chunks_count ?? 0)} · memory_card ${esc((item.quality || {}).memory_card_chars ?? 0)} 字</span></div>
      <div class="kv"><b>目标</b><span>${esc(item.target_origin || '-')}</span></div>
      <div class="kv"><b>来源</b><span>${esc(item.source_origin || '-')}</span></div>
      <div class="kv"><b>文件</b><span>${esc(item.source_file || '-')}</span></div>
      <h4>分条管理</h4>
      <div class="memory-chunk-toolbar">
        <input id="memoryChunkSearch" class="search-input" value="${esc(chunkKeyword)}" placeholder="搜索当前导入的 chunk 关键词 / ID / 标签" />
        <select id="memoryChunkStatusFilter" onchange="memoryBridgeSetChunkFilter(this.value)">
          <option value="all" ${chunkStatus==='all'?'selected':''}>全部条目</option>
          <option value="enabled" ${chunkStatus==='enabled'?'selected':''}>仅启用</option>
          <option value="disabled" ${chunkStatus==='disabled'?'selected':''}>仅禁用</option>
        </select>
        <span>${esc(filteredItems.length)} / ${esc((item.items || []).length)} 条</span>
      </div>
      ${previewBox}
      <div class="memory-item-list">${rows}</div>`;
    bindChunkSearchInput();
    syncTestOrigin();
  }

  function renderChunkPreviewBox(detail){
    const item = detail.item || {};
    const meta = [
      item.kind || 'chunk',
      item.category || '',
      item.synthetic ? 'card拆分' : '',
      item.enabled ? '参与注入' : '已排除',
      item.chunk_policy || '',
      item.source ? 'source=' + item.source : '',
      Array.isArray(item.source_indices) && item.source_indices.length ? 'indices=' + item.source_indices.join(',') : '',
    ].filter(Boolean).join(' · ');
    const tags = Array.isArray(item.tags) && item.tags.length ? `<div class="memory-preview-tags">${item.tags.map(x => `<span>${esc(x)}</span>`).join('')}</div>` : '';
    return `<section class="memory-chunk-preview">
      <div class="memory-preview-head">
        <div>
          <h4>${esc(item.id || 'chunk')}</h4>
          <p>${esc(meta)} · ${esc(item.chars || 0)} 字</p>
        </div>
        <button class="link-btn" onclick="memoryBridgeCloseChunkPreview()">收起</button>
      </div>
      ${tags}
      <pre>${esc(item.text || item.preview || '')}</pre>
    </section>`;
  }

  function selectedImport(){
    return (memoryImports || []).find(x => x.import_id === selectedImportId) || null;
  }

  function syncTestOrigin(){
    const input = $id('memoryTestOrigin');
    const item = selectedImport();
    if(input && item && !input.value){
      input.placeholder = '默认：' + (item.target_origin || '当前选中导入记录目标 origin');
    }
  }

  async function loadMemoryImports(){
    const summary = $id('memoryInjectSummary');
    if(summary) summary.textContent = '正在加载注入记忆...';
    try{
      const data = await requestJson('/api/memory-injection/imports');
      memoryImports = data.imports || [];
      renderSummary(data);
      renderImports();
    }catch(e){
      if(summary) summary.textContent = '加载失败：' + e.message;
      const box = $id('memoryImportsList');
      if(box) box.innerHTML = '<div class="hint">加载失败。</div>';
    }
  }

  window.memoryBridgeLoadMemoryImports = loadMemoryImports;

  window.memoryBridgeSelectImport = function(importId){
    selectedImportId = importId;
    chunkKeyword = '';
    chunkStatus = 'all';
    chunkPreview = null;
    renderImports();
    try{ if(typeof switchPage === 'function') switchPage('memory'); }catch(e){}
  };

  window.memoryBridgeSetChunkSearch = function(value){
    if(chunkSearchComposing) return;
    chunkKeyword = String(value || '');
    renderDetail();
    const input = $id('memoryChunkSearch');
    if(input){
      input.focus();
      const end = input.value.length;
      try{ input.setSelectionRange(end, end); }catch(e){}
    }
  };

  window.memoryBridgeSetChunkFilter = function(value){
    chunkStatus = String(value || 'all');
    renderDetail();
  };

  window.memoryBridgeCloseChunkPreview = function(){
    chunkPreview = null;
    renderDetail();
  };

  window.memoryBridgePreviewChunk = async function(importId, itemId){
    const box = $id('memoryDetailBox');
    try{
      const data = await requestJson('/api/memory-injection/imports/' + encodeURIComponent(importId) + '/items/' + encodeURIComponent(itemId));
      chunkPreview = data;
      renderDetail();
      const preview = document.querySelector('.memory-chunk-preview');
      if(preview) preview.scrollIntoView({block:'nearest', behavior:'smooth'});
    }catch(e){
      if(box){
        const old = box.querySelector('.memory-chunk-preview-error');
        if(old) old.remove();
        box.insertAdjacentHTML('afterbegin', `<div class="memory-chunk-preview-error">读取 chunk 详情失败：${esc(e.message)}</div>`);
      }else{
        alert('读取 chunk 详情失败：' + e.message);
      }
    }
  };

  async function runMemoryTest(){
    const resultBox = $id('memoryTestResult');
    const item = selectedImport();
    const origin = String($id('memoryTestOrigin')?.value || item?.target_origin || '').trim();
    const query = String($id('memoryTestQuery')?.value || '').trim();
    if(!origin){
      if(resultBox) resultBox.textContent = '缺少目标 origin。请先选择一条导入记录，或手动填写 origin。';
      return;
    }
    if(!query){
      if(resultBox) resultBox.textContent = '请输入测试问题。';
      return;
    }
    if(resultBox) resultBox.textContent = '正在检索长期记忆...';
    try{
      const res = await fetch('/api/memory-injection/test?origin=' + encodeURIComponent(origin) + '&query=' + encodeURIComponent(query), {headers: headers()});
      const data = await res.json().catch(() => ({}));
      const ok = !!data.ok;
      const meta = Object.assign({}, data);
      delete meta.context_preview;
      delete meta.context;
      const status = ok ? '命中可注入记忆' : '未命中：' + (data.reason || data.error || 'unknown');
      const lines = [
        `状态：${status}`,
        `origin：${origin}`,
        `query：${query}`,
        `imports：${(data.imports || []).join(', ') || '-'}`,
        `chunks：${data.chunks ?? 0} / total ${data.chunks_total ?? 0}`,
        `fallback：${data.fallback_used ? 'true' : 'false'}${data.fallback_cards_count !== undefined ? ' · cards ' + data.fallback_cards_count : ''}`,
        `keywords：${(data.keywords || []).join(', ') || '-'}`,
      ];
      if(resultBox){
        resultBox.innerHTML = esc(lines.join('\n')) + '<h4>诊断字段</h4><pre>' + esc(JSON.stringify(meta, null, 2)) + '</pre><h4>最终注入预览</h4><pre>' + esc(data.context_preview || data.context || '无注入内容') + '</pre>';
      }
    }catch(e){
      if(resultBox) resultBox.textContent = '检索失败：' + e.message;
    }
  }

  function qualityHtml(q){
    q = q || {};
    const warnings = (q.warnings || []).map(x => `<li>${esc(x)}</li>`).join('') || '<li>无</li>';
    const notes = (q.notes || []).map(x => `<li>${esc(x)}</li>`).join('') || '<li>无</li>';
    return `<h4>导入包质量诊断</h4>
      <div class="kv"><b>质量等级</b><span>${esc(q.quality_level || '-')}</span></div>
      <div class="kv"><b>memory_card</b><span>${q.has_memory_card ? '存在' : '缺失'} · ${esc(q.memory_card_chars ?? 0)} 字</span></div>
      <div class="kv"><b>chunks</b><span>原生 ${esc(q.native_chunks_count ?? 0)} · 加载 ${esc(q.loaded_chunks_count ?? 0)} · card 拆分 ${esc(q.synthetic_chunks_count ?? 0)}</span></div>
      <div class="kv"><b>seed/facts</b><span>seed ${esc(q.seed_count ?? 0)} · extracted_facts ${esc(q.extracted_facts_count ?? 0)} · kept ${esc(q.kept_messages_count ?? 0)}</span></div>
      <h4>质量提示</h4><ul>${notes}</ul><h4>质量警告</h4><ul>${warnings}</ul>`;
  }

  async function previewImportWithQuality(){
    const select = $id('importFileSelect');
    const box = $id('importBox');
    const filename = select ? select.value : '';
    if(filename){
      try{ selectedImportFile = filename; rememberSelections(); }catch(e){}
    }
    if(!filename){
      if(box) box.textContent = '暂无可预览 clean zip。';
      return;
    }
    if(box) box.textContent = '正在读取导入预览...';
    try{
      const data = await requestJson('/api/import-preview?filename=' + encodeURIComponent(filename));
      const warnings = (data.warnings || []).map(warning => `<li>${esc(warning)}</li>`).join('') || '<li>无</li>';
      if(box){
        box.innerHTML = `<h3>导入预览：${esc(data.file)}</h3>
          <div class="kv"><b>来源用户</b><span>${esc(data.source_label || data.source_origin || '未知来源')}</span></div>
          <div class="kv"><b>来源 origin</b><span>${esc(data.source_origin || '')}</span></div>
          <div class="kv"><b>原始/保留</b><span>${esc((data.stats && data.stats.raw_count) || '?')} / ${esc((data.stats && data.stats.kept_count) || '?')}</span></div>
          <div class="kv"><b>Seed 消息</b><span>${esc(data.seed_count)}</span></div>
          <div class="kv"><b>Chunks</b><span>${esc(data.chunks_count)}</span></div>
          ${qualityHtml(data.quality || {})}
          <h4>导入警告</h4><ul>${warnings}</ul>
          <h4>记忆卡预览</h4><pre>${esc(data.memory_card_preview || '')}</pre>`;
      }
    }catch(e){
      if(box) box.textContent = '导入预览失败：' + e.message;
    }
  }

  function bindChunkSearchInput(){
    const input = $id('memoryChunkSearch');
    if(input && !input.dataset.v612){
      input.dataset.v612 = '1';
      input.addEventListener('compositionstart', () => {
        chunkSearchComposing = true;
      });
      input.addEventListener('compositionend', () => {
        chunkSearchComposing = false;
        window.memoryBridgeSetChunkSearch(input.value);
      });
      input.addEventListener('input', () => {
        if(!chunkSearchComposing) window.memoryBridgeSetChunkSearch(input.value);
      });
    }
  }

  window.memoryBridgeToggleImport = async function(importId, enabled){
    try{
      await requestJson('/api/memory-injection/imports/' + encodeURIComponent(importId) + '/enabled', {
        method: 'POST',
        body: JSON.stringify({enabled: !!enabled})
      });
      await loadMemoryImports();
      try{ if(typeof refreshAll === 'function') await refreshAll(); }catch(e){}
    }catch(e){
      alert('操作失败：' + e.message);
    }
  };

  window.memoryBridgeToggleItem = async function(importId, itemId, enabled){
    try{
      await requestJson('/api/memory-injection/imports/' + encodeURIComponent(importId) + '/items/' + encodeURIComponent(itemId) + '/enabled', {
        method: 'POST',
        body: JSON.stringify({enabled: !!enabled})
      });
      await loadMemoryImports();
    }catch(e){
      alert('操作失败：' + e.message);
    }
  };

  function bind(){
    const refresh = $id('memoryRefreshBtn');
    if(refresh && !refresh.dataset.v069){
      refresh.dataset.v069 = '1';
      refresh.addEventListener('click', loadMemoryImports);
    }
    const search = $id('memorySearch');
    if(search && !search.dataset.v069){
      search.dataset.v069 = '1';
      search.addEventListener('input', renderImports);
    }
    const filter = $id('memoryStatusFilter');
    if(filter && !filter.dataset.v069){
      filter.dataset.v069 = '1';
      filter.addEventListener('change', renderImports);
    }
    const testBtn = $id('memoryTestBtn');
    if(testBtn && !testBtn.dataset.v615){
      testBtn.dataset.v615 = '1';
      testBtn.addEventListener('click', runMemoryTest);
    }
    const testQuery = $id('memoryTestQuery');
    if(testQuery && !testQuery.dataset.v615){
      testQuery.dataset.v615 = '1';
      testQuery.addEventListener('keydown', (event) => {
        if(event.key === 'Enter') runMemoryTest();
      });
    }
  }

  window.previewImport = previewImportWithQuality;

  const oldRefreshAll = window.refreshAll;
  if(typeof oldRefreshAll === 'function' && !oldRefreshAll.__v069_memory){
    const wrapped = async function(){
      const result = await oldRefreshAll.apply(this, arguments);
      await loadMemoryImports();
      return result;
    };
    wrapped.__v069_memory = true;
    window.refreshAll = wrapped;
  }

  bind();
  setTimeout(bind, 300);
  setTimeout(loadMemoryImports, 900);
})();
