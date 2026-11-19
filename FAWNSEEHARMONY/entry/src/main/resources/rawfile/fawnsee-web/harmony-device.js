/* The reminder list is re-rendered by the shared Android UI after each cloud operation. */
(() => {
  const screen = '[data-native-screen="reminders"]';
  const make = (tag, classes, text = '') => {
    const element = document.createElement(tag);
    element.className = classes;
    element.textContent = text;
    return element;
  };

  function attach() {
    if (location.hash !== '#today/reminders') return;
    const toolbar = document.querySelector(screen + ' [data-reminder-toolbar]');
    if (!toolbar || toolbar.parentElement.querySelector('[data-harmony-calendar]')) return;
    const card = make('section', 'reminder-card tactile-card bg-surface-container-lowest border border-surface-container rounded-2xl p-3.5');
    card.dataset.harmonyCalendar = '';
    const row = make('div', 'flex items-center justify-between gap-3');
    const label = make('strong', '', '系统日历自动提醒');
    const toggle = make('button', 't-switch');
    toggle.type = 'button';
    toggle.setAttribute('role', 'switch');
    toggle.setAttribute('aria-label', '系统日历自动提醒');
    toggle.appendChild(make('span', ''));
    const hint = make('small', 't-reminder-time', '');
    hint.setAttribute('aria-live', 'polite');
    row.append(label, toggle);
    card.append(row, hint);
    toolbar.after(card);
    let enabled = false;
    const native = (action, params) => window.FAWNSEE_MOBILE.native(action, params);

    function paint(value) {
      enabled = value === true;
      toggle.classList.toggle('on', enabled);
      toggle.setAttribute('aria-checked', String(enabled));
      hint.textContent = enabled ? '已将本账号的提醒同步到手机日历。' : '开启后可通过手机日历接收凡思提醒。';
    }
    async function read() {
      const value = await native('native.calendar.status', {});
      if (card.isConnected) paint(value.enabled);
    }
    toggle.disabled = true;
    read().catch(error => { hint.textContent = error.message || '未能读取日历设置。'; })
      .finally(() => { if (card.isConnected) toggle.disabled = false; });
    toggle.addEventListener('click', async () => {
      if (toggle.disabled || !card.isConnected) return;
      toggle.disabled = true;
      try {
        const value = await native('native.calendar.set', { enabled: !enabled });
        if (card.isConnected) paint(value.enabled);
      } catch (error) {
        try { await read(); } catch {}
        if (card.isConnected) hint.textContent = error.message || '日历设置未完成，请重试。';
      } finally { if (card.isConnected) toggle.disabled = false; }
    });
  }
  function start() {
    const view = document.getElementById('view');
    if (!view) return;
    attach();
    new MutationObserver(attach).observe(view, { childList: true, subtree: true });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, { once: true });
  else start();
})();
