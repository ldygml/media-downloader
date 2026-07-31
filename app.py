"""
多平台媒体下载器 - Web 服务
支持: 抖音 / B站（bilibili）
PC 端运行，手机和 PC 都可以通过浏览器访问
"""

import os
import sys
import socket
import time
import threading
import webbrowser
from collections import defaultdict
from functools import wraps

from flask import Flask, request, jsonify, send_file, render_template_string
from core import fetch_media_info, fetch_media_from_douyin, download_file, sanitize_filename, detect_platform

# ===== 配置（支持环境变量覆盖）=====
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, os.environ.get('DOWNLOAD_DIR', 'MediaSaver_downloads'))
HOST = os.environ.get('HOST', '0.0.0.0')
PORT = int(os.environ.get('PORT', 8932))
RATE_LIMIT = int(os.environ.get('RATE_LIMIT', '30'))  # 每 IP 每分钟最大请求数
RATE_WINDOW = 60  # 窗口秒数

app = Flask(__name__)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ===== PWA 图标 =====
if getattr(sys, 'frozen', False):
    ASSETS_DIR = os.path.join(os.path.dirname(sys.executable), 'assets')
else:
    ASSETS_DIR = os.path.join(BASE_DIR, 'assets')
os.makedirs(ASSETS_DIR, exist_ok=True)

def _generate_icons():
    """生成 PWA 图标（192x192 + 512x512）"""
    from PIL import Image, ImageDraw
    for size in (192, 512):
        path = os.path.join(ASSETS_DIR, f'icon-{size}.png')
        if os.path.exists(path):
            continue
        img = Image.new('RGBA', (size, size), (99, 102, 241, 255))
        draw = ImageDraw.Draw(img)
        # 居中画一个向下箭头
        cx, cy = size // 2, size // 2
        r = size * 0.3
        arrow_color = (255, 255, 255, 230)
        # 箭头主体（圆形背景上的三角形）
        tri = [
            (cx, int(cy + r * 0.7)),           # 下顶点
            (int(cx - r * 0.6), int(cy - r * 0.2)),  # 左上
            (int(cx + r * 0.6), int(cy - r * 0.2)),  # 右上
        ]
        draw.polygon(tri, fill=arrow_color)
        # 小横杠
        bar_y = int(cy - r * 0.5)
        draw.rectangle([int(cx - r * 0.35), bar_y, int(cx + r * 0.35), bar_y + size // 20], fill=arrow_color)
        img.save(path, 'PNG')

_generate_icons()

# ===== 速率限制（内存计数）=====
_rate_store: dict[str, list[float]] = defaultdict(list)

def rate_limit(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        ip = request.remote_addr or 'unknown'
        now = time.time()
        window = RATE_WINDOW
        # 清理过期记录
        _rate_store[ip] = [t for t in _rate_store[ip] if now - t < window]
        if len(_rate_store[ip]) >= RATE_LIMIT:
            return jsonify({'success': False, 'error': f'请求过于频繁，请{RATE_WINDOW}秒后再试'}), 429
        _rate_store[ip].append(now)
        return f(*args, **kwargs)
    return wrapper

# ===== Selenium 队列（一次只跑一个浏览器）=====
_selenium_lock = threading.Lock()
_selenium_busy = False

def run_selenium_task(task_fn, *args, **kwargs):
    """串行化 Selenium 任务"""
    global _selenium_busy
    if _selenium_busy:
        return {'success': False, 'error': '服务器忙，有其他任务正在处理，请稍后再试'}
    _selenium_busy = True
    try:
        return task_fn(*args, **kwargs)
    finally:
        _selenium_busy = False

HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,viewport-fit=cover">
<meta name="theme-color" content="#0a0a1a">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="MediaSaver">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/assets/icon-192.png">
<link rel="icon" href="/assets/icon-192.png" type="image/png">
<title>MediaSaver</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC',sans-serif;
    background:linear-gradient(135deg,#0a0a1a 0%,#16163a 50%,#0f0f2e 100%);
    color:#e0e0ff;
    min-height:100vh;
    display:flex;justify-content:center;
    padding:20px;
}
.container{
    max-width:680px;width:100%;
    padding:32px 24px;
}
.logo{
    text-align:center;margin-bottom:8px;
}
.app-icon{
    width:64px;height:64px;border-radius:14px;
    margin-bottom:6px;display:inline-block;
    image-rendering:pixelated;
    box-shadow:0 4px 20px rgba(99,102,241,.3);
}
.logo h1{
    font-size:2rem;font-weight:800;
    background:linear-gradient(135deg,#6366f1,#a855f7,#ec4899);
    -webkit-background-clip:text;-webkit-text-fill-color:transparent;
    background-clip:text;
    letter-spacing:1px;
}
.logo p{
    color:#8888bb;font-size:.88rem;margin-top:4px;
}
.card{
    background:rgba(30,30,70,.7);
    backdrop-filter:blur(12px);
    border:1px solid rgba(255,255,255,.08);
    border-radius:16px;
    padding:24px;margin-bottom:16px;
    transition:all .3s;
}
.card:hover{border-color:rgba(255,255,255,.15)}
label{
    display:block;font-size:.82rem;color:#8888cc;
    margin-bottom:8px;font-weight:600;text-transform:uppercase;
    letter-spacing:1px;
}
.input-row{
    display:flex;flex-direction:column;gap:10px;
}
input{
    flex:1;padding:14px 18px;
    border:1.5px solid rgba(255,255,255,.12);
    border-radius:12px;background:rgba(20,20,50,.8);
    color:#e0e0ff;font-size:.95rem;outline:none;
    transition:border-color .2s,box-shadow .2s;
}
input:focus{border-color:#6366f1;box-shadow:0 0 0 3px rgba(99,102,241,.2)}
.btn{
    padding:14px 24px;border:none;border-radius:12px;
    font-size:.95rem;font-weight:700;cursor:pointer;
    transition:all .2s;white-space:nowrap;
    display:flex;align-items:center;justify-content:center;gap:6px;
}
.btn-primary{
    background:linear-gradient(135deg,#6366f1,#7c3aed);
    color:#fff;box-shadow:0 4px 15px rgba(99,102,241,.3);
}
.btn-primary:hover:not(:disabled){
    transform:translateY(-1px);box-shadow:0 6px 20px rgba(99,102,241,.4);
}
.btn-primary:disabled{opacity:.5;cursor:not-allowed;transform:none;box-shadow:none}
.btn-secondary{
    background:rgba(255,255,255,.08);color:#ccc;
}
.btn-secondary:hover:not(:disabled){background:rgba(255,255,255,.14)}
.status{
    margin-top:16px;padding:16px;border-radius:12px;
    font-size:.88rem;text-align:center;
    transition:all .3s;display:none;
}
.status.show{display:block}
.status.loading{background:rgba(99,102,241,.1);color:#a5b4fc}
.status.success{background:rgba(16,185,129,.1);color:#6ee7b7}
.status.error{background:rgba(239,68,68,.1);color:#fca5a5}
.spinner{
    display:inline-block;width:18px;height:18px;
    border:2px solid rgba(255,255,255,.2);
    border-top-color:#a5b4fc;border-radius:50%;
    animation:spin .8s linear infinite;vertical-align:middle;
    margin-right:8px;
}
@keyframes spin{to{transform:rotate(360deg)}}
.platform-badge{
    display:inline-flex;align-items:center;gap:4px;
    padding:4px 10px;border-radius:6px;
    font-size:.75rem;font-weight:600;
}
.badge-douyin{background:rgba(0,0,0,.3);color:#06d6a0}
.badge-bilibili{background:rgba(251,114,153,.2);color:#fb7299}
.result-item{
    display:flex;align-items:center;gap:14px;
    padding:14px 0;border-bottom:1px solid rgba(255,255,255,.06);
}
.result-item:last-child{border:none}
.result-icon{
    width:40px;height:40px;border-radius:10px;
    display:flex;align-items:center;justify-content:center;
    font-size:1.2rem;flex-shrink:0;
}
.icon-video{background:rgba(99,102,241,.15)}
.icon-image{background:rgba(16,185,129,.15)}
.result-info{flex:1;min-width:0}
.result-name{font-size:.85rem;word-break:break-all;margin-bottom:2px}
.result-meta{font-size:.75rem;color:#888;display:flex;gap:12px}
.download-btn{
    padding:8px 18px;background:rgba(16,185,129,.15);
    color:#6ee7b7;border-radius:8px;text-decoration:none;
    font-size:.82rem;font-weight:600;white-space:nowrap;
    flex-shrink:0;transition:all .2s;border:1px solid rgba(16,185,129,.2);
}
.download-btn:hover{background:rgba(16,185,129,.25)}
.meta-line{
    color:#888;font-size:.83rem;margin-bottom:16px;
    padding:12px;background:rgba(255,255,255,.03);border-radius:8px;
}
.meta-author{color:#a5b4fc;font-weight:600}
.meta-badge{color:#888;margin-left:8px}
.ip-hint{
    text-align:center;font-size:.8rem;color:#555;margin-top:20px;
}
.ip-hint code{color:#777;background:rgba(255,255,255,.05);padding:2px 6px;border-radius:4px}
.qr-wrap{
    text-align:center;margin:16px 0 8px;
}
.qr-wrap svg{
    border-radius:12px;background:#fff;padding:6px;
    width:128px;height:128px;max-width:100%;
}
.qr-desc{font-size:.72rem;color:#555;margin-top:6px}
@media(max-width:480px){
    .container{padding:16px 12px}
    .logo h1{font-size:1.5rem}
    .card{padding:18px}
    .qr-wrap svg{width:100px;height:100px}
}
</style>
</head>
<body>
<div class="container">
<div class="logo">
<img src="/assets/icon-192.png" alt="MediaSaver" class="app-icon">
<h1>MediaSaver</h1>
<p>支持抖音 / B站，自动识别 · 无水印下载</p>
</div>

<div class="card">
<label>作品链接</label>
<div class="input-row">
<input type="text" id="url" placeholder="粘贴分享链接或完整文案" autofocus onkeydown="if(event.key==='Enter')go()">
<button class="btn btn-primary" id="btn" onclick="go()">解析</button>
</div>
<div id="status"></div>
<div class="qr-wrap" id="qrWrap"></div>
<p class="ip-hint" id="pwaHint">📱 手机打开 <code id="ipHint">...</code><br><small>添加到主屏幕使用更方便</small></p>
</div>

<div id="results" class="card" style="display:none">
<div id="meta" class="meta-line"></div>
<div id="files"></div>
</div>
</div>
<script>
var _extractData=null;
async function go(){
    var url=document.getElementById('url').value.trim();
    if(!url){alert('请粘贴链接');return}
    var btn=document.getElementById('btn'),st=document.getElementById('status');
    btn.disabled=true;btn.innerHTML='<span class="spinner"></span>解析中';
    st.className='status loading show';
    st.innerHTML='<span class="spinner"></span>正在提取媒体数据...<br><small style="color:#888">抖音需要几秒启动浏览器</small>';
    document.getElementById('results').style.display='none';
    try{
        var r=await fetch('/extract',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url})});
        var d=await r.json();_extractData=d;
        if(!d.success){
            st.className='status error show';
            st.innerHTML='❌ '+(d.error||'解析失败');
            btn.disabled=false;btn.innerHTML='重新解析';return;
        }
        // 平台标识
        var plat='';
        if(url.includes('bilibili')||url.includes('b23.tv')||(d.type==='video'&&!url.includes('douyin'))){
            plat='<span class="platform-badge badge-bilibili">B站</span>';
        }else{
            plat='<span class="platform-badge badge-douyin">抖音</span>';
        }
        var isVideo=d.type==='video';
        st.className='status success show';
        st.innerHTML='✅ '+(isVideo?'视频':'图片')+'提取成功，正在下载...';
        var dl=await fetch('/download',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({url,video_url:d.video_url||'',images:d.images||[],author:d.author||'',title:d.title||'',
            _cookies:d._cookies||{}})});
        var dd=await dl.json();
        if(!dd.success){
            st.className='status error show';st.innerHTML='❌ '+(dd.error||'下载失败');
            btn.disabled=false;btn.innerHTML='⬇ 解析并下载';return;
        }
        st.className='status success show';
        st.innerHTML='✅ 完成！共 <b>'+dd.count+'</b> 个文件';
        btn.disabled=false;btn.innerHTML='⬇ 解析并下载';
        // 元信息
        var meta=''+plat;
        if(d.author) meta+=' <span class="meta-author">@'+d.author+'</span>';
        if(d.title) meta+=' <span class="meta-badge">'+d.title.substring(0,50)+'</span>';
        document.getElementById('meta').innerHTML=meta;
        var h='';
        for(var f of dd.files){
            var icon=f.type==='video'?'icon-video':'icon-image';
            var emoji=f.type==='video'?'🎬':'🖼️';
            h+='<div class="result-item"><div class="result-icon '+icon+'">'+emoji+'</div>'+
                '<div class="result-info"><div class="result-name">'+f.name+'</div>'+
                '<div class="result-meta"><span>'+f.type+'</span><span>'+f.size+'</span></div></div>'+
                '<a href="/file/?name='+encodeURIComponent(f.id)+'" class="download-btn">⬇ 下载</a></div>';
        }
        document.getElementById('files').innerHTML=h;
        document.getElementById('results').style.display='block';
    }catch(e){
        st.className='status error show';st.innerHTML='❌ 请求失败: '+e.message;
        btn.disabled=false;btn.innerHTML='⬇ 解析并下载';
    }
}
fetch('/ip').then(r=>r.text()).then(ip=>{
    var url=ip+':{{port}}';
    document.getElementById('ipHint').innerHTML=url;
    // QR 码
    var qrWrap=document.getElementById('qrWrap');
    var qrUrl='http://'+url;
    var qrImg=document.createElement('img');
    qrImg.src='https://api.qrserver.com/v1/create-qr-code/?size=200x200&data='+encodeURIComponent(qrUrl);
    qrImg.alt='QR Code';
    qrImg.style='border-radius:12px;background:#fff;padding:6px;width:128px;height:128px;max-width:100%';
    qrWrap.appendChild(qrImg);
    var desc=document.createElement('div');
    desc.className='qr-desc';
    desc.innerHTML='📱 扫码访问';
    qrWrap.appendChild(desc);
});
// PWA: 注册 Service Worker
if('serviceWorker' in navigator){
    navigator.serviceWorker.register('/sw.js').catch(function(){});
}
</script>
</body>
</html>"""


@app.route('/')
def index():
    return render_template_string(HTML, port=PORT)


@app.route('/manifest.json')
def manifest():
    return {
        'name': 'MediaSaver 媒体下载',
        'short_name': 'MediaSaver',
        'description': '抖音 / B站 无水印媒体下载工具',
        'start_url': '/',
        'display': 'standalone',
        'background_color': '#0a0a1a',
        'theme_color': '#6366f1',
        'icons': [
            {'src': '/assets/icon-192.png', 'sizes': '192x192', 'type': 'image/png'},
            {'src': '/assets/icon-512.png', 'sizes': '512x512', 'type': 'image/png'},
        ],
    }


@app.route('/sw.js')
def service_worker():
    sw = '''self.addEventListener('fetch',function(e){e.respondWith(fetch(e.request).catch(function(){return new Response('Offline',{status:503})}))})'''
    return app.response_class(sw, mimetype='application/javascript')


@app.route('/assets/<path:filename>')
def serve_asset(filename):
    return send_file(os.path.join(ASSETS_DIR, filename))


@app.route('/ip')
def get_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return 'localhost'


@app.route('/health')
def health():
    return {'ok': True, 'busy': _selenium_busy}


@app.route('/extract', methods=['POST'])
@rate_limit
def extract():
    url = (request.get_json() or {}).get('url', '').strip()
    if not url:
        return jsonify({'success': False, 'error': '请提供链接'})

    platform = detect_platform(url)

    # 抖音需要 Selenium → 走队列
    if platform == 'douyin':
        result = run_selenium_task(fetch_media_info, url)
        return jsonify(result)

    # B站 / 其他 → 直接 HTTP API
    return jsonify(fetch_media_info(url))


@app.route('/download', methods=['POST'])
def download():
    data = request.get_json() or {}
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'success': False, 'error': '请提供链接'})

    # 如果前端传了 video_url 和 images，直接下载（不重新提取）
    video_url = data.get('video_url', '').strip()
    image_urls = data.get('images', [])
    author = data.get('author', '').strip()
    title = data.get('title', '').strip()
    cookies = data.get('_cookies', {}) or {}

    if not video_url and not image_urls:
        # 回退：重新提取（B站场景或旧前端）
        platform = detect_platform(url)
        if platform == 'bilibili':
            from bilibili import download_bilibili
            result = download_bilibili(url, DOWNLOAD_DIR)
            return jsonify(result)
        info = fetch_media_info(url)
        if not info.get('success'):
            return jsonify({'success': False, 'error': info.get('error', '解析失败')})
        author = info.get('author', 'unknown')
        title = info.get('title', 'untitled')
        video_url = info.get('video_url', '')
        image_urls = info.get('images', [])

    base = sanitize_filename(
        f"{author}_{title[:40]}"
    ) or f'media_{hash(url) % 100000}'

    files = []

    if video_url:
        path = os.path.join(DOWNLOAD_DIR, base + '.mp4')
        if download_file(video_url, path, cookies=cookies):
            size = os.path.getsize(path)
            files.append({'id': base + '.mp4', 'name': base + '.mp4', 'type': 'video',
                          'size': f'{size/1024/1024:.1f} MB' if size > 1024*1024 else f'{size/1024:.0f} KB'})

    for i, img_url in enumerate(image_urls):
        fname = f'{base}_img_{i+1}.jpg'
        path = os.path.join(DOWNLOAD_DIR, fname)
        if download_file(img_url, path, cookies=cookies):
            size = os.path.getsize(path)
            files.append({'id': fname, 'name': fname, 'type': 'image', 'size': f'{size/1024:.0f} KB'})

    if not files:
        return jsonify({'success': False, 'error': '下载失败'})
    return jsonify({'success': True, 'files': files, 'count': len(files)})


@app.route('/file/')
def serve_file():
    filename = request.args.get('name', '')
    if not filename or '..' in filename or '/' in filename or '\\' in filename:
        return 'invalid', 400
    path = os.path.join(DOWNLOAD_DIR, filename)
    if not os.path.exists(path):
        return 'not found', 404
    return send_file(path, as_attachment=True)


def open_browser():
    import time
    time.sleep(1.5)
    webbrowser.open(f'http://localhost:{PORT}')


# ===== 启动（gunicorn 会加载 app，此块仅本地开发用）=====
if __name__ == '__main__':
    is_prod = os.environ.get('RAILWAY_SERVICE_ID') or os.environ.get('GUNICORN_CMD_ARGS')
    print('  ╔═══════════════════════════════════════╗')
    print('  ║     MediaSaver 媒体下载服务           ║')
    print(f'  ║  http://localhost:{PORT}               ')
    print('  ╚═══════════════════════════════════════╝')
    if not is_prod:
        threading.Thread(target=open_browser, daemon=True).start()
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
