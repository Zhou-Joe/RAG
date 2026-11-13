(function(root) {
  'use strict';
  let serial = 0;
  function create(host, options = {}) {
    host._fosChartTooltip?.destroy();
    const document = options.document || host.ownerDocument || root.document;
    const viewport = options.window || document.defaultView || root;
    const tooltip = document.createElement('div');
    tooltip.className = 'fos-chart-tooltip notranslate';
    tooltip.id = 'fos-chart-tooltip-' + (++serial);
    tooltip.hidden = true;
    tooltip.setAttribute('role', 'tooltip');
    tooltip.setAttribute('translate', 'no');
    document.body.appendChild(tooltip);
    const listeners = [];
    let active = null, pinned = false, destroyed = false;
    function listen(node, type, handler, capture = false) {
      node.addEventListener(type, handler, capture);
      listeners.push(() => node.removeEventListener(type, handler, capture));
    }
    function hide() {
      if (active) {
        active.marker?.classList.remove('is-chart-point-active');
        if (active.description === null) active.target.removeAttribute('aria-describedby');
        else active.target.setAttribute('aria-describedby', active.description);
      }
      active = null; pinned = false; tooltip.hidden = true;
    }
    function position(target) {
      const anchor = target.getBoundingClientRect(), box = tooltip.getBoundingClientRect();
      const width = viewport.innerWidth || document.documentElement.clientWidth;
      const height = viewport.innerHeight || document.documentElement.clientHeight;
      const left = Math.max(8, Math.min(width - box.width - 8, anchor.left + anchor.width / 2 - box.width / 2));
      let top = anchor.top - box.height - 10;
      if (top < 8) top = anchor.bottom + 10;
      top = Math.max(8, Math.min(height - box.height - 8, top));
      tooltip.style.left = left + 'px'; tooltip.style.top = top + 'px';
    }
    function bind(target, lines, {marker} = {}) {
      const entry = {target, marker, description: target.getAttribute('aria-describedby')};
      function show(pin = false) {
        if (destroyed) return;
        if (active !== entry) hide();
        active = entry; pinned = pin;
        const values = typeof lines === 'function' ? lines() : lines;
        tooltip.replaceChildren(...values.map(value => {
          const line = document.createElement('div');
          line.textContent = String(value); return line;
        }));
        tooltip.hidden = false;
        target.setAttribute('aria-describedby', [entry.description, tooltip.id].filter(Boolean).join(' '));
        marker?.classList.add('is-chart-point-active');
        position(target);
      }
      listen(target, 'pointerenter', () => show());
      listen(target, 'pointerleave', () => {if (!pinned && document.activeElement !== target) hide();});
      listen(target, 'focus', () => show());
      listen(target, 'blur', hide);
      listen(target, 'click', () => {if (active === entry && pinned) hide(); else show(true);});
      listen(target, 'keydown', event => {
        if (event.key === 'Escape') hide();
        else if (event.key === 'Enter' || event.key === ' ') {event.preventDefault(); show(true);}
      });
      return target;
    }
    function outside(event) {
      if (active && event.target !== active.target && !active.target.contains?.(event.target)) hide();
    }
    listen(document, 'pointerdown', outside, true);
    listen(document, 'keydown', event => {if (event.key === 'Escape') hide();});
    listen(viewport, 'scroll', hide, true);
    listen(viewport, 'resize', hide);
    const api = {bind, hide, destroy() {
      if (destroyed) return;
      hide(); destroyed = true;
      listeners.splice(0).forEach(remove => remove()); tooltip.remove();
      if (host._fosChartTooltip === api) delete host._fosChartTooltip;
    }};
    host._fosChartTooltip = api;
    return api;
  }
  const api = {create};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.FosChartTooltip = api;
})(globalThis);
