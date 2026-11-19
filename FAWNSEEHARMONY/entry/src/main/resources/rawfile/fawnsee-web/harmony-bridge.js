(() => {
  'use strict';
  const entry = 'resource://rawfile/fawnsee-web/index.html';
  if (window !== top || location.href.split('#')[0] !== entry) return;
  const queue = [];
  const chunks = new Map();
  let key = '';
  let pendingSize = 0;
  function transmit(message) {
    window.FawnseeArkTransport.postMessage(key, message);
  }
  Object.defineProperty(window, '__activateFawnseeBridge', {
    value(token) {
      if (!token || typeof token !== 'string' || key) return;
      key = token;
      for (const message of queue.splice(0)) transmit(message);
      pendingSize = 0;
    }
  });
  Object.defineProperty(window, '__fawnseeInbox', {
    value: Object.freeze({
      append(id, part) { if (!chunks.has(id)) chunks.set(id, []); chunks.get(id).push(part); },
      finish(id) { const parts = chunks.get(id); chunks.delete(id); if (parts) window.FAWNSEE_NATIVE?.receive(parts.join('')); },
      clear(id) { chunks.delete(id); }
    })
  });
  Object.defineProperty(window, 'AndroidBridge', {
    value: Object.freeze({ postMessage(message) {
      const request = JSON.parse(message);
      const app = window.FAWNSEE_MOBILE;
      if (typeof app?.scopeId === 'string') {
        request.scope_id = app.scopeId;
        request.session_epoch = app.scopeEpoch;
      }
      const serialized = JSON.stringify(request);
      if (key) { transmit(serialized); return; }
      if (queue.length >= 32 || pendingSize + serialized.length > 24 * 1024 * 1024) {
        window.FAWNSEE_NATIVE?.receive({ id: request.id, ok: false, error: { code: 'busy', message: '正在打开凡思，请稍后重试。' } });
        return;
      }
      queue.push(serialized);
      pendingSize += serialized.length;
      window.FawnseeArkTransport?.ready();
    } })
  });
  window.FawnseeArkTransport?.ready();
})();
