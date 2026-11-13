(function (global) {
  'use strict';
  const t = value => global.FOSI18n?.t(value) || value;
  const operators = {eq:'等于',ne:'不等于',gt:'大于',gte:'大于或等于',lt:'小于',lte:'小于或等于',contains:'包含',is_missing:'缺失',is_present:'有值'};
  function render(trigger, evaluation) {
    if (!trigger || !global.document) return null;
    const element = (tag, text, raw = false) => {
      const node = global.document.createElement(tag);
      if (text !== undefined) node.textContent = raw ? String(text) : t(text);
      if (raw) node.setAttribute('translate', 'no');
      return node;
    };
    const text = value => global.document.createTextNode(value);
    function condition(rule) {
      const node = element('span');
      const children = rule.all || rule.any;
      if (children) {
        node.append(text('('));
        children.forEach((child, index) => {
          if (index) node.append(text(' '), element('span', rule.all ? '且' : '或'), text(' '));
          node.append(condition(child));
        });
        node.append(text(')'));
      } else {
        node.append(element('span', rule.metric === 'row_count' ? '输出行数' : rule.field, rule.metric !== 'row_count'),
          text(' '), element('span', operators[rule.op] || rule.op));
        if ('value' in rule) node.append(text(' '), element('span', JSON.stringify(rule.value), true));
      }
      return node;
    }
    const host = element('div'); host.className = 'report-trigger-summary';
    host.append(element('strong', '条件触发'));
    const rule = element('p'); rule.append(condition(trigger.condition)); host.append(rule);
    host.append(element('p', trigger.match === 'all' ? '所有输出行满足条件时触发。' : '任一输出行满足条件时触发。'));
    host.append(element('p', trigger.notify === 'every_match' ? '每次检查满足条件时发送。' : '首次满足时发送；条件解除后可再次触发。'));
    if (trigger.cooldown_minutes) {
      const cooldown = element('p'); cooldown.append(element('span', '通知冷却时间'), text(': '),
        element('span', trigger.cooldown_minutes, true), text(' '), element('span', '分钟')); host.append(cooldown);
    }
    if (evaluation) host.append(element('p', evaluation.matched === true ? '当前预览满足触发条件。' :
      evaluation.matched === false ? '当前预览未满足触发条件；不会发送。' : '当前数据无法确定触发条件；不会发送。'));
    return host;
  }
  const api = {render}; global.ReportTriggerUI = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
