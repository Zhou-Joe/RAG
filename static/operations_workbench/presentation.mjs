const t = text => globalThis.FOSI18n?.t(text) || text;
const labels = {
  wonum: '工单号', workorderid: '记录编号', jpnum: '作业计划', description: '说明',
  description_zh: '中文说明', description_en: '英文说明', long_description: '详细说明',
  long_description_zh: '中文详述', long_description_en: '英文详述', long_text: '工程备注',
  assetnum: '资产编号', location: '设备位置', status: '状态', worktype: '工单类型',
  wopriority: '优先级', persongroup: '班组', origrecordid: '来源记录', taskid: '任务号',
  jobtaskid: '任务 ID', jptask: '作业步骤', tasks: '任务步骤', task_count: '任务数',
  location_description: '位置说明', asset_description: '资产说明', supervisor: '主管', lead: '负责人',
  crewid: '班组编号', estdur: '预计工期', estlabhrs: '预计人工工时', cust_wrenchtime: '扳手工时',
  reportedby: '报告人', parent: '父工单', problemcode: '问题代码', cust_guestimpact: '游客影响',
  targstartdate: '目标开始', targcompdate: '目标完成', pmduedate: 'PM 到期',
  targstart_local: '目标开始', targcomp_local: '目标完成', pmdue_local: 'PM 到期',
  schedstart_local: '计划开始', schedfinish_local: '计划完成', actstart_local: '实际开始',
  actfinish_local: '实际完成', jp_description_en: '作业计划英文说明', jp_description_zh: '作业计划中文说明',
  jobplanid: '作业计划 ID', priority: '优先级', jpduration: '计划工期', taskduration: '任务工期',
  craft_description_zh: '工艺说明', craft_description_en: '工艺英文说明', cust_attraction: '景点简码',
  changeby: '变更人', memo: '备注', elapsed_seconds: '查询耗时（秒）', timezone: '时区',
  revnum: '版本', pluscrevnum: '版本', pmnum: '预防性维护编号', status_history: '状态履历',
  status_events: '状态履历', changed_local: '变更时间', related: '关联记录', workorders: '关联工单',
  workorder: '工单信息', jobplan: '作业计划', report_local: '报告时间', report_day: '报告日期',
  attraction_name_zh: '景点', attraction_name_en: '景点英文名', attraction_code: '景点代码',
  downtime_minutes: '停机分钟', downtime_hours: '停机小时', downtime_code: '停机代码',
  downtime_start_local: '停机开始', downtime_end_local: '设备恢复', ongoing: '尚未恢复',
  failurecode: '故障代码', failure_description: '故障说明', problem_description: '问题说明',
  _time: '时间', _raw: '日志原文', apexmssgdatetime: '日志时间', attrname: '景点',
  apexmssgtarget: '目标设备', apexmssgmnemonic: '助记码', apexmssgtext: '消息',
  apexmssgid: '消息编号', host: '主机', target: '目标设备', mnemonic: '助记码', message: '消息',
  cust_craft: '工艺', cust_jpfreq: '频率', cust_jptype: '计划类型', cust_compliance: '遵从类型',
  start_date: '开始日期', end_date: '结束日期', start_time: '开始时间', end_time: '结束时间',
  earliest_time: '起始时间', latest_time: '截止时间', limit: '最多返回', count: '返回条数',
  keyword: '关键词', source: '数据来源', queried_at: '查询时间', classification: '设备分类',
  equipment_location: '设备位置', codes: '停机代码', event_count: '事件数', minutes: '停机分钟',
  average_minutes: '平均分钟', ongoing_count: '尚未恢复', duration_count: '有时长的事件',
  missing_duration_count: '缺少时长的事件', label: '类别', daily: '每日趋势', reasons: '原因分布',
  attractions: '景点分布', key: '标识', totals: '合计', warnings: '数据提示', assets: '设备（资产）分布',
  asset_count: '涉及资产数', heatmap: '日期 × 景点', comparison: '上期对比',
};
export const sourceNames = {maximo: 'Maximo', splunk: 'Splunk', downtime: 'Downtime'};

export function fieldLabel(key) {
  return labels[key] ? t(labels[key]) : key;
}

export function cellText(value) {
  if (value === false) return t('否');
  if (value === true) return t('是');
  return value == null || value === '' ? '—' : String(value);
}

const HOUR_FIELDS = new Set(['estdur', 'estlabhrs', 'cust_wrenchtime', 'taskduration', 'jpduration', 'downtime_hours']);
// Log timestamps keep their seconds and milliseconds.
const PRECISE_FIELDS = new Set(['_time', 'apexmssgdatetime', '_raw']);
const DATE_TIME = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$/;
const siteTime = new Intl.DateTimeFormat('sv-SE', {timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit',
  day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false});

// Maximo stores these in UTC; the *_local columns hold the same moment in site time.
export const RAW_TIME_PAIRS = {targstartdate: 'targstart_local', targcompdate: 'targcomp_local', pmduedate: 'pmdue_local'};

export function formatHours(value) {
  const hours = Number(value);
  return Number.isFinite(hours) ? Number(hours.toFixed(2)) + ' h' : cellText(value);
}

export function formatDateTime(value) {
  const match = typeof value === 'string' ? DATE_TIME.exec(value.trim()) : null;
  if (!match) return null;
  if (!match[3]) return match[1] + ' ' + match[2];
  const moment = new Date(value);
  return Number.isNaN(moment.getTime()) ? null : siteTime.format(moment);
}

export function formatField(key, value) {
  if (value == null || value === '') return '—';
  if (HOUR_FIELDS.has(key) && value !== '' && Number.isFinite(Number(value))) return formatHours(value);
  if (!PRECISE_FIELDS.has(key)) {
    const moment = formatDateTime(value);
    if (moment) return moment;
  }
  return cellText(value);
}

const statuses = {
  WAPPR: ['待批准', 'warn'], APPR: ['已批准', 'brand'], WSCH: ['待排程', 'warn'], WMATL: ['待物料', 'warn'],
  WPCOND: ['待工况', 'warn'], INPRG: ['进行中', 'brand'], COMP: ['已完成', 'ok'], AUDIT: ['待审核', 'violet'],
  CCOMP: ['强制完成', 'mute'], CLOSE: ['已关闭', 'ok'], CAN: ['已取消', 'mute'], ACTIVE: ['生效', 'ok'],
  INACTIVE: ['停用', 'mute'], DRAFT: ['草稿', 'warn'], PNDREV: ['待修订', 'warn'], REVISED: ['已修订', 'mute'],
};

export function statusInfo(code) {
  const text = code == null ? '' : String(code).trim().toUpperCase();
  const [label, tone] = statuses[text] || [text || '—', 'mute'];
  return {code: text, label: statuses[text] ? t(label) : label, tone};
}

export function recordsFrom(data) {
  if (Array.isArray(data)) return data;
  return Array.isArray(data?.items) ? data.items : [];
}

const preferredColumns = {
  maximo: ['wonum', 'jpnum', 'description_zh', 'description', 'status', 'assetnum', 'location', 'report_local', 'cust_craft'],
  downtime: ['wonum', 'attraction_name_zh', 'description', 'downtime_start_local', 'downtime_end_local', 'downtime_minutes', 'location', 'downtime_code'],
  splunk: ['_time', 'apexmssgdatetime', 'attrname', 'apexmssgtarget', 'apexmssgmnemonic', 'apexmssgtext', 'message', '_raw'],
};

export function tableColumns(rows, source, max = 5) {
  const keys = new Set();
  for (const row of rows) {
    for (const [key, value] of Object.entries(row)) {
      if (value != null && value !== '' && typeof value !== 'object') keys.add(key);
    }
  }
  const order = [...new Set([...(preferredColumns[source] || []), ...keys])];
  return order.filter(key => keys.has(key)).slice(0, max);
}

export function csvContent(rows, source) {
  const columns = tableColumns(rows, source, Infinity);
  const quote = value => {
    let s = value == null ? '' : String(value);
    if (/^[\s]*[=+@-]/.test(s) || /^[\t\r\n]/.test(s)) s = "'" + s;
    return '"' + s.replaceAll('"', '""') + '"';
  };
  const lines = [columns.map(key => quote(fieldLabel(key))).join(',')];
  for (const row of rows) lines.push(columns.map(key => quote(row[key])).join(','));
  return '\uFEFF' + lines.join('\r\n');
}
