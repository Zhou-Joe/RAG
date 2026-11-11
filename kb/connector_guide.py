from __future__ import annotations

"""采集工具的操作说明与完整最小示例。"""

TOOL_GUIDE = r'''
【test_browser_connector 使用手册】
先观察一个真实列表、一个真实详情，确认字段，再调用本工具。它接收完整 spec 对象，不是浏览器动作片段。
必填 name、url、source_type="browser"、records_path="records"、id_paths、body_fields、browser.output。
1. url 是实际浏览器起始地址。单页应用的 #/spot/list?... 必须原样保留，不能删掉hash，也不能猜造 /spot/list HTTP路径，否则会404。
2. 始终在body_fields保留 {"label":"采集日期","source":"today"}，用户确认时该字段必选。它来自采集器系统时钟，按任务timezone（默认Asia/Shanghai）生成，不是官网字段，不需要用户再确认，不搜索网页日期。另需精确采集时间时可用 {"label":"采集时间","source":"now"}。API、DOM和network所有模式都支持。
   如果日期还要用于 id_paths，另在 output.fields 中加 date:{source:"today"}，就会注入每条记录。
3. network 模式读取浏览器实际捕获的JSON数组，path相对JSON根；保留数组对象原有字段。output.fields 可用 path 映射JSON字段，或用 source 注入系统日期/时间。
   network 支持 detail:{url_field:"详情链接字段",fields:{...}}，会逐条打开真实详情再合并。不是只返回列表！不要因为需要详情或日期就放弃network改猜DOM。
4. dom 模式要求 rows(每条记录的CSS)、fields(相对每行的CSS)。detail.url_field 引用已经采到的链接；无链接时用 detail.click_selector(相对每行的点击目标)。network详情不支持click_selector。
5. Tailwind类有方括号/点号/斜杠时，使用 [class~="mb-[.2rem]"] 这类属性选择器，或直接复用观察工具提供的已转义selector。禁止写 .mb-[.2rem]；也不要用整个class字符串相等来依赖类名顺序。
6. “关闭时间−开启时间”写 computed:{op:"duration",start:"open_time",end:"close_time",format:"hhmm",unit:"hours"}，由程序计算。不开网页找duration，不编造时间，不能把算不出的时长填0。
7. 默认 on_error="raw"：原文格式变化、正则不匹配、非数字时间都保留原文和采集提示，供用户审核及以后修改。只要取得真实记录，不为格式警告反复验证或追问。确实要严格失败才设 on_error="fail"。
8. 稳定ID、URL、网络权限、必要配置仍须有效；抓不到任何数据或缺稳定ID时说明具体阻碍。禁止用数组序号/随机数冒充稳定ID。
9. 本工具成功后系统直接展示真实样例，结束当前生成步骤，等待用户审核。warning是原文回退提示，不是要求重试。完成后不要再重放已通过的脚本。
10. 用户追问/回答/确认后继续沿用消息和浏览器。只变body_fields(如加日期/时长/计数/业务公式)时，可直接test_mapping或show_data_preview复用已验证的原始样例；test_browser_connector也会自动复用同一取数配置当日已验证的完整批次。取数步骤改变、样例跨天或refresh:true才重新抓取；用户明确要求实时刷新时传refresh:true。
11. browser.max_records与output平级；不能写output.max_records。例：browser:{max_records:50000,actions:[],output:{kind:"dom",rows:".实际卡片",fields:{...},max_pages:100}}。首次拉取历史可使用最多50000条、500MB、100页；不要为了通过验证而漏采用户要求的记录。
12. 详情默认render:"auto"：先读取原始HTML，目标字段不在HTML才加载JS。网页观察已确认HTML含所需数据时可以选html。资源上限报错时优先改详情加载策略，不原样重跑整站、不缩小记录数假装任务完成。
13. 用户可先要求采集完整历史，成功后再在同一对话修改时间范围和cron用于每日更新。无需额外的补历史状态流程；已有明确max_records、max_pages或API分页大小较小时，按这次用户要求调整并重新验证真实请求。API一次只取一页，不能声称会自动翻完所有页。

【业务计算手册：对任意行业都适用】
原始网页/JSON字段是输入，派生字段在body_fields.computed定义。计算结果不要出现在browser.output.fields里；不要发明一个selector去找“总数、利润、平均值、率”。
网页字段all:true配合regex会返回全部匹配（包括同一节点内多个时间），DOM无匹配返回未知，不是已知空列表；不创建(x)等虚假正则或占位selector。
每个表达式为{op:"操作名",args:[输入...]}。输入可以是{"field":"原始路径"}、数字/文本/布尔/null常量、{"literal":[...]}或另一个表达式。可嵌套组合；不要使用Python、JS或公式字符串。
支持：add/subtract/multiply/divide，round(digits)、abs、number；parse_number(pattern/group,decimal_separator,thousands_separator,scale,on_error=raise/null)用于数值和数字字符串、单位文本转换，原始值不修改；例如{"op":"parse_number","args":[{"field":"时长"}],"pattern":"([0-9]+(?:\\.[0-9]+)?)\\s*分钟","group":1}。默认失败报错；确认缺失值才用null，禁止填0。先preview_report_calculation核对多种真实输入，含多个数字的文本需明确提取规则。count/sum/average/min/max/unique；split(separator正则)、match(pattern正则,group)；eq/ne/gt/gte/lt/lte/contains、and/or/not、if、coalesce；map/filter/group_by(path,aggregate)；join/concat；duration和date_part(part)。
map/filter的第二个args在每条子记录内求值，{"field":""}指当前值；$root.<路径>引用外层原记录，$records指本次完整采集批次。group_by.aggregate在每组数组内求值。count默认按数组项计数；文本默认按 |、逗号、分号或换行拆分，也可指定pattern/ separator和distinct去重。sum等聚合接收数字数组，记录数组先map取值。
示例A：原始show_times="11:00|13:00|16:00"，用户要当天场次数（一个数字），写{"label":"当日场次数","computed":{"op":"count","args":[{"field":"show_times"}],"separator":"[|,，;；\\n]+"}}，结果必须是数字3，不是时间列表、不是字符串"3"。连续开放时段不能擅自按两个端点算成两场；无场次信息保留未知，明确停演才可按条件给0。
示例B：订单利润与利润率：{"op":"subtract","args":[{"field":"revenue"},{"field":"cost"}]}；{"op":"round","digits":2,"args":[{"op":"multiply","args":[100,{"op":"divide","args":[{"op":"subtract","args":[{"field":"revenue"},{"field":"cost"}]},{"field":"revenue"}]}]}]}。
示例C：只统计paid订单金额：{"op":"sum","args":[{"op":"map","args":[{"op":"filter","args":[{"field":"orders"},{"op":"eq","args":[{"field":"status"},"paid"]}]},{"field":"amount"}]}]}。数量×单价可用map嵌套multiply后sum。
示例D：温度等级：{"op":"if","args":[{"op":"gte","args":[{"field":"temperature"},80]},"高温","正常"]}。按地区汇总金额：{"op":"group_by","path":"region","args":[{"field":"orders"}],"aggregate":{"op":"sum","args":[{"op":"map","args":[{"field":""},{"field":"amount"}]}]}}。
时间差兼容原有duration:{op:"duration",start:"open",end:"close",format:"hhmm",unit:"hours"}，也可用args的两个字段，unit支持seconds/minutes/hours/days。
对不确定公式先用preview_calculation(computed,record,records可选)试算真实样例，检查value和value_type，再把同一规则写入body_fields.computed；已有有效取数配置时不要重抓网页只为修改公式。最终通过test_mapping/show_data_preview或test_browser_connector验证完整输出。
缺失输入、除数0、无匹配文本不能随意编成0；默认保留实际输入并显示计算提示。明确的空数组计数为0。所有已知算式由程序确定计算，允许且应当主动完成，不需要用户再授权你做加减乘除。

【完整network+详情示例】字段/选择器仅示意，必须替换成实际观察结果。假设列表JSON的data为[{id:101,title:"项目A",jump_url:"https://site.example/#/spot/detail?id=101"}]，详情的[class~="hours"]是真实观察到的营业原文。
{"spec":{"name":"项目运营时间","url":"https://site.example/#/spot/list?type=7","source_type":"browser","records_path":"records","id_paths":["id"],"title_path":"title","source_url_path":"source_url","on_error":"raw","timezone":"Asia/Shanghai","cron":"0 8 * * *","body_fields":[{"label":"名称","path":"title"},{"label":"日期","source":"today"},{"label":"开始","path":"open_time"},{"label":"结束","path":"close_time"},{"label":"时长(小时)","computed":{"op":"duration","start":"open_time","end":"close_time","format":"hhmm","unit":"hours"}},{"label":"官网原文","path":"time_text"}],"browser":{"actions":[],"output":{"kind":"network","url_contains":"get_spot_list","path":"data","fields":{"date":{"source":"today"}},"detail":{"url_field":"jump_url","fields":{"time_text":{"selector":"[class~=\"hours\"]"},"open_time":{"selector":"[class~=\"hours\"]","regex":"([0-9]{1,2}:[0-9]{2})","group":1},"close_time":{"selector":"[class~=\"hours\"]","regex":"[-–—~至]\\s*([0-9]{1,2}:[0-9]{2})","group":1}}}}}}}

【DOM列表最小示例】同样必须使用真实选择器。
{"spec":{"name":"项目清单","url":"https://site.example/#/list","source_type":"browser","records_path":"records","id_paths":["detail_url"],"title_path":"name","body_fields":[{"label":"名称","path":"name"},{"label":"采集日期","source":"today"}],"browser":{"output":{"kind":"dom","rows":".observed-card","fields":{"name":{"selector":".observed-title"},"detail_url":{"selector":"a","attribute":"href"}}}}}}
'''
