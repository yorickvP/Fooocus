# based on https://github.com/AUTOMATIC1111/stable-diffusion-webui/blob/v1.6.0/modules/ui_gradio_extensions.py

import gradio as gr
import args_manager
from pathlib import Path

from modules.localization import localization_js


GradioTemplateResponseOriginal = gr.routes.templates.TemplateResponse

script_path = Path(__file__).resolve().parent.parent
js_path = script_path / "javascript"


def webpath(fn):
    if fn.is_relative_to(script_path):
        web_path = str(fn.relative_to(script_path)).replace('\\', '/')
    else:
        web_path = str(fn.absolute())

    return f'file={web_path}?{fn.stat().st_mtime}'


def javascript_html():
    script_js_path = webpath(js_path / 'script.js')
    context_menus_js_path = webpath(js_path / 'contextMenus.js')
    localization_js_path = webpath(js_path / 'localization.js')
    zoom_js_path = webpath(js_path / 'zoom.js')
    edit_attention_js_path = webpath(js_path / 'edit-attention.js')
    viewer_js_path = webpath(js_path / 'viewer.js')
    image_viewer_js_path = webpath(js_path / 'imageviewer.js')
    head = f'<script type="text/javascript">{localization_js(args_manager.args.language)}</script>\n'
    head += f'<script type="text/javascript" src="{script_js_path}"></script>\n'
    head += f'<script type="text/javascript" src="{context_menus_js_path}"></script>\n'
    head += f'<script type="text/javascript" src="{localization_js_path}"></script>\n'
    head += f'<script type="text/javascript" src="{zoom_js_path}"></script>\n'
    head += f'<script type="text/javascript" src="{edit_attention_js_path}"></script>\n'
    head += f'<script type="text/javascript" src="{viewer_js_path}"></script>\n'
    head += f'<script type="text/javascript" src="{image_viewer_js_path}"></script>\n'
    return head


def css_html():
    style_css_path = webpath(script_path / 'css' / 'style.css')
    head = f'<link rel="stylesheet" property="stylesheet" href="{style_css_path}">'
    return head


def reload_javascript():
    js = javascript_html()
    css = css_html()

    def template_response(*args, **kwargs):
        res = GradioTemplateResponseOriginal(*args, **kwargs)
        res.body = res.body.replace(b'</head>', f'{js}</head>'.encode("utf8"))
        res.body = res.body.replace(b'</body>', f'{css}</body>'.encode("utf8"))
        res.init_headers()
        return res

    gr.routes.templates.TemplateResponse = template_response
