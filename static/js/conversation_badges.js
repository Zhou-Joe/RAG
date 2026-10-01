/* Read cursors acknowledge only the completion visible when the user clicks. */
(function(root) {
  root.FosConversationBadges = {
    create({csrf, onState, fetchImpl = root.fetch, interval = 2000}) {
      let paused = true, epoch = 0, timer, controller;
      const states = new Map(), readThrough = new Map();
      function display(row) {
        const acknowledged = readThrough.get(row.thread_id) || 0;
        onState({...row, unread:row.unread && row.answer_id > acknowledged});
      }
      async function poll(version) {
        if (paused || version !== epoch) return;
        controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 15000);
        try {
          const response = await fetchImpl('/kb/conv/states/', {cache:'no-store',signal:controller.signal});
          if (!response.ok) throw new Error('会话状态读取失败');
          const data = await response.json();
          if (paused || version !== epoch) return;
          for (const row of data.conversations) { states.set(row.thread_id,row); display(row); }
        } catch (_) { /* Keep the previous badge and retry; never fake completion. */ }
        finally {
          clearTimeout(timeout);
          if (!paused && version === epoch) timer = setTimeout(() => poll(version),interval);
        }
      }
      return {
        start() { if (!paused) return; paused = false; poll(++epoch); },
        pause() { paused = true; epoch++; clearTimeout(timer); if (controller) controller.abort(); },
        async read(thread) {
          const row = states.get(thread);
          if (!row || !row.unread || !row.answer_id) return false;
          const answerId = row.answer_id;
          try {
            const response = await fetchImpl('/kb/conv/' + encodeURIComponent(thread) + '/read/', {
              method:'POST', headers:{'X-CSRFToken':csrf(),'Content-Type':'application/x-www-form-urlencoded'},
              body:new URLSearchParams({answer_id:String(answerId)}).toString(),
            });
            if (!response.ok) return false;
            readThrough.set(thread, Math.max(readThrough.get(thread) || 0,answerId));
            if (!paused) display(states.get(thread) || row);
            return true;
          } catch (_) { return false; }
        }
      };
    }
  };
})(globalThis);
