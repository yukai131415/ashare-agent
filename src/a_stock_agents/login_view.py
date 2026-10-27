"""Self-contained authentication screen; decorative charts never represent quotes."""
from pathlib import Path


def login_page(setup=False):
    assets=Path(__file__).with_name('static')
    extra='''<div class="setup-fields"><label for="display-name">显示名称<span class="optional">选填</span><input id="display-name" name="display_name" autocomplete="name" maxlength="80" placeholder="怎么称呼你"></label><label for="tenant-name">租户名称<input id="tenant-name" name="tenant_name" value="默认租户" maxlength="80" required></label></div>''' if setup else ''
    replacements={
        '__TITLE__':'开启你的投研工作台' if setup else '欢迎回到投研工作台',
        '__MODE__':'首次初始化' if setup else '身份验证',
        '__SUBTITLE__':'创建首个管理员账号，建立专属研究空间。' if setup else '登录后，继续你的数据洞察与策略研究。',
        '__EXTRA__':extra,
        '__AUTOCOMPLETE__':'new-password' if setup else 'current-password',
        '__BUTTON__':'创建管理员并进入' if setup else '登录工作台',
        '__ENDPOINT__':'setup' if setup else 'login',
        '__FOOTNOTE__':'首次设置仅支持本机操作，无内置默认密码。' if setup else '还没有账号？请联系管理员或本租户经理开通。',
        '__CSS__':(assets/'auth-login.css').read_text(encoding='utf-8'),
        '__JS__':(assets/'auth-login.js').read_text(encoding='utf-8'),
    }
    html=TEMPLATE
    for key,value in replacements.items():html=html.replace(key,value)
    return html


TEMPLATE='''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="dark"><title>登录 · A股多 Agent 投研工作台</title><style>__CSS__</style></head>
<body><div class="ambient ambient-one" aria-hidden="true"></div><div class="ambient ambient-two" aria-hidden="true"></div><div class="page-grid" aria-hidden="true"></div>
<div class="auth-shell"><header class="brand-bar"><a href="/" class="brand" aria-label="A股多Agent投研工作台首页"><span class="brand-symbol" aria-hidden="true"><svg viewBox="0 0 32 32"><path d="M5 23 12 9l6 10 8-14M6 27h21"/></svg></span><span>A股多 Agent<span class="brand-sub">RESEARCH WORKSPACE</span></span></a><span class="terminal-label"><i></i> 多因子 · 多视角 · 有依据</span></header>
<main class="auth-layout"><section class="story" aria-labelledby="story-title"><div class="eyebrow"><span></span> YOUR RESEARCH, CONNECTED</div><h1 id="story-title">让数据成为洞察，<br>让研究<span class="gradient-text">有迹可循。</span></h1><p class="story-copy">从行情采集到决策汇总，串联每一次分析。<br>在一个工作台里，看清信号、依据与风险。</p>
<div class="signal-card" aria-label="投研链路展示示意"><div class="signal-header"><span><i class="live-dot"></i> AGENT SIGNAL FLOW</span><span class="demo-label">可视化示意 · 非实时行情</span></div>
<div class="chart-area" aria-hidden="true"><div class="chart-grid"></div><div class="chart-caption"><span>多视角协同研究</span><strong>连接数据与决策</strong></div><svg class="signal-chart" viewBox="0 0 600 160" preserveAspectRatio="none"><defs><linearGradient id="line-grad"><stop stop-color="#48a8ff"/><stop offset="1" stop-color="#65f3d1"/></linearGradient><linearGradient id="fill-grad" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#52d5dc" stop-opacity=".22"/><stop offset="1" stop-color="#52d5dc" stop-opacity="0"/></linearGradient></defs><path class="chart-fill" d="M0 135 28 128 52 136 80 105 105 111 130 88 156 99 182 73 208 83 237 61 262 75 290 65 315 79 343 50 370 56 400 31 428 44 456 29 483 40 511 16 540 26 568 12 600 18V160H0Z"/><path class="chart-reference" d="M0 143 60 126 120 132 180 105 240 113 300 92 360 102 420 78 480 89 540 62 600 72"/><path class="chart-line" d="M0 135 28 128 52 136 80 105 105 111 130 88 156 99 182 73 208 83 237 61 262 75 290 65 315 79 343 50 370 56 400 31 428 44 456 29 483 40 511 16 540 26 568 12 600 18"/><circle class="chart-dot" cx="568" cy="12" r="4"/></svg><div class="chart-scan"></div></div>
<ol class="agent-flow"><li><span>01</span>数据获取</li><li><span>02</span>基本面</li><li><span>03</span>技术面</li><li><span>04</span>风控审查</li><li><span>05</span>决策汇总</li></ol></div>
<div class="feature-row"><div><span class="feature-icon">◈</span><strong>多 Agent 协作</strong><small>分析过程清晰可追溯</small></div><div><span class="feature-icon">⌁</span><strong>原模型研究</strong><small>因子与依据集中呈现</small></div><div><span class="feature-icon">▦</span><strong>租户独立空间</strong><small>分级权限与用量归属</small></div></div></section>
<section class="login-card" aria-labelledby="login-title"><div class="card-topline"><span class="access-mark" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M12 3 20 6v6c0 5-8 9-8 9s-8-4-8-9V6Z"/><path d="m8 12 3 3 5-6"/></svg></span><span class="mode-pill">__MODE__</span></div><h2 id="login-title">__TITLE__</h2><p class="form-intro">__SUBTITLE__</p>
<form id="login" data-endpoint="__ENDPOINT__"><label for="username">登录账号<input id="username" name="username" autocomplete="username" required pattern="[A-Za-z0-9_.-]{3,64}" maxlength="64" placeholder="请输入你的账号" aria-describedby="username-hint"></label><span id="username-hint" class="input-hint">3–64 位字母、数字或 _ . -</span>__EXTRA__<label for="password">登录密码<div class="password-field"><input id="password" name="password" type="password" autocomplete="__AUTOCOMPLETE__" minlength="10" maxlength="256" required placeholder="请输入至少 10 位密码"><button type="button" class="password-toggle" aria-label="显示密码" aria-pressed="false"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg></button></div></label><div class="form-meta"><span><i></i> 受保护的登录会话</span><span>密码不少于 10 位</span></div><button type="submit" class="submit-button"><span class="submit-label">__BUTTON__</span><span class="submit-arrow" aria-hidden="true">↗</span></button><p id="error" role="alert" aria-live="polite"></p></form>
<p class="account-help">__FOOTNOTE__</p><div class="access-info"><span class="access-info-title">按角色开放工作台</span><div class="role-chips"><span>管理员</span><span>经理</span><span>普通用户</span></div><p>普通用户仅可访问监控总览与每日简报；<br>账号管理与 Token 统计按角色授权。</p></div><div class="privacy-note"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/></svg>密码加密存储 · 连接密钥不在页面回显</div></section></main>
<footer class="auth-footer"><span>A-SHARE INTELLIGENCE / 多 Agent 投研工作台</span><span>仅供数据与量化研究，不构成投资建议</span><time id="auth-clock" aria-label="北京时间"></time></footer></div><script>__JS__</script></body></html>'''
