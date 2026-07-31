"""
抖音/B站 无水印媒体提取核心引擎
自动识别链接所属平台并调用对应的提取逻辑
"""

import os
import re
import time
import json
import requests
from urllib.parse import urlparse

from selenium import webdriver

# ===== 平台路由 =====

def detect_platform(url_or_text: str) -> str:
    """识别链接属于哪个平台: douyin | bilibili | xiaohongshu | unknown"""
    t = url_or_text.lower()
    if 'douyin.com' in t or 'iesdouyin.com' in t:
        return 'douyin'
    if 'bilibili.com' in t or 'b23.tv' in t:
        return 'bilibili'
    if re.search(r'BV[A-Za-z0-9]{10,}', url_or_text):
        return 'bilibili'
    return 'unknown'


def fetch_media_info(url_or_text: str) -> dict:
    """
    统一入口：自动识别平台并提取媒体信息
    返回格式与 fetch_media_from_douyin 一致
    """
    platform = detect_platform(url_or_text)
    if platform == 'douyin':
        return fetch_media_from_douyin(url_or_text)
    elif platform == 'bilibili':
        from bilibili import get_video_info
        info = get_video_info(url_or_text)
        return {
            'success': info['success'],
            'type': 'video' if info['success'] else None,
            'video_url': info.get('best_url', ''),
            'images': [info['cover_url']] if info.get('cover_url') else [],
            'title': info.get('title', ''),
            'author': info.get('author', ''),
            'qualities': info.get('qualities', []),
            'error': info.get('error'),
        }
    else:
        return {'success': False, 'type': None, 'video_url': None,
                'images': [], 'title': '', 'author': '',
                'error': '无法识别链接，目前支持: 抖音 / B站'}


def download_media(url_or_text: str, save_dir: str, quality_key: str = '') -> dict:
    """
    统一下载入口（仅供内部调用，app.py 会直接调用各平台的下载函数）
    """
    platform = detect_platform(url_or_text)
    if platform == 'bilibili':
        from bilibili import download_bilibili
        qn = 0
        if quality_key and quality_key.startswith('qn_'):
            qn = int(quality_key[3:])
        return download_bilibili(url_or_text, save_dir, quality=qn)
    return {'success': False, 'files': [], 'error': 'use app.py for douyin'}


# ===== 抖音逻辑 =====


def _create_driver():
    """创建浏览器实例（Linux 用 Chromium，Windows 用 Edge）"""
    browser = os.environ.get('BROWSER', 'auto').lower()
    headless = os.environ.get('HEADLESS', '1') == '1'

    # Linux / Docker 环境 → Chromium
    if browser == 'chromium' or (browser == 'auto' and os.name != 'nt'):
        opts = webdriver.ChromeOptions()
        if headless:
            opts.add_argument('--headless=new')
        opts.add_argument('--no-sandbox')
        opts.add_argument('--disable-gpu')
        opts.add_argument('--disable-dev-shm-usage')
        opts.add_argument('--disable-blink-features=AutomationControlled')
        opts.add_experimental_option('excludeSwitches', ['enable-automation'])
        bin_path = os.environ.get('CHROMIUM_BIN', '')
        if bin_path:
            opts.binary_location = bin_path
        return webdriver.Chrome(options=opts)

    # Windows 开发环境 → Edge
    from selenium.webdriver.edge.options import Options
    opts = Options()
    if headless:
        opts.add_argument('--headless=new')
    opts.add_argument('--disable-gpu')
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-blink-features=AutomationControlled')
    opts.add_experimental_option('excludeSwitches', ['enable-automation'])
    return webdriver.Edge(options=opts)


def extract_url_from_text(text: str) -> str | None:
    """从抖音分享文案中提取纯链接"""
    m = re.search(r'https?://(?:www\.)?v\.douyin\.com/[a-zA-Z0-9_-]+', text)
    if m:
        return m.group(0)
    m = re.search(r'https?://(?:www\.)?douyin\.com/(?:video|note)/\d+', text)
    if m:
        return m.group(0)
    return None


def extract_aweme_id(url_or_text: str) -> str | None:
    """从分享链接/文案中提取作品 ID"""
    raw = extract_url_from_text(url_or_text) or url_or_text.strip()
    m = re.search(r'/(?:video|note)/(\d+)', raw)
    if m:
        return m.group(1)
    try:
        resp = requests.get(raw, allow_redirects=True, timeout=15, headers={
            'User-Agent': 'Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36',
        })
        for pattern in [r'/(?:video|note)/(\d+)']:
            m = re.search(pattern, resp.url)
            if m:
                return m.group(1)
            m = re.search(pattern, resp.text)
            if m:
                return m.group(1)
    except Exception:
        return None
    return None


def _clean_url(url: str) -> str:
    """清理 URL：还原 HTML 编码、去水印"""
    url = url.replace('&amp;', '&')
    url = url.replace('playwm', 'play')
    return url


def _is_valid_post_image(url: str) -> bool:
    """判断是否是有意义的作品图片"""
    excludes = ['avatar', 'logo', 'icon', 'wallpaper', '100x100',
                'web-extension', 'emoji', 'badge', 'watermark']
    for ex in excludes:
        if ex in url.lower():
            return False
    return ('aweme-images' in url or 'tos-cn' in url)


def _extract_metadata(driver) -> dict:
    """从页面提取标题/作者"""
    result = {'title': '', 'author': ''}
    try:
        desc = driver.execute_script(
            'return document.querySelector(\'meta[name="description"]\')?.content || ""')
        if desc:
            # 格式: "标题 - 作者于YYYYMMDD发布在抖音..."
            parts = desc.split(' - ', 1)
            result['title'] = parts[0].replace('\n', ' ').strip()[:120]
            if len(parts) > 1:
                m = re.match(r'(.+?)于\d{8}', parts[1])
                if m:
                    result['author'] = m.group(1)
    except Exception:
        pass

    return result


def _get_browser_cookies(driver) -> dict:
    """从浏览器获取 cookie"""
    cookies = {}
    for c in driver.get_cookies():
        cookies[c['name']] = c['value']
    return cookies


def fetch_media_from_douyin(share_url: str) -> dict:
    """主入口：打开抖音页面，提取视频/图片

    返回:
    {
        'success': bool,
        'type': 'video' | 'image' | None,
        'video_url': str | None,
        'images': list[str],
        'title': str,
        'author': str,
        '_cookies': dict,  # 浏览器 cookie, 用于下载
        'error': str | None,
    }
    """
    aweme_id = extract_aweme_id(share_url)
    if not aweme_id:
        return {'success': False, 'error': '无法识别链接，请确认是抖音分享链接'}

    print('[core] 作品 ID:', aweme_id)
    driver = _create_driver()

    try:
        driver.get('https://www.douyin.com/')
        time.sleep(2)

        # 访问页面
        result = {'success': False, 'type': None, 'video_url': None,
                  'images': [], 'title': '', 'author': '', 'error': None}

        page_urls = [
            f'https://www.douyin.com/video/{aweme_id}',
            f'https://www.douyin.com/note/{aweme_id}',
        ]

        loaded = False
        for url in page_urls:
            try:
                driver.get(url)
                time.sleep(2)
                driver.execute_script('window.scrollTo(0, document.body.scrollHeight * 0.5)')
                time.sleep(4)
                loaded = True
                break
            except Exception:
                continue

        if not loaded:
            return {'success': False, 'error': '无法访问页面'}

        # 判断当前是否为图文（note）页面
        current_url = driver.current_url
        is_note = '/note/' in current_url
        print('[core] 页面类型: %s' % ('图文' if is_note else '视频'))

        # === 提取视频 URL（仅视频页面）===
        if not is_note:
            # 方法1: 通过 API 获取
            try:
                api_info = driver.execute_async_script("""
                    var awemeId = arguments[0];
                    var done = arguments[1];
                    fetch('/aweme/v1/web/aweme/detail/?aweme_id=' + awemeId, {
                        credentials: 'include',
                        headers: {'Accept': 'application/json', 'Referer': 'https://www.douyin.com/'}
                    }).then(function(r) {
                        if (!r.ok) return done(null);
                        return r.json().then(function(data) {
                            if (!data.aweme_detail || !data.aweme_detail.video) return done(null);
                            var v = data.aweme_detail.video;
                            for (var key of ['play_addr_h264', 'play_addr', 'play_addr_265']) {
                                if (v[key] && v[key].url_list && v[key].url_list[0]) return done(v[key].url_list[0]);
                            }
                            done(null);
                        });
                    }).catch(function() { done(null); });
                """, aweme_id)
                if api_info:
                    result['video_url'] = str(api_info)
                    result['type'] = 'video'
                    print('[core] 从 API 提取到视频')
            except Exception as e:
                print('[core] API 提取失败:', e)

            # 方法2: video.currentSrc
            if not result['video_url']:
                for _ in range(5):
                    try:
                        vsrc = driver.execute_script("""
                            var v = document.querySelector('video');
                            if (!v) return '';
                            var s = v.currentSrc || v.src || '';
                            return s.indexOf('blob:') === 0 ? '' : s;
                        """)
                        if vsrc and vsrc.startswith('http'):
                            result['video_url'] = _clean_url(vsrc)
                            result['type'] = 'video'
                            print('[core] 从 video 标签提取到视频')
                            break
                    except Exception:
                        pass
                    time.sleep(1)

            # 方法3: 源码搜索 douyinvod CDN
            if not result['video_url']:
                src = driver.page_source
                vod_urls = re.findall(r'https?://[^"\'<>\s]*douyinvod\.com[^"\'<>\s]*', src)
                for url in vod_urls:
                    if 'mime_type=video' in url or '.mp4' in url:
                        result['video_url'] = _clean_url(url)
                        result['type'] = 'video'
                        print('[core] 从 CDN 提取到视频')
                        break

        # === 提取图片 ===
        # 从 RENDER_DATA 中搜图片
        try:
            page_data = driver.execute_script("""
                var el = document.getElementById('RENDER_DATA');
                if (!el) return null;
                var raw = el.textContent.trim();
                if (raw.startsWith('%')) return JSON.parse(decodeURIComponent(raw));
                try { return JSON.parse(atob(raw)); } catch(e) { return JSON.parse(raw); }
            """)
            if page_data:
                s = json.dumps(page_data, ensure_ascii=False)
                imgs = re.findall(
                    r"https?://[^\"'<>\s]*?douyinpic\.com[^\"'<>\s]*?\.(?:jpg|jpeg|png|webp)[^\"'<>\s]*", s)
                seen = set()
                for url in imgs:
                    url = _clean_url(url)
                    if _is_valid_post_image(url) and url not in seen:
                        seen.add(url)
                        result['images'].append(url)
        except Exception:
            pass

        # 从页面源码补充
        if not result['images']:
            src = driver.page_source
            imgs = re.findall(
                r"https?://[^\"'<>\s]*?douyinpic\.com[^\"'<>\s]*?\.(?:jpg|jpeg|png|webp)[^\"'<>\s]*", src)
            seen = set()
            for url in imgs:
                url = _clean_url(url)
                if _is_valid_post_image(url) and url not in seen:
                    seen.add(url)
                    result['images'].append(url)

        # === 提取元信息 ===
        meta = _extract_metadata(driver)
        result['title'] = meta['title']
        result['author'] = meta['author']

        # 保存浏览器 cookie
        result['_cookies'] = _get_browser_cookies(driver)

        # === 判断结果 ===
        # 如果有视频，只保留视频（去掉无关的封面图）
        if result['video_url']:
            result['images'] = []
            result['type'] = 'video'
            result['success'] = True
            print('[core] 成功: 视频=%s' % ('有' if result['video_url'] else '无'))
        else:
            result['images'] = result['images'][:10]
            if result['images']:
                result['type'] = 'image'
                result['success'] = True
                print('[core] 成功: 图片=%d张' % len(result['images']))
            else:
                result['error'] = '未找到媒体内容'

        return result

    except Exception as e:
        return {'success': False, 'error': f'处理出错: {str(e)}'}
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def download_file(url: str, save_path: str, cookies: dict = None, timeout: int = 120) -> bool:
    """下载文件到本地（可携带浏览器 cookie）"""
    try:
        # 动态 Referer
        ul = url.lower()
        if 'bilibili' in ul or 'bilivideo' in ul or 'hdslb' in ul or 'mountaintoys' in ul:
            referer = 'https://www.bilibili.com/'
        elif 'xhscdn' in ul or 'xiaohongshu' in ul:
            referer = 'https://www.xiaohongshu.com/'
        else:
            referer = 'https://www.douyin.com/'
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36',
            'Referer': referer,
        }
        resp = requests.get(url, headers=headers, stream=True, timeout=timeout, cookies=cookies)
        if resp.status_code != 200:
            return False
        os.makedirs(os.path.dirname(save_path) or '.', exist_ok=True)
        with open(save_path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        return True
    except Exception:
        return False


def sanitize_filename(name: str) -> str:
    """清理文件名"""
    name = name.strip()
    name = re.sub(r'[\r\n\t]', ' ', name)  # 换行→空格
    name = re.sub(r'[\\/:*?"<>|#]', '_', name)
    name = ''.join(c for c in name if ord(c) < 0x10000)
    name = name.rstrip(' ._-')
    return name[:120]
