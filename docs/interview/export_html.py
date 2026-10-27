"""Export the interview guide to a self-contained, searchable HTML document."""
import argparse
import html
from pathlib import Path
import re

parser=argparse.ArgumentParser()
parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args()
source=Path(__file__).with_name('Harness面试题与项目实战问答.md').read_text(encoding='utf-8')

def inline(text):
 text=html.escape(text)
 text=re.sub(r'`([^`]+)`',r'<code>\1</code>',text)
 text=re.sub(r'\*\*(.+?)\*\*',r'<strong>\1</strong>',text)
 return re.sub(r'\[([^\]]+)\]\((https?://[^\s)]+)\)',r'<a href="\2" target="_blank" rel="noopener noreferrer">\1</a>',text)

lines=source.splitlines();parts=[];toc=[];index=0;questions=0
while index<len(lines):
 line=lines[index];index+=1
 if not line.strip():continue
 if line.startswith('```'):
  block=[]
  while index<len(lines) and not lines[index].startswith('```'):block.append(lines[index]);index+=1
  index+=1;parts.append('<pre><code>'+html.escape('\n'.join(block))+'</code></pre>');continue
 match=re.match(r'^(#{1,3}) (.+)',line)
 if match:
  level=len(match[1]);title=match[2];qid=re.match(r'Q(\d+)：',title)
  anchor='q'+qid[1] if qid else 'section-'+str(index)
  if qid:questions+=1
  if level==2:toc.append(f'<a href="#{anchor}">{inline(title)}</a>')
  parts.append(f'<h{level} id="{anchor}">{inline(title)}</h{level}>');continue
 if line=='---':parts.append('<hr>');continue
 if line.startswith('|'):
  rows=[line]
  while index<len(lines) and lines[index].startswith('|'):rows.append(lines[index]);index+=1
  cells=lambda row:row.strip().strip('|').split('|')
  rendered=['<div class="table-wrap"><table><thead><tr>']
  rendered.extend('<th>'+inline(v.strip())+'</th>' for v in cells(rows[0]));rendered.append('</tr></thead><tbody>')
  for row in rows[2:]:rendered.append('<tr>'+''.join('<td>'+inline(v.strip())+'</td>' for v in cells(row))+'</tr>')
  rendered.append('</tbody></table></div>');parts.append(''.join(rendered));continue
 if re.match(r'^(- |\d+\. )',line):
  ordered=bool(re.match(r'^\d+\. ',line));tag='ol' if ordered else 'ul';items=[line]
  pattern=r'^\d+\. ' if ordered else r'^- '
  while index<len(lines) and re.match(pattern,lines[index]):items.append(lines[index]);index+=1
  parts.append('<'+tag+'>'+''.join('<li>'+inline(re.sub(pattern,'',item))+'</li>' for item in items)+'</'+tag+'>');continue
 parts.append('<p>'+inline(line)+'</p>')
assert questions==90
page=r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Harness 面试题与项目实战问答 · 90题</title>
<style>
:root{--ink:#24384c;--blue:#216fa7;--line:#dce5ef}*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:100px}body{margin:0;background:#f3f6fa;color:var(--ink);font:16px/1.9 "Microsoft YaHei",system-ui,sans-serif}header{padding:28px 36px;background:#16354f;color:white}header b{font-size:25px}header p{margin:5px 0;color:#cae3f6}.layout{display:grid;grid-template-columns:270px minmax(0,1000px);gap:26px;max-width:1340px;margin:26px auto;padding:0 20px}aside{position:sticky;top:20px;height:calc(100vh - 40px);overflow:auto;background:white;border:1px solid var(--line);border-radius:12px;padding:20px}nav a{display:block;padding:7px 0;font-size:13px;color:#38566f;text-decoration:none;border-bottom:1px solid #edf1f6}nav a:hover{color:var(--blue)}main{min-width:0;background:white;border:1px solid var(--line);padding:30px 38px;border-radius:12px}.toolbar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}input{width:100%;padding:10px 12px;border:1px solid #b5cadc;border-radius:7px;font:inherit;font-size:14px}button{padding:7px 10px;border:1px solid #c4d5e4;border-radius:6px;background:#edf5fc;color:#175a8a;cursor:pointer}h1{font-size:29px;line-height:1.5}h2{font-size:23px;border-left:4px solid var(--blue);padding-left:14px;margin:42px 0 22px}h3{font-size:18px;line-height:1.8;color:#175f91;margin:0 0 15px}p{margin:11px 0}.question{padding:23px;border:1px solid var(--line);border-radius:9px;margin:20px 0;background:#fcfdff}.question.hidden{display:none}code{font:14px/1.8 Consolas,monospace;background:#eaf0f6;padding:2px 5px;border-radius:3px;overflow-wrap:anywhere}pre{overflow:auto;background:#152d43;color:#e0edf7;padding:20px;border-radius:8px}pre code{background:transparent;color:inherit;white-space:pre;padding:0}a{color:var(--blue);overflow-wrap:anywhere}li{margin:5px 0}.table-wrap{overflow:auto}table{border-collapse:collapse;width:100%;font-size:14px}td,th{border:1px solid var(--line);padding:10px;text-align:left;vertical-align:top}th{background:#edf4fb}hr{border:0;border-top:1px solid var(--line);margin:30px 0}.muted{color:#6c8194;font-size:12px}.collapsed p,.collapsed ul,.collapsed ol,.collapsed pre,.collapsed .table-wrap{display:none}.question h3{cursor:pointer}.question h3:hover{text-decoration:underline}#count{min-height:24px}#empty{display:none;color:#965f14;background:#fff7e5;padding:15px;border-radius:8px}@media(max-width:900px){.layout{display:block}aside{position:static;height:auto;margin-bottom:18px}nav{max-height:170px;overflow:auto}main{padding:22px}header{padding:22px}h1{font-size:24px}}@media print{header,aside,.toolbar,#empty{display:none!important}.layout{display:block;margin:0;padding:0}main{border:0;padding:0}body{background:white;font-size:10pt;line-height:1.65}.question.hidden{display:block}.collapsed p,.collapsed ul,.collapsed ol,.collapsed pre,.collapsed .table-wrap{display:block}.question{background:white;border:0;border-top:1px solid #ccc;padding:12px 0;margin:8px 0}h2,h3{break-after:avoid}h1{font-size:20pt}h2{font-size:15pt}h3{font-size:12pt}pre{white-space:pre-wrap}a{color:inherit}button{display:none}}
</style></head><body><header><b>Harness 面试备考手册</b><p>90 道问答 · 通用架构 + 当前项目实战 · 离线可用</p></header><div class="layout"><aside><div class="toolbar"><input id="search" aria-label="搜索题目或答案" placeholder="搜索：幂等、恢复、主Agent…"><button id="expand">展开全部</button><button id="collapse">折叠答案</button><button id="print">打印 / PDF</button></div><div id="count" class="muted">90 / 90 道题</div><p class="muted">点击题目可折叠答案。打印时包含全部题目。</p><nav>__TOC__</nav></aside><main id="content"><div id="empty">没有匹配的题目，请更换关键词。</div>__BODY__</main></div><script>
const headings=[...document.querySelectorAll('h3[id^="q"]')];
for(const h of headings){const section=document.createElement('section');section.className='question';h.before(section);section.append(h);while(section.nextElementSibling&&!/^H[123]$/.test(section.nextElementSibling.tagName)){section.append(section.nextElementSibling)}h.tabIndex=0;h.setAttribute('role','button');h.setAttribute('aria-expanded','true');function toggle(){section.classList.toggle('collapsed');h.setAttribute('aria-expanded',!section.classList.contains('collapsed'))}h.onclick=toggle;h.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();toggle()}}}
const questions=[...document.querySelectorAll('.question')];
document.getElementById('search').addEventListener('input',e=>{const words=e.target.value.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);let shown=0;for(const q of questions){const match=words.every(w=>q.textContent.toLocaleLowerCase().includes(w));q.classList.toggle('hidden',!match);if(match){shown++;if(words.length){q.classList.remove('collapsed');q.querySelector('h3').setAttribute('aria-expanded','true')}}}document.getElementById('count').textContent=`${shown} / ${questions.length} 道题`;document.getElementById('empty').style.display=shown?'none':'block'});
for(const [id,collapse] of [['expand',false],['collapse',true]])document.getElementById(id).onclick=()=>questions.forEach(q=>{q.classList.toggle('collapsed',collapse);q.querySelector('h3').setAttribute('aria-expanded',!collapse)});
document.getElementById('print').onclick=()=>window.print();
document.querySelectorAll('nav a').forEach(a=>a.addEventListener('click',()=>{document.getElementById('search').value='';document.getElementById('search').dispatchEvent(new Event('input'))}));
</script></body></html>'''
page=page.replace('__TOC__',''.join(toc)).replace('__BODY__','\n'.join(parts))
args.output.parent.mkdir(parents=True,exist_ok=True)
args.output.write_text(page,encoding='utf-8')
assert page.count('<h3 id="q')==90
print(f'Saved: {args.output}; questions={questions}; bytes={args.output.stat().st_size}')
