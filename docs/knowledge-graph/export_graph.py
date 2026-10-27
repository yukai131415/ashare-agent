"""Render the code-grounded Harness architecture and comparison (Pillow required)."""
import json
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).parent
FONT = 'C:/Windows/Fonts/msyh.ttc'
BOLD = 'C:/Windows/Fonts/msyhbd.ttc'
layers = [
 ('entry','入口层','保留',[
  ('web','Web 看板与租户入口','web.py'),('cli','CLI：plan / audit / resume','cli.py'),('scheduler','定时调度与每日尝试上限','scheduler.py'),('mcp','MCP 服务：5 个工具','mcp_server.py')]),
 ('application','应用编排层','接入 Harness',[
  ('formal','正式选股：Supervisor / Workflow','supervisor.py; workflow.py'),('shadow','影子研究：独立模型结论','shadow.py'),('intraday','当日分析：时段唯一记录','intraday.py'),('master','主 Agent：受限分发与恢复','harness/control/master.py')]),
 ('planning','可信计划层','新增',[
  ('plan','get_execution_plan 只读工具','harness/planning/tool.py'),('steps','正式 / 影子 / 盘中三类计划','harness/planning/tool.py'),('contract','依赖、工具白名单、验收、副作用','harness/planning/tool.py'),('version','版本化模板；不由模型任意改写','harness/planning/tool.py')]),
 ('execution','执行与边界层','新增 / 统一',[
  ('runtime','WorkflowHarness 阶段执行器','harness/runtime.py'),('gateway','工具与模型调用网关','harness/gateway.py'),('context','上下文指纹与大小检查','harness/context.py'),('locks','单机跨进程互斥锁','harness/locking.py')]),
 ('recovery','状态与恢复层','新增 / 加强',[
  ('ledger','SQLite 运行账本 runs.db','harness/persistence.py'),('checkpoint','检查点：同输入成功阶段复用','harness/runtime.py'),('policy','错误分类、白名单、累计预算','harness/policy.py'),('unknown','发布或模型结果不明 → 待核对','harness/runtime.py; harness/gateway.py')]),
 ('business','业务与模型层','保留 / 迁移',[
  ('workers','行情 / 新闻 / 分析 / 回测 / 推荐','agents/'),('rules','原评分、准入与证据校验','selection.py; models.py'),('adapter','千问等服务商协议适配','harness/adapters/llm.py'),('data','行情 / 财务 / Milvus / 外部 MCP','financial_transport.py; research.py; external_stock_mcp.py')]),
 ('observability','审计与安全层','新增 / 保留',[
  ('audit','轨迹一致性验收，不调用模型','harness/evaluation.py'),('monitor','管理员弹框：计划与最近任务','static/agent-control.js'),('usage','调用回执与真实 usage 留存','harness/gateway.py; token_usage.py'),('security','租户隔离、鉴权、脱敏与日志','auth.py; privacy.py; console_log.py')]),
]
nodes = [{'id':'system','label':'A股多 Agent 系统 · Harness 架构','kind':'system'}]
edges = []
for gid,title,change,children in layers:
 nodes.append({'id':gid,'label':title,'kind':'layer','change':change})
 edges.append(['system','分层组织',gid])
 for ident,label,source in children:
  nodes.append({'id':ident,'label':label,'kind':'component','source':'src/a_stock_agents/'+source})
  edges.append([gid,'包含',ident])
edges += [
 ['web','发起/查看','formal'],['web','发起/查看','shadow'],['web','查看','intraday'],
 ['cli','读取','plan'],['cli','审计','audit'],['cli','仅正式任务恢复','formal'],
 ['mcp','暴露只读计划工具','plan'],['scheduler','触发','formal'],['scheduler','触发','intraday'],
 ['formal','按计划执行','runtime'],['shadow','按计划执行','runtime'],['intraday','按计划执行','runtime'],
 ['master','受规则约束','policy'],['master','运行内模型调用经由','gateway'],
 ['plan','返回','steps'],['plan','返回','contract'],['plan','返回','version'],
 ['runtime','读取步骤依赖','plan'],['runtime','执行','workers'],['runtime','使用','locks'],
 ['runtime','写入','ledger'],['runtime','恢复时核对','checkpoint'],['runtime','不确定副作用暂停','unknown'],
 ['gateway','执行工具白名单','contract'],['gateway','检查资料截止时间','context'],
 ['gateway','存储调用档案','ledger'],['gateway','模型发送前登记','usage'],
 ['adapter','经过模型边界','gateway'],['gateway','调用工具获取','data'],
 ['workers','遵循','rules'],['shadow','调用','adapter'],['intraday','调用','adapter'],
 ['checkpoint','持久保存','ledger'],['policy','持久累计恢复次数','ledger'],
 ['unknown','阻止盲目重放','runtime'],['audit','读取执行轨迹','ledger'],
 ['monitor','展示任务摘要','ledger'],['monitor','展示三类计划','plan'],
 ['security','限制访问','monitor'],['usage','记录实际回执而非字符估算','ledger'],
]
assert len({n['id'] for n in nodes}) == len(nodes)
assert all(a in {n['id'] for n in nodes} and b in {n['id'] for n in nodes} for a,_,b in edges)
(OUT/'harness-knowledge-graph.json').write_text(json.dumps({'snapshot':'2026-10-25','scope':'静态代码结构，不是实时运行状态','nodes':nodes,'edges':[{'source':a,'relation':r,'target':b} for a,r,b in edges]},ensure_ascii=False,indent=2),encoding='utf-8')

def font(size,bold=False): return ImageFont.truetype(BOLD if bold else FONT,size)
def draw_text(draw,xy,text,size,width,fill='#19324d',bold=False):
 x,y=xy; line=''
 for c in text:
  if draw.textlength(line+c,font=font(size,bold))>width:
   draw.text((x,y),line,font=font(size,bold),fill=fill);y+=size+10;line=c
  else:line+=c
 if line:draw.text((x,y),line,font=font(size,bold),fill=fill);y+=size+10
 return y

W=2800;H=3150
im=Image.new('RGB',(W,H),'#f3f6fb');d=ImageDraw.Draw(im)
d.rectangle((0,0,W,255),fill='#10283f')
draw_text(d,(80,40),'改造后系统知识图谱',62,2600,'#ffffff',True)
draw_text(d,(83,134),'Harness：计划驱动 · 边界执行 · 状态持久化 · 可验证恢复',32,2600,'#8fe5df')
draw_text(d,(83,194),f'代码快照 2026-10-25  |  {len(nodes)} 个实体 / {len(edges)} 条语义关系（完整关系见 JSON 与说明）',24,2600,'#c6d5e4')
colors=['#2563a6','#27857b','#7b52ad','#3469ab','#bc6c32','#237f8c','#78609c']
y=292
for i,(gid,title,change,children) in enumerate(layers):
 color=colors[i]
 d.rounded_rectangle((70,y,2730,y+293),radius=20,fill='white',outline='#d8e2ed',width=2)
 d.rounded_rectangle((90,y+20,555,y+272),radius=16,fill=color)
 draw_text(d,(115,y+53),title,37,410,'white',True)
 draw_text(d,(115,y+160),change,26,410,'#e4f4ff')
 for j,(ident,label,source) in enumerate(children):
  x=590+j*530
  d.rounded_rectangle((x,y+32,x+502,y+262),radius=12,fill='#f2f6fb',outline='#d7e1ed',width=2)
  end=draw_text(d,(x+22,y+54),label,29,458,bold=True)
  assert end < y+190,(label,end)
  draw_text(d,(x+22,y+192),source.split(';')[0],19,460,'#576d84')
 if i<6:
  d.line((1400,y+295,1400,y+319),fill='#90a8bd',width=4)
  d.polygon([(1390,y+312),(1410,y+312),(1400,y+324)],fill='#90a8bd')
 y+=325
draw_text(d,(80,2592),'关键执行关系',39,2600,bold=True)
for j,t in enumerate([
 '入口 → 应用编排 → 读取可信计划 → 阶段执行 → 工具 / 模型网关 → 原业务与数据源',
 '执行器 → 检查输入与依赖 → 真实执行及业务验收 → 保存检查点 → 下一阶段',
 '临时故障 → 白名单 + 恢复预算 → 有限重试；结果不明 → needs_review → 人工核对',
]):draw_text(d,(90,2660+j*74),t,29,2600)
draw_text(d,(80,2900),'边界：单机底座；非任意自主规划，不改代码 / 密钥 / 权限 / 策略，不自动交易。',28,2640,'#9c4e37',True)
draw_text(d,(80,2970),'层间箭头表示架构阅读顺序，不表示每个请求必经全部层。结构图不代表生产任务已成功运行。',25,2640,'#576d84')
im.save(OUT/'harness-system-knowledge-graph.png',dpi=(200,200))
im.resize((1120,1260)).save(OUT/'graph-preview.png')

comparisons=[
 ('执行计划','固定图与角色规则分布在业务模块','只读计划工具统一步骤、依赖、工具权限和验收'),
 ('主 Agent','已有分发、受限恢复；不是完全自主规划','迁入 control 层；模型意见仍受可信运行时约束'),
 ('阶段状态','已有业务记录、日志和局部互斥','新增统一运行账本、阶段检查点和输入指纹'),
 ('失败后恢复','局部重试和业务任务记录','同运行且同输入复用成功阶段；CLI 仅恢复正式任务'),
 ('工具与上下文','已有类型校验和业务证据规则','网关统一权限、资料截止时间、上下文与调用档案'),
 ('模型调用','已有服务商适配与 Token 统计','Harness 内发送前登记；留存实际 usage 与回执'),
 ('不确定结果','主要依赖原业务防重与错误处理','发布或模型请求结果不明时阻止自动重放'),
 ('恢复策略','已有临时故障重试白名单','结构化错误分类、持久累计预算、每日尝试上限'),
 ('监控与审计','已有主控监控和修复次数展示','管理员增加计划 / 最近任务；新增轨迹一致性验收'),
 ('代码结构','主控、模型适配与业务模块并列','Harness 分层归档；移除已核实废弃实现并备份样例'),
]
W2=2800;H2=2520
ci=Image.new('RGB',(W2,H2),'#f3f6fb');cd=ImageDraw.Draw(ci)
cd.rectangle((0,0,W2,240),fill='#10283f')
draw_text(cd,(80,43),'改造前后对比',60,2600,'white',True)
draw_text(cd,(83,139),'核心变化：不是增加几个 Agent，而是让执行过程受约束、可追踪、可恢复。',31,2630,'#8fe5df')
for x,label in [(80,'对比维度'),(540,'改造前：已有多 Agent 系统'),(1630,'改造后：统一 Harness 底座')]:draw_text(cd,(x,280),label,32,1050,bold=True)
for i,(title,before,after) in enumerate(comparisons):
 yy=350+i*164
 cd.rounded_rectangle((65,yy,2735,yy+148),radius=12,fill='white' if i%2==0 else '#e8eff7')
 draw_text(cd,(90,yy+28),title,30,415,bold=True)
 draw_text(cd,(540,yy+27),before,29,995)
 draw_text(cd,(1630,yy+27),after,29,1040,'#176f67')
draw_text(cd,(80,2040),'保持不变',36,2600,bold=True)
draw_text(cd,(80,2110),'原选股评分与准入规则、正式与影子隔离、千问兼容、原业务数据库、租户与权限保护。',29,2630)
draw_text(cd,(80,2220),'尚未实现 / 不能推断',36,2600,'#a35138',True)
draw_text(cd,(80,2290),'不是分布式平台；没有自动改代码或服务商账单对账；未提供旧影子报告自动恢复入口。',28,2630)
draw_text(cd,(80,2370),'此对比依据代码与 docs/harness.md，不将结构改造等同于线上故障全部解决。',25,2630,'#576d84')
ci.save(OUT/'harness-before-after.png',dpi=(200,200))
ci.resize((1120,1008)).save(OUT/'comparison-preview.png')

names={n['id']:n['label'] for n in nodes}
md=['# Harness 改造后的系统知识图谱与对比','','代码快照：2026-10-25。基线为上一版知识图谱和本次迁移记录；当前结构依据 `src/a_stock_agents/harness/`、应用入口及 `docs/harness.md`。本文件描述静态结构，不证明生产运行状态。','','## 本地文件','','- `harness-system-knowledge-graph.png`：改造后知识图谱，2800 × 3150。','- `harness-before-after.png`：改造前后对比，2800 × 2520。','- `harness-knowledge-graph.json`：完整实体、关系及源码索引。','- `export_graph.py`：可重复生成脚本，需要 Pillow。','','## 核心区别','','原系统已经有多 Agent、工具校验、局部重试、日志和业务持久化；改造不是从零增加这些能力，而是用可信计划、执行器、边界网关和运行账本统一约束执行。','','| 维度 | 改造前 | 改造后 |','|---|---|---|']
md += [f'| {a} | {b} | {c} |' for a,b,c in comparisons]
md += ['','## 执行与恢复边界','','- get_execution_plan 只返回可信模板，不会启动任务，不允许模型任意改写执行步骤。','- 正式、影子与盘中均接入阶段执行。原业务评分及证据校验保留。','- 成功检查点仅在运行编号、输入、计划匹配时复用。正式 CLI 恢复还限制同日和相同配置。','- 模型发送后无有效回执、或正式发布结果无法确认时，暂停核对，不盲目重放。','- 影子同实例可复用检查点，但没有新增旧影子报告自动恢复入口；盘中不重放已认领时段。','- 单机跨进程互斥不等于分布式队列或完整故障接管。','- 普通问答沿用原有调用与统计，不应理解为所有问答均已进入 Harness 账本。','- 不自动交易，不修改代码、密钥、权限或策略；没有服务商账单对账及人工批准重放接口。','','## 完整语义关系','',f'本图建模包含 {len(nodes)} 个实体、{len(edges)} 条关系；实体数量不代表能力提升幅度。','']
md += [f'{i}. {names[a]} — **{r}** → {names[b]}' for i,(a,r,b) in enumerate(edges,1)]
(OUT/'harness-comparison.md').write_text('\n'.join(md)+'\n',encoding='utf-8')
print(f'Generated {len(nodes)} nodes / {len(edges)} relationships in {OUT}')
