"""Render 启动入口：先固定运行设置，再以 uvicorn 启动服务。

Docker Command 填 `python render_boot.py` 即可（该字段不做 shell 解析，
所以不能写管道/&&/分号）。
"""
import os
import runpy
import sys

WORK_DIR = '/app' if os.path.isdir('/app') else os.path.dirname(os.path.abspath(__file__))

# 让 config / auth / routes 等顶层模块可被导入
if WORK_DIR not in sys.path:
    sys.path.insert(0, WORK_DIR)


def patch_config_defaults() -> None:
    """只改 DEFAULT_SETTINGS 里这 6 个值，其它字符原样保留。"""
    p = os.path.join(WORK_DIR, 'config.py')
    if not os.path.exists(p):
        print('WARN 找不到 config.py:', p)
        return
    src = open(p, encoding='utf-8').read()
    pairs = [
        ('"native_thinking_mode": "request"', '"native_thinking_mode": "off"'),
        ('"express_location": "global"', '"express_location": ""'),
        ('"image_size": "4K"', '"image_size": "2K"'),
        ('"image_aspect_ratio": ""', '"image_aspect_ratio": ""'),
        ('"inject_system_instruction": false', '"inject_system_instruction": true'),
        ('"inject_prefill_for_image": false', '"inject_prefill_for_image": false'),
    ]
    for old, new in pairs:
        if old in src:
            src = src.replace(old, new, 1)
            print('PATCH', old, '->', new)
        else:
            print('SKIP (未命中)', old)
    open(p, 'w', encoding='utf-8').write(src)
    for line in src.splitlines():
        if ('native_thinking_mode' in line or 'express_location' in line
                or 'image_size' in line or 'image_aspect_ratio' in line
                or 'inject_system_instruction' in line):
            print('CHECK |', line.strip())


def patch_state_file() -> None:
    """状态文件若已存在，同步覆盖这几项，避免旧值盖过新默认值。"""
    d = os.environ.get('STATE_DIR', WORK_DIR) or WORK_DIR
    p = os.path.join(d, 'web_state.json')
    if not os.path.exists(p):
        print('INFO 无 web_state.json，使用代码默认值：', p)
        return
    try:
        import json
        s = json.load(open(p, encoding='utf-8'))
        st = s.setdefault('settings', {})
        st['native_thinking_mode'] = 'off'
        st['express_location'] = ''
        st['image_size'] = '2K'
        st['image_aspect_ratio'] = ''
        st['inject_system_instruction'] = True
        st['inject_prefill_for_image'] = False
        json.dump(s, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
        print('SEED OK', p, '| thinking=', st['native_thinking_mode'],
              '| location=', repr(st['express_location']), '| size=', st['image_size'])
    except Exception as e:
        print('WARN 写状态文件失败:', e)


print('=== render_boot: 固定运行设置 ===')
patch_config_defaults()
patch_state_file()
print('=== render_boot: 启动 uvicorn ===')

port = os.environ.get('PORT') or '7860'
args = ['uvicorn', 'main:app', '--host', '0.0.0.0', '--port', str(port)]
print(' '.join(args))
os.chdir(WORK_DIR)
os.execvp(args[0], args)
