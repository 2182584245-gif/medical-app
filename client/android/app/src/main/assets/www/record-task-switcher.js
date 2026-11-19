/* Recoverable record drafts in the current conversation. */
(function (root) {
  'use strict';

  const statuses = {needs_input: '待补信息', ready: '待确认', conflict: '待核对'};
  const categories = {diet: '饮食', water: '饮水', activity: '运动', sleep: '睡眠', environment: '居住环境', medical: '医疗检查', metric: '身体指标'};
  const key = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/;
  const validId = value => typeof value === 'string' && key.test(value);
  const css = `
    html[data-primary-page="ai"] .f-app-header:has([data-record-task-bar]:not([hidden])) {padding-block-start:max(29px,env(safe-area-inset-top,0px))!important}
    .f-record-task-bar {display:flex;position:relative;flex:none;align-items:center;pointer-events:auto}
    .f-record-task-bar[hidden] {display:none!important}
    .f-record-task-entry {position:relative;display:grid;place-items:center;box-sizing:border-box;width:44px;height:44px;padding:0;overflow:visible;background:none;border:0;color:#1c453b;cursor:pointer;font:inherit}
    .f-record-task-entry svg {width:24px;height:24px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
    [data-record-task-open] .f-record-task-hint {position:absolute;z-index:45;bottom:calc(100% - 2px);right:0;min-width:max-content;max-width:none!important;width:max-content;white-space:nowrap!important;padding:4px 8px;background:#FFF2D9;border:1px solid #EACF9A;border-radius:10px 10px 3px 10px;box-shadow:0 3px 8px #9D631116;color:#785319;font:600 10px/1.5 system-ui,sans-serif;letter-spacing:0}
    .f-record-task-hint::after {content:"";position:absolute;right:18px;bottom:-4px;width:6px;height:6px;background:#FFF2D9;border-right:1px solid #EACF9A;border-bottom:1px solid #EACF9A;transform:rotate(45deg)}
    .f-record-task-dialog {box-sizing:border-box;margin:auto auto 0;width:min(100%,460px);max-width:100%;max-height:82dvh;padding:0;border:1px solid #cfdfd5;border-radius:24px 24px 0 0;background:#fdfbf7;color:#233f35;box-shadow:0 -5px 28px #1d392527;font:inherit}
    .f-record-task-dialog::backdrop {background:#172e2260}
    .f-record-task-sheet {display:flex;flex-direction:column;max-height:82dvh;gap:12px;padding:16px 15px max(16px,env(safe-area-inset-bottom));box-sizing:border-box}
    .f-record-task-head {display:flex;align-items:center;justify-content:space-between;gap:10px}
    .f-record-task-head h2 {margin:0;font-size:19px;line-height:1.3;font-weight:750}
    .f-record-task-dismiss,.f-record-task-more {border:1px solid #c4dbcd;border-radius:12px;background:#fff;color:#225346;min-height:44px;padding:6px 12px;font:inherit;font-size:14px;font-weight:700}
    .f-record-task-search {box-sizing:border-box;width:100%;min-height:46px;padding:8px 12px;border:1px solid #bcd1c1;border-radius:12px;background:#fff;color:#233f35;font:inherit;font-size:16px}
    .f-record-task-scroll {overflow:auto;overscroll-behavior:contain;min-height:0;padding:0 2px 7px}
    .f-record-task-row {box-sizing:border-box;width:100%;min-height:64px;display:grid;gap:4px;text-align:left;margin:0 0 8px;padding:10px 12px;border:1px solid #d3e3d8;border-radius:13px;background:#fff;color:#254639;font:inherit}
    .f-record-task-row[aria-current=true] {background:#ebf5ef;border-color:#81b49e}
    .f-record-task-row strong,.f-record-task-preview {overflow-wrap:anywhere;font-size:15px}
    .f-record-task-row small {color:#516b5e;font-size:13px;font-weight:500}
    .f-record-task-preview {color:#385b4a;font-size:14px}
    .f-record-task-status {color:#536c60;font-size:14px;margin:5px 0 9px}
    .f-record-task-dialog .f-rc-confirmation {margin:8px 0}
    .f-record-task-dialog button:focus-visible,.f-record-task-entry:focus-visible {outline:2px solid #267961;outline-offset:2px}
  `;

  let view = null;
  let scope = null;
  const drafts = new Map();
  const capability = {owner: null, value: null, pending: null};

  function element(doc, tag, className, label) {
    const item = doc.createElement(tag);
    item.className = className;
    if (label) item.textContent = label;
    return item;
  }

  function button(doc, className, label, action) {
    const item = element(doc, 'button', className, label);
    item.type = 'button';
    item.addEventListener('click', action);
    return item;
  }

  function current(v) {
    return v === view && scope === v.scope && v.config.isCurrent();
  }

  function task(item) {
    if (!validId(item?.id) || !validId(item?.draft_id) || !Number.isSafeInteger(item.assistant_message_id) || item.assistant_message_id < 1) return null;
    return item;
  }

  function title(row) {
    const custom = typeof row.title === 'string' ? row.title.trim() : '';
    return custom || (categories[row.category] || '') + '记录';
  }

  function meta(row) {
    const category = categories[row.category];
    return [category, row.record_date, row.record_time, statuses[row.status] || '待完成'].filter(Boolean).join(' · ');
  }

  function paintBar(v) {
    if (!current(v)) return;
    const visible = capability.value === true && Number.isSafeInteger(v.total) && v.total > 0;
    v.bar.hidden = !visible;
    v.entry.disabled = !!v.config.disabled;
    if (visible) {
      const selected = v.scope.selected ? '，正在补' + title(v.scope.selected.task) : '';
      v.entry.setAttribute('aria-label', '有待确定的记录，点击查看（' + v.total + '条' + selected + '）');
    }
  }

  // The shared header and conversation finish mounting independently.
  function attachHeader(doc, update) {
    if (view?.doc === doc && update?.isConnected && update.previousElementSibling !== view.bar) {
      update.before(view.bar);
      paintBar(view);
    }
  }

  async function ensureCapability(v) {
    if (!v.scope.owner || capability.pending || capability.value !== null) return;
    const owner = v.scope.owner;
    const request = v.config.api('chat.record_tasks.capability', {});
    capability.pending = request;
    try {
      const result = await request;
      if (capability.pending !== request || capability.owner !== owner) return;
      capability.value = result?.record_tasks === true;
      if (view?.scope.owner === owner) {
        paintBar(view);
        if (capability.value) refresh();
      }
    } catch (_) {
      if (capability.pending === request) capability.value = false;
    } finally {
      if (capability.pending === request) capability.pending = null;
    }
  }

  async function refresh() {
    const v = view;
    if (!v || capability.value !== true) return;
    if (!current(v)) {
      v.recount = true;
      return;
    }
    v.recount = false;
    const request = ++v.countRequest;
    try {
      const packet = await v.config.api('chat.record_tasks.list', {conversation_id: v.scope.conversation, limit: 1});
      if (view !== v || request !== v.countRequest) return;
      if (!current(v)) {
        v.recount = true;
        return;
      }
      v.total = packet.total;
      paintBar(v);
    } catch (_) {
      // Keep the last successful count while the app is offline.
    }
  }

  function mount(doc, config) {
    const composer = doc.querySelector('.ai-composer-panel') || doc.querySelector('[data-primary-chat-input]')?.closest('.fixed');
    if (!config.owner || !Number.isSafeInteger(config.conversation) || !composer) {
      dropView();
      scope = null;
      return;
    }
    if (capability.owner !== config.owner) {
      reset();
      capability.owner = config.owner;
    }
    if (scope?.owner !== config.owner || scope.conversation !== config.conversation) {
      dropView();
      scope = {owner: config.owner, conversation: config.conversation, selected: null};
      drafts.clear();
    }
    const ownsBar = view?.bar.hasAttribute('data-record-task-bar') && view.entry.parentElement === view.bar;
    if (view?.doc !== doc || !ownsBar) {
      dropView();
      if (!doc.querySelector('#f-record-task-style')) {
        const style = element(doc, 'style', '', css);
        style.id = 'f-record-task-style';
        doc.head.append(style);
      }
      const bar = element(doc, 'div', 'f-record-task-bar');
      bar.dataset.recordTaskBar = '';
      bar.hidden = true;
      const entry = button(doc, 'f-record-task-entry', '', () => open(view));
      entry.dataset.recordTaskOpen = '';
      entry.dataset.fontSizeFixed = 'true';
      entry.setAttribute('aria-haspopup', 'dialog');
      entry.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5v5.5m0 3h.01"/></svg>';
      const hint = element(doc, 'span', 'f-record-task-hint', '有待确定的记录，点击查看');
      hint.dataset.fontSizeFixed = 'true';
      entry.append(hint);
      bar.append(entry);
      view = {doc, bar, entry, scope, config, total: null, countRequest: 0, panel: null};
      attachHeader(doc, doc.querySelector('.f-header-actions [data-parent-action="app-update"]'));
      if (capability.value) refresh();
    }
    view.config = config;
    if (view.recount && current(view)) refresh();
    paintBar(view);
    ensureCapability(view);
  }

  function dropView() {
    if (!view) return;
    if (view.panel) tearDownPanel(view);
    if (view.bar.hasAttribute('data-record-task-bar') && view.entry.parentElement === view.bar) view.bar.remove();
    view = null;
  }

  function reset() {
    dropView();
    scope = null;
    drafts.clear();
    Object.assign(capability, {owner: null, value: null, pending: null});
  }

  function tearDownPanel(v) {
    const p = v.panel;
    if (!p) return;
    v.panel = null;
    root.clearTimeout(p.timer);
    if (p.dialog.open) p.dialog.close();
    p.dialog.remove();
  }

  function validPanel(v, p) {
    return current(v) && v.panel === p;
  }

  function open(v) {
    if (!v || !current(v) || v.config.disabled || v.panel) return;
    const doc = v.doc, dialog = element(doc, 'dialog', 'f-record-task-dialog');
    dialog.dataset.recordTaskDialog = '';
    const sheet = element(doc, 'div', 'f-record-task-sheet');
    const header = element(doc, 'header', 'f-record-task-head');
    const name = element(doc, 'h2', '', '待完成记录');
    name.id = 'f-record-task-heading';
    dialog.setAttribute('aria-labelledby', name.id);
    const close = button(doc, 'f-record-task-dismiss', '关闭', () => dialog.close());
    header.append(name, close);
    const input = element(doc, 'input', 'f-record-task-search');
    input.type = 'search';
    input.maxLength = 120;
    input.placeholder = '搜索日期、时间或内容';
    input.setAttribute('aria-label', '搜索待完成记录');
    const content = element(doc, 'div', 'f-record-task-scroll');
    sheet.append(header, input, content); dialog.append(sheet); doc.body.append(dialog);
    const p = v.panel = {dialog, name, close, input, content, items: [], cursor: null, query: '', count: null, seq: 0, error: '', loading: false, focusing: false, detail: null};
    dialog.addEventListener('close', () => {
      if (v.panel === p) tearDownPanel(v);
      if (current(v)) {
        const target = v.bar.hidden ? doc.querySelector('.f-header-actions [data-parent-action="app-update"]') : v.entry;
        target?.focus({preventScroll: true});
      }
    });
    input.addEventListener('input', () => {
      root.clearTimeout(p.timer);
      p.seq++;
      p.query = input.value.trim();
      p.items = [];
      p.cursor = null;
      p.error = '';
      p.loading = true;
      paintList(v, p);
      p.timer = root.setTimeout(() => readPage(v, p), 180);
    });
    dialog.showModal();
    close.focus({preventScroll: true});
    readPage(v, p);
  }

  async function readPage(v, p, more = false) {
    if (!validPanel(v, p) || (more && (!p.cursor || p.loading))) return;
    const seq = ++p.seq;
    p.loading = true;
    p.error = '';
    paintList(v, p);
    const params = {conversation_id: v.scope.conversation, limit: 20};
    if (p.query) params.query = p.query;
    if (more) params.cursor = p.cursor;
    try {
      const packet = await v.config.api('chat.record_tasks.list', params);
      if (!validPanel(v, p) || p.seq !== seq) return;
      if (!Array.isArray(packet?.tasks) || !Number.isSafeInteger(packet.total) || packet.total < 0) throw Error('列表格式无法核对。');
      const selected = new Map((more ? p.items : []).map(item => [item.id, item]));
      for (const raw of packet.tasks) {
        const candidate = task(raw);
        if (candidate) selected.set(candidate.id, candidate);
      }
      p.items = [...selected.values()];
      p.count = packet.total;
      p.active = packet.active_task_id;
      p.cursor = typeof packet.next_cursor === 'string' && packet.next_cursor && packet.next_cursor !== params.cursor ? packet.next_cursor : null;
      if (!p.query) { v.total = packet.total; paintBar(v); }
    } catch (_) {
      if (validPanel(v, p) && p.seq === seq) p.error = '待完成记录暂时无法读取。';
    } finally {
      if (validPanel(v, p) && p.seq === seq) { p.loading = false; paintList(v, p); }
    }
  }

  function paintList(v, p) {
    if (!validPanel(v, p) || p.detail) return;
    const doc = v.doc, list = doc.createDocumentFragment();
    const dupes = new Map();
    const rowKey = item => JSON.stringify([title(item), meta(item), item.preview || '']);
    for (const row of p.items) dupes.set(rowKey(row), (dupes.get(rowKey(row)) || 0) + 1);
    for (const row of p.items) {
      const option = button(doc, 'f-record-task-row', '', () => focus(v, p, row));
      option.dataset.recordTaskId = row.id;
      if (p.active === row.id) option.setAttribute('aria-current', 'true');
      option.disabled = p.focusing;
      option.append(element(doc, 'strong', '', title(row)), element(doc, 'small', '', meta(row)));
      if (typeof row.preview === 'string' && row.preview.trim() && row.preview.trim() !== title(row)) {
        option.append(element(doc, 'span', 'f-record-task-preview', row.preview));
      }
      if (dupes.get(rowKey(row)) > 1) {
        const cue = element(doc, 'small', '', '来源消息 #' + row.assistant_message_id + ' · 草稿 ' + row.id.slice(-8));
        cue.title = row.id;
        option.append(cue);
      }
      list.append(option);
    }
    const status = p.error || (p.loading ? '正在读取…' : p.focusing ? '正在打开草稿…' : !p.items.length ? (p.query ? '没有找到符合的记录。' : '这段对话没有待完成记录。') : '');
    if (status) {
      const info = element(doc, 'p', 'f-record-task-status', status);
      info.setAttribute('role', 'status');
      list.append(info);
    }
    if (p.error) list.append(button(doc, 'f-record-task-more', '重试', () => readPage(v, p)));
    else if (p.cursor && !p.focusing) {
      const more = button(doc, 'f-record-task-more', '加载更多', () => readPage(v, p, true));
      more.disabled = p.loading;
      list.append(more);
    }
    p.content.replaceChildren(list);
  }

  async function focus(v, p, row) {
    if (!validPanel(v, p) || p.focusing || v.config.disabled) return;
    p.focusing = true;
    p.input.disabled = true;
    p.error = '';
    paintList(v, p);
    try {
      const packet = await v.config.api('chat.record_tasks.focus', {conversation_id: v.scope.conversation, task_id: row.id});
      if (!validPanel(v, p)) return;
      const saved = task(packet?.task);
      const [draft] = root.FAWNSEE_RECORD_ASSISTANT?.normalizeDrafts([packet?.draft]) || [];
      if (!saved || saved.id !== row.id || !draft || draft.id !== saved.draft_id) throw Error('草稿身份无法核对。');
      drafts.clear();
      const source = {id: String(saved.assistant_message_id), status: 'sent', record_receipts: [], record_drafts: [draft]};
      const selection = {task: saved, row: source, draft};
      drafts.set(draft.id, selection);
      v.scope.selected = selection;
      p.detail = selection;
      p.name.textContent = title(saved);
      p.close.textContent = '返回对话';
      p.input.hidden = true;
      paintBar(v);
      paintDetail(v, p);
    } catch (_) {
      if (validPanel(v, p)) p.error = '这份草稿暂时打不开，请重试。';
    } finally {
      if (validPanel(v, p)) {
        p.focusing = false;
        p.input.disabled = false;
        if (!p.detail) paintList(v, p);
      }
    }
  }

  function paintDetail(v, p) {
    if (!validPanel(v, p) || !p.detail) return;
    const {row, draft} = p.detail;
    root.FAWNSEE_RECORD_ASSISTANT.installStyle(v.doc);
    const parts = root.FAWNSEE_RECORD_ASSISTANT.card(v.doc, row, draft);
    const wrap = element(v.doc, 'div', '');
    wrap.dataset.aiRecordDrafts = 'true';
    wrap.append(parts.draft);
    if (parts.confirmation) wrap.append(parts.confirmation);
    p.content.replaceChildren(wrap);
  }

  function rowUpdated(row) {
    const v = view;
    if (!v || !current(v) || !v.panel?.detail) return;
    const detail = v.panel.detail;
    if (detail.row !== row) {
      if (!row.record_drafts?.some(item => item.id === detail.draft.id)) return;
      for (const field of ['record_receipts', 'record_action_busy', 'record_action_error']) detail.row[field] = row[field] || null;
    }
    if (row.record_receipts?.length && v.scope.selected === v.panel.detail) v.scope.selected = null;
    paintBar(v);
    paintDetail(v, v.panel);
  }

  function selected(conversation) {
    return scope?.conversation === Number(conversation) ? (scope.selected?.task.id || null) : null;
  }

  function release(conversation, id) {
    if (id && selected(conversation) === id) { scope.selected = null; if (view) paintBar(view); }
  }

  function focused(id, conversation) {
    const item = scope?.conversation === Number(conversation) && drafts.get(id);
    if (!item) return null;
    return {row: item.row, draft: item.draft, receipt: item.row.record_receipts?.find(receipt => receipt.id === id), superseded: false};
  }

  root.FAWNSEE_DRAFT_TASKS = {mount, attachHeader, reset, refresh, selected, release, focused, rowUpdated};
})(globalThis);
