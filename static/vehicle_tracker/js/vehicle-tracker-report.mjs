// QA green tags transcribed from the 5# train final assembly visual inspection report
// (SHDR-NDT-VTR001, WO 3281713, 2020-08-27). Pages 3–22: wheel carriers; 23–27: coaches.
import {PART_MAP,KINDS,uid,log,newProject} from './vehicle-tracker-core.mjs';
const t=text=>globalThis.FOSI18n?.t(text)||text;

export const REPORT = {
  file:'VT Report_Appearance_SDMT_WO3281713.pdf',
  wonum:'3281713',
  date:'2020-08-27',
  train:'5',
  title:'5# 列车（T5）总装销轴目视检测报告',
  inspector:'Geng Xu（检验）/ Cong Wang（确认，II 级）',
};

// Tag batches: the work order, inspector and date printed on every tag of that batch.
const BATCH = {
  loadPin:{wonum:'2103550',inspector:'QA01',date:'2019-03-04',facility:'SDMT'},
  pin:{wonum:'3023726',inspector:'QA12',date:'2020-04-08',facility:'SDMT'},
  pin2019:{wonum:'2870277',inspector:'QA16',date:'2019-12-27',facility:'SDMT'},
  carrierPin:{wonum:'3023726',inspector:'QA16',date:'2020-03-30',facility:'SDMT'},
  loadSpindle:{wonum:'3156343',inspector:'QA16',date:'2020-07-17',facility:'SDMT'},
  spindle:{wonum:'3156343',inspector:'QA16',date:'2020-07-20',facility:'SDMT'},
  coach:{wonum:'3023726',inspector:'QA16',date:'2020-03-30',facility:'SDMT'},
  scu:{wonum:'3023726',inspector:'QA07',date:'2020-03-31',facility:'DWAR'},
  site2019:{wonum:'2761915',inspector:'QA09',date:'2019-11-11',facility:'小矮人矿山车'},
};
// Serial order per carrier page: load/guide/upstop pivot pins F,R; wheel carrier pivot pin;
// then load/guide/upstop spindles F,R. "*" = 2019-12 pin batch (WO 2870277); "!" = inspector QA11.
const SLOTS = [['RF-PP','loadPin'],['RR-PP','loadPin'],['GF-PP','pin'],['GR-PP','pin'],['SF-PP','pin'],['SR-PP','pin'],['PP','carrierPin'],
  ['RF-SP','loadSpindle'],['RR-SP','loadSpindle'],['GF-SP','spindle'],['GR-SP','spindle'],['SF-SP','spindle'],['SR-SP','spindle']];
const CARRIER_PAGES = [
  [3,'C1-LF','BZID009 BZID002 BSIH-002 BXTH-046 BTXZ-250 BUMQ-041 BTYH-014 BXSP021 BADE004 ABIP081 ABIP086 ABIP102 ABIP095'],
  [4,'C1-LR','BZID032 BZID028 BXTH-068 BXTH-172 BUMQ-015 BXTH-033 BXDB-018 AYDW132 !AYDW212 ABIP083 ABIP082 ABIP033 ABIP101'],
  [5,'C1-RF','BZID090 BZID167 BXTH-036 *AYTK-458 BSIH-009 *AYTK-366 BTYH-011 AYDW179 AYDW193 ABIP074 ABIP099 ABIP084 ABIP097'],
  [6,'C1-RR','BZID108 BZID056 BXTH-132 *AYTK-406 BUMQ-008 BSIH-001 BTYH-052 ABIQ038 AYDW174 ABIP091 ABIP072 ABIP100 ABIP028'],
  [7,'C2-RF','BZID170 BZID126 AYTK-061 *AYTK-325 *AYTK-328 AYTK-398 BTYH-018 AYDW017 AYDW133 ABIP060 ABIP041 ABIP067 ABIP036'],
  [8,'C2-LR','BZID117 BZID175 AYTK-041 AYTK-028 AYTK-329 AYTK-221 BXDB-020 !AYDW097 AYDW186 ABIP021 ABIP042 ABIP040 ABIP075'],
  [9,'C2-LF','BZID010 BZID151 BXTH-133 BUMQ-030 BUMQ-057 BXTH-158 BTYH-051 ABIQ032 AYDW110 ABIP068 ABIP019 ABIP078 ABIP023'],
  [10,'C2-RR','BZID054 BZID016 *AYTK-372 AYTK-116 AYTK-440 AYTK-378 BTYH-020 !AYDW185 AYDW062 ABIP063 ABIP069 ABIP017 ABIP061'],
  [11,'C3-LR','BZID173 BZID169 AYTK-153 AYTK-163 AYTK-025 AYTK-099 BTYH-017 AYDW034 AYDW143 AYDX081 ABIP044 ABIP109 ABIP108'],
  [12,'C3-RF','BZID145 BZID082 *AYTK-333 *AYTK-417 *AYTK-151 *AYTK-226 BTYH-053 AYDW046 AYDW155 AYDX083 ABIP051 ABIP111 AYDX044'],
  [13,'C3-LF','BZID062 BZID152 AYTK-189 *AYTK-201 AYTK-349 AYTK-264 BTYH-012 AYDW092 AYDW127 ABIP047 ABIP054 ABIP107 AYDX082'],
  [14,'C3-RR','BZID112 BZID046 *AYTK-040 *AYTK-110 *AYTK-370 *AYTK-216 BTYH-059 AYDW203 AYDW102 ABIP049 ABIP030 ABIP110 ABIP106'],
  [15,'C4-LR','BZID164 BZID146 BXTH-176 BXTH-030 BXTH-080 BXTH-110 BXDB-008 AYDW206 AYDW145 AYDX204 AYDX252 AYDX401 BXVM007'],
  [16,'C4-RF','BZID135 BZID177 180206005 AYTK-012 AYTK-450 AYTK-387 BTYH-058 AYDW043 AYDW082 AYDX468 AYDX110 AYDX429 AYDX261'],
  [17,'C4-LF','BZID143 BZID034 *AYTK-416 *AYTK-428 *AYTK-334 *AYTK-253 BTYH-060 AYDW194 AYDW083 AYDX209 AYDX113 AYDX471 AYDX218'],
  [18,'C4-RR','BZID078 BZID128 *AYTK-218 *AYTK-114 *AYTK-063 *AYTK-169 BXTG-063 AYDW230 AYDW242 AYDX293 AYDX103 AYDX402 AYDX233'],
  [19,'C5-RF','BZID013 BZID067 AYTK-359 AYTK-353 AYTK-136 AYTK-023 BTYH-056 AYDW072 AYDW164 ABIP103 AYDX004 AYDX088 AYDX106'],
  [20,'C5-LR','BZID154 BZID077 AYTK-033 AYTK-138 AYTK-445 AYTK-018 BTYH-015 AYDW227 AYDW195 ABIP026 ABIP020 AYDX108 ABIP104'],
  [21,'C5-RR','BZID064 BZID079 AYTK-010 BCIR-008 AYTK-404 AYTK-489 BTYH-016 AYDW156 AYDW167 ABIP027 AYDX062 AYDX061 AYDX104'],
  [22,'C5-LF','BZID035 BZID091 AYTK-295 AYTK-174 AYTK-413 AYTK-243 BXDB-027 !AYDW240 AYDW061 ABIP024 AYDX014 AYDX084 AYDX018'],
];
// Coach pages: towbar spindle F/R, SCU pivoting spindle F/R ("轴 Shaft"), banking spindle, chain/ARB dog axles.
// "AES" = assembled and inspected on site by AES, no QA tag.
const COACH_PAGES = [
  [23,1,{'TB-SPF':['site2019','AYDY035'],'TB-SPR':['coach','BYRG-005'],'SCU-SPF':['scu','AOVD-001'],'SCU-SPR':['scu','BCHO-004'],'CH-MA-BS':['coach','AYQD-001']}],
  [24,2,{'TB-SPF':'AES','TB-SPR':['coach','BYRG-001'],'SCU-SPF':['scu','AYDA-029'],'SCU-SPR':['scu','BYWX-009'],'CH-MA-BS':['coach','BABX-034']}],
  [25,3,{'TB-SPF':'AES','TB-SPR':['coach','AYDY-018'],'SCU-SPF':['scu','BYWX-003'],'SCU-SPR':['scu','BYWX-015'],'CH-MA-BS':['coach','AYQD-030'],'DOG-AX1':['site2019','BTAS004'],'DOG-AX2':['site2019','BSIL017']}],
  [26,4,{'TB-SPF':'AES','TB-SPR':['coach','BYRG-011'],'SCU-SPF':['scu','BCHO-008'],'SCU-SPR':['scu','AYDA-050'],'CH-MA-BS':['coach','AYQD-007'],'DOG-AX1':['site2019','BTAS017'],'DOG-AX2':['site2019','BTAS007']}],
  [27,5,{'TB-SPF':'AES','TB-SPR':['coach','BYRG-002'],'SCU-SPF':['scu','BYWX-021'],'SCU-SPR':['scu','BCHO-005'],'CH-MA-BS':['coach','AYQD-027']}],
];

function record(partId,batch,serial,page,extra={}) {
  const part=PART_MAP.get(partId);
  if(!part)throw Error(t(`报告位置未对应部件：${partId}`));
  const kind=KINDS[part.kind];
  return {id:uid(),...BATCH[batch],...extra,serial,drawing:kind.dwg,slot:part.code||'',reportWo:REPORT.wonum,
    description:`${kind.zh} · ${kind.en}`,partId,source:REPORT.file,sourceUnit:'页',sourceRow:page};
}
export function reportRecords() {
  const records=[];
  for(const [page,carrier,list] of CARRIER_PAGES) {
    const serials=list.split(' ');
    if(serials.length!==SLOTS.length)throw Error(t(`第 ${page} 页标签数量不符`));
    serials.forEach((raw,i)=>{
      const [slot,batch]=SLOTS[i],serial=raw.replace(/^[*!]/,'');
      const id=`${carrier}-${slot}`;
      records.push(record(id,raw[0]==='*'?'pin2019':batch,serial,page,raw[0]==='!'?{inspector:'QA11'}:{}));
    });
  }
  for(const [page,coach,slots] of COACH_PAGES)
    for(const [slot,value] of Object.entries(slots)) if(value!=='AES') records.push(record(`C${coach}-${slot}`,value[0],value[1],page));
  return records;
}
export function reportNotes() {
  const notes={};
  for(const [page,coach,slots] of COACH_PAGES) for(const [slot,value] of Object.entries(slots)) if(value==='AES')
    notes[`C${coach}-${slot}`]={name:PART_MAP.get(`C${coach}-${slot}`).name,notes:`总装报告第 ${page} 页：AES 现场装配并检验，无 QA 绿标。`};
  for(const coach of [1,2,5]) notes[`C${coach}-CH`]={name:'底盘',notes:'总装报告：本车无链条/防倒滑卡件轴（标注“无”）。'};
  return notes;
}
export function reportProject() {
  const project=newProject(`5# 列车（T5）· 总装报告 WO${REPORT.wonum}`,`依据 ${REPORT.file}`);
  project.description=`${REPORT.title}。检验日期 ${REPORT.date}，${REPORT.inspector}。绿标信息按报告位置码直接对应部件。`;
  project.records=reportRecords();
  project.parts=reportNotes();
  log(project,'导入总装报告','',`${REPORT.file}：${project.records.length} 张 QA 绿标，按报告位置码绑定`);
  return project;
}
