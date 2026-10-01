/* Bounded SSE transport. A completed event, not a socket close, ends a turn. */
(function (root) {
  'use strict';
  root.FosChatStream = {
    create({onEvent, onAbort, idleMs = 45000, totalMs = 270000, fetchImpl = root.fetch} = {}) {
      const controller = new AbortController();
      let reader, idleTimer, totalTimer, failure, rejectAbort;
      const aborted = new Promise((_, reject) => { rejectAbort = reject; });
      // A click can cancel between construction and the first awaited operation.
      aborted.catch(() => {});
      function abort(reason = '已停止接收页面数据；后台任务状态可恢复。') {
        if (controller.signal.aborted) return;
        failure = new Error(reason);
        controller.abort(failure);
        rejectAbort(failure);
        // pagehide may freeze Promise callbacks until the page is restored.
        // Reset visible state synchronously, before entering the browser cache.
        if (onAbort) onAbort(failure);
        if (reader) Promise.resolve(reader.cancel()).catch(() => {});
      }
      function touch() {
        clearTimeout(idleTimer);
        idleTimer = setTimeout(() => abort('服务超过 45 秒没有响应；正在恢复后台回答状态。'), idleMs);
      }
      async function run(url, options) {
        touch();
        totalTimer = setTimeout(() => abort('页面连接等待时间过长，正在恢复后台回答状态。'), totalMs);
        try {
          const response = await Promise.race([fetchImpl(url, {...options, signal: controller.signal}), aborted]);
          if (!response.ok) throw new Error('请求失败（HTTP ' + response.status + '），本次回答未完成。');
          if (!(response.headers.get('content-type') || '').includes('text/event-stream')) {
            throw new Error('未收到问答数据流，请确认登录状态后重试。');
          }
          if (!response.body) throw new Error('服务未返回数据流，本次回答未完成。');
          reader = response.body.getReader();
          const decoder = new TextDecoder();
          let buffer = '';
          while (true) {
            const {value, done} = await Promise.race([reader.read(), aborted]);
            if (failure) throw failure;
            if (done) throw new Error('连接在回答完成前中断；已显示的内容可能不完整，请重试。');
            touch();
            buffer += decoder.decode(value, {stream: true});
            let separator;
            while ((separator = /\r?\n\r?\n/.exec(buffer))) {
              const frame = buffer.slice(0, separator.index);
              buffer = buffer.slice(separator.index + separator[0].length);
              let type = '';
              const lines = [];
              frame.split(/\r?\n/).forEach(line => {
                if (line.startsWith('event:')) type = line.slice(6).trim();
                if (line.startsWith('data:')) lines.push(line.slice(5).trimStart());
              });
              if (type === 'done') return;
              if (!type) continue;
              let payload;
              try { payload = JSON.parse(lines.join('\n') || '{}'); }
              catch (_) { throw new Error('问答数据不完整，请重新提问。'); }
              if (type === 'error') throw new Error(payload.message || '服务返回错误，本次回答未完成。');
              if (onEvent) onEvent(type, payload);
            }
          }
        } catch (error) {
          throw failure || error;
        } finally {
          clearTimeout(idleTimer);
          clearTimeout(totalTimer);
          if (reader) {
            Promise.resolve(reader.cancel()).catch(() => {});
            try { reader.releaseLock(); } catch (_) { /* Pending cancelled read. */ }
          }
        }
      }
      return {run, abort, signal: controller.signal};
    }
  };
})(globalThis);
