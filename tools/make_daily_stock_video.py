"""Read-only recommendation input; render a narrated vertical research video."""
import argparse
import csv
import json
import sqlite3
import subprocess
import sys
import wave
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools/video_deps'))
import imageio_ffmpeg


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', required=True)
    args = parser.parse_args()
    db = ROOT / 'runtime/agents/recommendation/data/recommendations.db'
    with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as con:
        con.row_factory = sqlite3.Row
        row = con.execute('SELECT * FROM recommendation_history WHERE recommendation_date=?', (args.date,)).fetchone()
    if not row:
        raise ValueError('No recommendation for this date')
    rec = dict(row)
    basis = json.loads(rec['basis_json'])
    if rec['model'] != 'balanced' or basis.get('news_factor_enabled') is not False:
        raise ValueError('This template requires balanced/no-news scoring')
    health = json.loads((ROOT / 'runtime/state/health.json').read_text(encoding='utf-8'))
    if not health.get('ok') or health.get('degraded') or not health['started_at'].startswith(args.date):
        raise ValueError('Cannot verify a successful same-day run')
    market = Path(health['agents']['market']['output_files'][0])
    with market.open(encoding='utf-8-sig', newline='') as f:
        quote = next(r for r in csv.DictReader(f) if r['code'] == rec['code'])
    stamp = quote['quote_time']
    if stamp[:8] != args.date.replace('-', ''):
        raise ValueError('Stale quote')
    out = ROOT / 'output/videos' / args.date
    out.mkdir(parents=True, exist_ok=True)
    name, code = rec['name'], rec['code']
    price = f"{rec['reference_price']:.2f}"
    change = float(basis['change_pct'])
    amount = float(basis['amount']) / 1e8
    turnover = float(basis['turnover_pct'])
    clock = f'{stamp[8:10]}:{stamp[10:12]}:{stamp[12:14]}'
    scenes = [
        ('今日模型观察', name, code, ['平衡多因子 · 每日一只', '看数据，也看风险'],
         f"今天的模型观察股，{name}。它为什么被选出来？我们只看数据，不猜明天涨跌。"),
        ('01 / 价格表现', f'{change:+.2f}%', '采集时涨跌幅', [f'参考价  {price} 元', f'行情时点  {clock}', '这是快照，不是当前报价'],
         f"采集时上涨百分之{change:.2f}，参考价{price}元。短期表现偏强，但上涨不等于明天还会涨。"),
        ('02 / 交易规模', f'{amount:.2f}', '亿元成交额', ['交易规模较大', '不等于资金净流入'],
         f"成交额约{amount:.2f}亿元，说明交易规模较大。注意，成交额不是资金净流入。"),
        ('03 / 交易活跃度', f'{turnover:.2f}%', '换手率', ['交投活跃', '高换手，也要防范大幅波动'],
         f"换手率百分之{turnover:.2f}，交投活跃，但高换手也可能伴随较大的价格波动。"),
        ('04 / 模型怎么看', f"{rec['score']:.3f}", '模型评分 · 不是胜率', ['平衡多因子  /  新闻不计分', f"观察目标日  {rec['target_date']}", '评分用于排序，不是收益承诺'],
         '平衡多因子综合市场数据排序，新闻不参与评分。模型分数不是胜率，更不是收益承诺。'),
        ('最后，记住风险', '观察 ≠ 买入', '独立判断 · 不盲目追涨', [f'{name}  {code}', '仅供研究，不构成投资建议', '历史表现不代表未来收益'],
         f"{name}，列入观察，不代表买入指令。别把模型当答案。仅供研究，不构成投资建议。"),
    ]
    manifest = [{'path': str(out / f'voice_{i}.wav'), 'text': s[4]} for i, s in enumerate(scenes)]
    (out / 'voice_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
    subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(ROOT / 'tools/video_voice.ps1'), '-Manifest', str(out / 'voice_manifest.json')], check=True)
    regular = 'C:/Windows/Fonts/msyh.ttc'
    bold = 'C:/Windows/Fonts/msyhbd.ttc'
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    subtitles, elapsed = [], 0
    def timecode(seconds):
        ms = round(seconds * 1000)
        return f'{ms//3600000:02}:{ms//60000%60:02}:{ms//1000%60:02},{ms%1000:03}'
    for i, (title, big, unit, lines, spoken) in enumerate(scenes):
        im = Image.new('RGB', (1080, 1920), '#080e19')
        d = ImageDraw.Draw(im)
        for y in range(1920):
            d.line((0, y, 1080, y), fill=(8 + int(8*y/1920), 14 + int(12*y/1920), 25 + int(17*y/1920)))
        for x in range(60, 1080, 80):
            d.line((x, 220, x, 1550), fill='#1c2837')
        for y in range(220, 1550, 80):
            d.line((60, y, 940, y), fill='#1c2837')
        def text(x, y, value, size=42, color='#f4f6fa', heavy=False):
            d.text((x, y), value, font=ImageFont.truetype(bold if heavy else regular, size), fill=color)
        accent = '#ff575f' if i in (0, 1, 2) else '#efbf67'
        text(76, 135, 'A股 / 模型观察', 34, '#a4b1c4')
        text(76, 192, args.date, 30, '#a4b1c4')
        d.rounded_rectangle((76, 295, 930, 365), 16, fill='#233041')
        text(100, 306, title, 34)
        size = 118 if len(big) <= 6 else 92
        text(72, 460, big, size, accent, True)
        text(80, 625, unit, 43, '#c5cddd')
        for j, line in enumerate(lines):
            y = 785 + j*140
            d.rounded_rectangle((76, y, 930, y+110), 18, fill='#172332', outline='#344356', width=2)
            text(100, y+28, line, 34)
        text(76, 1280, '数据：腾讯财经 · 系统推荐快照', 27, '#9daabc')
        text(76, 1325, f"生成 {rec['created_at'][11:19]}  /  非实时行情", 27, '#9daabc')
        # Burn in the narration as short readable lines, with comfortable UI-safe margins.
        wrapped = [spoken[n:n+22] for n in range(0, len(spoken), 22)]
        d.rounded_rectangle((60, 1420, 950, 1665), 20, fill='#070d16')
        for j, line in enumerate(wrapped[:4]):
            text(90, 1440+j*50, line, 36)
        text(76, 1730, '仅供研究，不构成投资建议', 30, '#a4b1c4')
        for j in range(6):
            d.rounded_rectangle((76+j*144, 1695, 204+j*144, 1702), 3, fill=accent if j <= i else '#334052')
        png = out / f'scene_{i}.png'
        im.save(png)
        if i == 0:
            im.save(out / '封面.png')
        wav = out / f'voice_{i}.wav'
        with wave.open(str(wav)) as audio:
            duration = audio.getnframes() / audio.getframerate() + .35
        subtitles.append(f'{i+1}\n{timecode(elapsed)} --> {timecode(elapsed+duration)}\n{spoken}\n')
        elapsed += duration
        command = [ffmpeg, '-y', '-loglevel', 'error', '-loop', '1', '-i', str(png), '-i', str(wav), '-t', str(duration), '-vf', f'fade=t=in:st=0:d=0.18,fade=t=out:st={duration-.18}:d=0.18', '-af', 'apad', '-r', '24', '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '21', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-ar', '48000', '-ac', '2', str(out / f'clip_{i}.mp4')]
        subprocess.run(command, check=True)
        print(f'Scene {i+1}/6 complete', flush=True)
    (out / 'concat.txt').write_text('\n'.join(f"file 'clip_{i}.mp4'" for i in range(6)), encoding='ascii')
    video = out / f'{args.date}_{name}_竖屏解读.mp4'
    subprocess.run([ffmpeg, '-y', '-loglevel', 'error', '-f', 'concat', '-safe', '0', '-i', str(out / 'concat.txt'), '-c', 'copy', '-movflags', '+faststart', str(video)], check=True)
    (out / '字幕.srt').write_text('\n'.join(subtitles), encoding='utf-8-sig')
    (out / '发布文案.txt').write_text(f'{args.date}模型观察：{name}，为什么进入今日观察名单？\n用涨跌幅、成交额、换手率拆解模型结果，关注风险，不盲目追涨。\n数据为{clock}快照，并非实时行情。仅供研究，不构成投资建议。\n#A股 #股票复盘 #量化研究 #风险提示\n\n配音：AI合成。发布时请如实标注AI生成内容，并自行确认账号及平台发布要求。', encoding='utf-8')
    print(json.dumps({'video': str(video), 'seconds': elapsed, 'bytes': video.stat().st_size}, ensure_ascii=False))


if __name__ == '__main__':
    main()
