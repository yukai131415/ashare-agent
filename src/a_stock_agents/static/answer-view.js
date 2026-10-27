(() => {
  // Only create text nodes and a small allowlist of elements. Never interpret
  // provider text as HTML, scripts, event handlers or arbitrary links.
  function inline(parent,text){
    const parts=String(text).split(/(\*\*[^*\n]+\*\*|`[^`\n]+`)/g);
    for(const part of parts){if(part.startsWith('**')&&part.endsWith('**')){const strong=document.createElement('strong');strong.textContent=part.slice(2,-2);parent.append(strong);}else if(part.startsWith('`')&&part.endsWith('`')){const code=document.createElement('code');code.textContent=part.slice(1,-1);parent.append(code);}else parent.append(document.createTextNode(part));}
  }
  window.renderResearchAnswer=(text)=>{
    const container=document.getElementById('answer');container.className='answer answer-rich';container.replaceChildren();
    const toolbar=document.createElement('div');toolbar.className='answer-toolbar';const badge=document.createElement('span');badge.textContent='研究解读 · 完整正文';const copy=document.createElement('button');copy.type='button';copy.textContent='复制全文';copy.onclick=async()=>{try{await navigator.clipboard.writeText(String(text));copy.textContent='已复制';setTimeout(()=>copy.textContent='复制全文',2000);}catch{copy.textContent='复制失败，请选中正文复制';}};toolbar.append(badge,copy);container.append(toolbar);
    const article=document.createElement('article');article.className='answer-document';article.setAttribute('aria-label','大模型回答正文');
    let list=null,paragraph=null,inCode=false,code=null;
    for(const line of String(text??'').replace(/\r\n/g,'\n').split('\n')){
      if(/^\s*```/.test(line)){inCode=!inCode;list=paragraph=null;if(inCode){const pre=document.createElement('pre');code=document.createElement('code');pre.append(code);article.append(pre);}continue;}
      if(inCode){code.append(document.createTextNode(line+'\n'));continue;}
      const trimmed=line.trim();if(!trimmed){list=paragraph=null;continue;}
      if(/^([-*_])(?:\s*\1){2,}$/.test(trimmed)){article.append(document.createElement('hr'));list=paragraph=null;continue;}
      const heading=trimmed.match(/^(#{1,6})\s+(.+)$/);if(heading){const node=document.createElement('h'+Math.min(4,heading[1].length+1));inline(node,heading[2]);article.append(node);list=paragraph=null;continue;}
      const item=trimmed.match(/^(?:(\d+)[.、)]\s+|([-*+])\s+)(.*)$/);if(item){const tag=item[1]?'OL':'UL';if(!list||list.tagName!==tag){list=document.createElement(tag.toLowerCase());if(item[1])list.start=Number(item[1]);article.append(list);}const li=document.createElement('li');inline(li,item[3]);list.append(li);paragraph=null;continue;}
      list=null;if(trimmed.startsWith('>')){const quote=document.createElement('blockquote');inline(quote,trimmed.replace(/^>\s?/,''));article.append(quote);paragraph=null;continue;}
      if(!paragraph){paragraph=document.createElement('p');article.append(paragraph);}else paragraph.append(document.createElement('br'));inline(paragraph,line);
    }
    container.append(article);
  };
})();
