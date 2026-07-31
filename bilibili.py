"""
B站 (Bilibili) 视频提取
使用公开 API，无需浏览器，直接 HTTP 请求
"""

import re
import os
import requests
from urllib.parse import urlparse, parse_qs

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36',
    'Referer': 'https://www.bilibili.com/',
}

# 画质映射
QUALITY_MAP = {
    16: '360p 流畅',
    32: '480p 清晰',
    64: '720p 高清',
    80: '1080p 高清',
    120: '4K 超清',
    125: 'HDR',
}


def extract_bv(url_or_text: str) -> str | None:
    """从链接或文案中提取 BV 号"""
    # 直接匹配 BV 号
    m = re.search(r'BV[A-Za-z0-9]{10,12}', url_or_text)
    if m:
        return m.group(0)
    # av 号
    m = re.search(r'av(\d+)', url_or_text, re.IGNORECASE)
    if m:
        return m.group(0)
    # 标准视频链接
    m = re.search(r'video/(BV[A-Za-z0-9]+)', url_or_text)
    if m:
        return m.group(1)
    # b23.tv 短链接 → 需要解析重定向
    m = re.search(r'b23\.tv[/:]([A-Za-z0-9]+)', url_or_text)
    if m:
        short = m.group(0)
        if not short.startswith('http'):
            short = 'https://' + short
        try:
            resp = requests.get(short, allow_redirects=True, timeout=10, headers=HEADERS)
            bv = re.search(r'video/(BV[A-Za-z0-9]+)', resp.url)
            if bv:
                return bv.group(1)
        except Exception:
            pass
    return None


def get_avid(bv_or_url: str) -> tuple[str, int]:
    """将 BV 号或链接转为 (bvid, aid)"""
    bv = extract_bv(bv_or_url)
    if not bv:
        return None, 0
    # BV 开头才调用 API
    if bv.startswith('BV'):
        r = requests.get('https://api.bilibili.com/x/web-interface/view?bvid=' + bv, headers=HEADERS, timeout=10)
        d = r.json().get('data', {})
        return bv, d.get('aid', 0)
    # av 号
    aid = int(bv[2:])
    return f'av{aid}', aid


def get_video_info(bv_or_url: str) -> dict:
    """
    获取 B 站视频信息
    返回: {
        'success': bool,
        'title': str,
        'author': str,
        'duration': int,
        'cover_url': str | None,
        'qualities': list[dict],  # 可用的画质选项
        'best_url': str | None,   # 最高质量的合并流 URL
        'has_dash': bool,         # 是否有 DASH 流（需 FFmpeg 合并）
        'error': str | None,
    }
    """
    result = {'success': False, 'title': '', 'author': '', 'duration': 0,
              'cover_url': None, 'qualities': [], 'best_url': None,
              'has_dash': False, 'error': None}

    bv = extract_bv(bv_or_url)
    if not bv:
        result['error'] = '无法识别 B 站链接，请确认是 bilibili.com 的视频链接'
        return result

    # 获取视频基本信息
    try:
        r = requests.get('https://api.bilibili.com/x/web-interface/view?bvid=' + bv, headers=HEADERS, timeout=10)
        if r.status_code != 200:
            result['error'] = 'API 请求失败'
            return result
        data = r.json()
        if data.get('code') != 0:
            result['error'] = data.get('message', '视频不存在')
            return result
    except Exception as e:
        result['error'] = f'请求失败: {str(e)}'
        return result

    d = data.get('data', {})
    result['title'] = d.get('title', '')
    result['author'] = d.get('owner', {}).get('name', '')
    result['duration'] = d.get('duration', 0)
    result['cover_url'] = d.get('pic', '')

    cid = d.get('cid', 0)
    aid = d.get('aid', 0)

    if not cid:
        result['error'] = '无法获取视频 cid'
        return result

    # 获取视频流信息 - 从高到低尝试各画质的合并流
    for qn in [120, 80, 64, 32, 16]:
        try:
            url = (
                f'https://api.bilibili.com/x/player/playurl'
                f'?bvid={bv}&cid={cid}&qn={qn}&platform=web&fnver=0&fnval=0'
            )
            r = requests.get(url, headers=HEADERS, timeout=10)
            if r.status_code != 200:
                continue
            data = r.json().get('data', {})
            durls = data.get('durl', [])

            if durls:
                label = QUALITY_MAP.get(qn, f'{qn}p')
                result['best_url'] = durls[0]['url']
                result['qualities'].append({
                    'qn': qn,
                    'label': label,
                    'url': durls[0]['url'],
                    'size': durls[0].get('size', 0),
                })
        except Exception:
            continue

    # 检查是否有 DASH 流（更高画质但需合并）
    try:
        url = (
            f'https://api.bilibili.com/x/player/playurl'
            f'?bvid={bv}&cid={cid}&qn=80&platform=web&fnver=0&fnval=4048'
        )
        r = requests.get(url, headers=HEADERS, timeout=10)
        dash_data = r.json().get('data', {}).get('dash', {})
        if dash_data.get('video'):
            result['has_dash'] = True
            # 列出 DASH 中可用的更高画质
            existing_qns = {q['qn'] for q in result['qualities']}
            for v in dash_data['video']:
                qn = int(v.get('id', 0))
                if qn not in existing_qns and qn in QUALITY_MAP:
                    # 记录 DASH 方式可达的画质
                    result['qualities'].append({
                        'qn': qn,
                        'label': QUALITY_MAP[qn] + ' (DASH)',
                        'url': v.get('base_url', '') or (v.get('baseUrl', '')),
                        'size': 0,
                        'dash': True,
                    })
                    existing_qns.add(qn)
    except Exception:
        pass

    if result['best_url']:
        result['success'] = True
        print('[bilibili] 成功: %s - %s' % (result['author'], result['title'][:40]))
    elif result['qualities']:
        # 只有 DASH 流
        result['best_url'] = result['qualities'][0].get('url', '')
        result['success'] = True
    else:
        result['error'] = '无法获取视频下载地址'

    return result


def download_bilibili(bv_or_url: str, save_dir: str, quality: int = 0) -> dict:
    """
    下载 B 站视频
    返回: {'success': bool, 'files': list, 'error': str | None}
    """
    info = get_video_info(bv_or_url)
    if not info.get('success'):
        return {'success': False, 'files': [], 'error': info.get('error', '解析失败')}

    from core import sanitize_filename, download_file

    base = sanitize_filename(f"{info['author']}_{info['title'][:40]}") or f'bilibili_{hash(bv_or_url) % 100000}'
    files = []

    # 选择指定画质
    target_url = None
    target_label = ''
    if quality > 0 and info.get('qualities'):
        for q in info['qualities']:
            if q.get('qn') == quality and not q.get('dash'):
                target_url = q['url']
                target_label = '_' + q['label'].split()[0]
                break

    if not target_url:
        target_url = info['best_url']

    if target_url:
        path = os.path.join(save_dir, base + target_label + '.mp4')
        if download_file(target_url, path):
            size = os.path.getsize(path)
            files.append({
                'id': os.path.basename(path),
                'name': os.path.basename(path),
                'type': 'video',
                'size': f'{size/1024/1024:.1f} MB' if size > 1024*1024 else f'{size/1024:.0f} KB',
            })

    if info.get('has_dash') and not files:
        # 有 DASH 但没下载成功，尝试用 best_url
        if info.get('best_url'):
            path = os.path.join(save_dir, base + '_无音频.mp4')
            if download_file(info['best_url'], path):
                size = os.path.getsize(path)
                files.append({
                    'id': os.path.basename(path),
                    'name': os.path.basename(path),
                    'type': 'video',
                    'size': f'{size/1024/1024:.1f} MB' if size > 1024*1024 else f'{size/1024:.0f} KB',
                })
        if not files:
            return {'success': False, 'files': [], 'error': '该视频仅支持DASH流（需FFmpeg合并音频），后续版本支持'}

    if not files:
        return {'success': False, 'files': [], 'error': '下载失败'}

    return {'success': True, 'files': files}
