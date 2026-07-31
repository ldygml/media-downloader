"""
多平台媒体下载器 - Web 服务
支持: 抖音 / B站（bilibili）
PC 端运行，手机和 PC 都可以通过浏览器访问
"""

import os
import sys
import socket
import time
import logging
import threading
import webbrowser
from collections import defaultdict
from functools import wraps

from flask import Flask, request, jsonify, send_file, render_template
from core import fetch_media_info, fetch_media_from_douyin, download_file, sanitize_filename, detect_platform

# ===== 配置（支持环境变量覆盖）=====
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
    # 打包版位于 dist/ 下时，统一到项目根目录（与 python 版一致）
    if os.path.basename(BASE_DIR).lower() == 'dist':
        BASE_DIR = os.path.dirname(BASE_DIR)
    # PyInstaller 解压目录：模板/静态资源打包在这里
    _RESOURCE_DIR = getattr(sys, '_MEIPASS', BASE_DIR)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    _RESOURCE_DIR = BASE_DIR
DOWNLOAD_DIR = os.path.join(BASE_DIR, os.environ.get('DOWNLOAD_DIR', 'MediaSaver_downloads'))
HOST = os.environ.get('HOST', '0.0.0.0')
PORT = int(os.environ.get('PORT', 8932))
RATE_LIMIT = int(os.environ.get('RATE_LIMIT', '30'))  # 每 IP 每分钟最大请求数
RATE_WINDOW = 60  # 窗口秒数

app = Flask(__name__, template_folder=os.path.join(_RESOURCE_DIR, 'templates'))
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ===== 文件日志 =====
_log_path = os.path.join(BASE_DIR, 'mediasaver.log')
logging.basicConfig(
    filename=_log_path,
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    encoding='utf-8',
)
logger = logging.getLogger('mediasaver')
# 同时输出到控制台
_console = logging.StreamHandler()
_console.setLevel(logging.INFO)
logger.addHandler(_console)

# ===== PWA 图标 =====
ASSETS_DIR = os.path.join(_RESOURCE_DIR, 'assets')
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




@app.route('/')
def index():
    return render_template('index.html', port=PORT)


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
        logger.info('extract douyin: %s -> success=%s type=%s', url[:60], result.get('success'), result.get('type'))
        if not result.get('success'):
            logger.error('extract douyin failed: %s error=%s', url[:60], result.get('error'))
        return jsonify(result)

    # B站 / 其他 → 直接 HTTP API
    result = fetch_media_info(url)
    logger.info('extract %s: %s -> success=%s', platform, url[:60], result.get('success'))
    return jsonify(result)


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
        logger.error('download failed: %s author=%s title=%s', url[:60], author, title[:30])
        return jsonify({'success': False, 'error': '下载失败'})
    logger.info('download ok: %s files=%d', url[:60], len(files))
    return jsonify({'success': True, 'files': files, 'count': len(files)})


@app.route('/file/')
def serve_file():
    filename = request.args.get('name', '')
    if not filename:
        return 'invalid', 400
    # 只允许纯文件名（不含路径分隔符），防路径穿越
    if '/' in filename or '\\' in filename or os.path.basename(filename) != filename:
        logger.warning('file rejected (bad name): %r', filename[:60])
        return 'invalid', 400
    path = os.path.join(DOWNLOAD_DIR, filename)
    # 最终校验：真实路径必须在下载目录内
    if not os.path.realpath(path).startswith(os.path.realpath(DOWNLOAD_DIR)):
        logger.warning('file rejected (path escape): %r', filename[:60])
        return 'invalid', 400
    if not os.path.exists(path):
        logger.warning('file not found: %r', filename[:60])
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
