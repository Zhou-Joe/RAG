/* Reattach to durable message snapshots without starting another model request. */
(function(root) {
  root.FosAnswerRecovery = {
    watch(thread, {onUpdate, onError, fetchImpl = root.fetch, interval = 2000} = {}) {
      let stopped = false, timer, controller;
      async function poll() {
        controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 15000);
        try {
          const response = await fetchImpl('/kb/conv/' + encodeURIComponent(thread) + '/messages/', {signal: controller.signal, cache: 'no-store'});
          if (!response.ok || response.redirected) {
            const error = new Error('读取会话失败（HTTP ' + (response.redirected ? 401 : response.status) + '）');
            error.status = response.redirected ? 401 : response.status;
            throw error;
          }
          const data = await response.json();
          if (stopped) return;
          onUpdate(data);
          if (!stopped && data.active_answer) timer = setTimeout(poll, interval);
        } catch(error) {
          if (!stopped) {
            if (onError) onError(error);
            if (!stopped && ![401,403,404].includes(error.status)) timer = setTimeout(poll, interval);
          }
        } finally { clearTimeout(timeout); }
      }
      poll();
      return {stop() { stopped = true; clearTimeout(timer); if (controller) controller.abort(); }};
    },
    remember(key, thread) { try { sessionStorage.setItem(key, thread); } catch (_) {} },
    recalled(key) { try { return sessionStorage.getItem(key) || ''; } catch (_) { return ''; } },
    async stop(thread, csrf) {
      const response = await fetch('/kb/conv/' + encodeURIComponent(thread) + '/stop/', {method:'POST', headers:{'X-CSRFToken':csrf}});
      if (!response.ok) throw new Error('停止失败（HTTP ' + response.status + '）');
      return response.json();
    }
  };
})(globalThis);
