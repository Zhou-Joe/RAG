/* Keep IME candidate confirmation separate from message submission. */
(function (root) {
  'use strict';
  root.FosChatInput = {
    bind(input, send, now = () => performance.now(), allowSend = () => true) {
      let composing = false;
      let endedAt = -Infinity;
      input.addEventListener('compositionstart', () => { composing = true; });
      input.addEventListener('compositionend', () => { composing = false; endedAt = now(); });
      input.addEventListener('keydown', event => {
        // WebKit can emit compositionend before the confirmation keydown.
        if (composing || event.isComposing || event.keyCode === 229 || now() - endedAt < 50) return;
        if (event.key === 'Enter' && !event.shiftKey && allowSend()) {
          event.preventDefault();
          send();
        }
      });
      return () => composing;
    }
  };
})(globalThis);
