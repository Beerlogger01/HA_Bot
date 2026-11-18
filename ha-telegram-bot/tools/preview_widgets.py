#!/usr/bin/env python3
"""Generate a standalone, offline preview using the exact shipped widget HTML.

The bridge is deliberately outside the production artifact. This is a browser
layout/mock check, not an implementation of Telegram's QuickJS sandbox.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
from widget_transport import validate_html

html = (ROOT / "app/widgets/home.html").read_text()
raw, compressed = validate_html(html)
parser = argparse.ArgumentParser()
parser.add_argument("--output", type=Path, default=Path("/tmp/ha-widget-preview.html"))
args = parser.parse_args()
bridge = '''<script>
var listeners={}, state={r:1,u:1,t:'Мой дом',b:'Активно: 4 · Сущностей: 28\\nСвет: 2 из 8 включено\\nГостиная: 22.4 °C\\nВлажность: 46%',a:[['Свет','lights'],['Климат','climate'],['Комнаты','rooms'],['Избранное','favorites']],n:''};
window.TelegramWidget={viewer:{id:1},chatData:JSON.stringify(state),publicData:null,colorScheme:'light',profile:1,
onEvent:function(n,f){listeners[n]=f},watchPublicData:function(){},ready:function(){},
request:function(action){return new Promise(function(resolve,reject){setTimeout(function(){
if(window.mockError){reject({code:window.mockError});return}
if(action==='lights'){state.t='Свет';state.b='Кухня · Выключено';state.a=[['Включить','on'],['Ярче +','on'],['Назад','home']]}
else if(action==='on'){state.b='Кухня · Включено\\nЯркость: 80%';state.a=[['Выключить','off'],['Назад','home']]}
else if(action==='off'){state.b='Кухня · Выключено';state.a=[['Включить','on'],['Назад','home']]}
else if(action==='climate'){state.t='Спальня';state.b='Климат · Нагрев\\nСейчас: 21.5 °C\\nЦель: 22 °C';state.a=[['− 0.5°','climate'],['+ 0.5°','climate'],['Дом','home']]}
else{state.t='Мой дом';state.b='Активно: 4 · Свет: 2 из 8';state.a=[['Свет','lights'],['Климат','climate'],['Комнаты','rooms'],['Избранное','favorites']]}
state.r++;TelegramWidget.chatData=JSON.stringify(state);listeners.chatDataChanged();resolve({result:JSON.stringify({message:'Готово'})})},250)})}};
</script>'''
content = html.replace('<head>', '<head>' + bridge)
# Embed as JSON, escaping script termination, so user controlled HA text cannot
# break out of the preview harness (production always uses textContent).
source = json.dumps(content, ensure_ascii=False).replace('</', '<\\/')
page = '''<!doctype html><html lang="ru"><meta charset="utf-8"><title>Home · Widget preview</title>
<style>body{margin:0;padding:32px;background:#edf1f3;font:15px system-ui;color:#283c42}main{max-width:1000px;margin:auto}h1{font-size:32px;letter-spacing:-1px;margin-bottom:8px}p{color:#65777c;max-width:640px;line-height:1.5}nav{display:flex;gap:8px;flex-wrap:wrap;margin:24px 0}button{background:white;border:1px solid #d2dcdf;border-radius:8px;padding:9px 14px;color:#283c42;cursor:pointer}.chat{padding:28px;background:#dbe6e2;border-radius:20px;max-width:640px}iframe{display:block;border:0;border-radius:18px;box-shadow:0 4px 18px #26362d12;width:360px;height:530px;max-width:100%;background:white}small{display:block;margin-top:18px;color:#65777c}</style>
<main><h1>Дом, всегда под рукой</h1><p>Локальный preview HTML Widget. Все устройства и запросы здесь вымышленные. Переключите тему и попробуйте управление светом.</p><nav><button onclick="theme(false)">Светлая</button><button onclick="theme(true)">Тёмная</button><button onclick="size(200)">200 px</button><button onclick="size(320)">320 px</button><button onclick="size(430)">430 px</button><button onclick="size(600)">600 px</button><button onclick="error('RATE_LIMITED')">Ошибка лимита</button><button onclick="error('')">Без ошибок</button><button onclick="viewer()">Другой пользователь</button></nav><div class="chat"><iframe id="widget" title="Дом"></iframe></div><small>Тот же HTML, что в add-on. Browser preview не заменяет проверку Profile 1 в клиенте Telegram.</small></main>
<script>var frame=document.getElementById('widget');frame.srcdoc=SOURCE;
function theme(dark){var w=frame.contentWindow;var p=dark?['#202a31','#f0f4f6','#899ca6','#2b3943','#70d0c0','#102c26']:['#ffffff','#243b40','#71868b','#f0f5f4','#2d8d7d','#ffffff'];['bg-color','text-color','hint-color','secondary-bg-color','button-color','button-text-color'].forEach(function(k,i){w.document.body.style.setProperty('--tg-theme-'+k,p[i])});w.document.body.style.background=p[0];w.TelegramWidget.colorScheme=dark?'dark':'light';w.listeners.themeChanged()}
function size(n){frame.style.width=n+'px'}function error(code){frame.contentWindow.mockError=code}function viewer(){var w=frame.contentWindow;w.TelegramWidget.viewer.id=w.TelegramWidget.viewer.id===1?2:1;w.listeners.chatDataChanged()}
frame.onload=function(){theme(false);new frame.contentWindow.ResizeObserver(function(){frame.style.height=frame.contentWindow.document.body.getBoundingClientRect().height+'px'}).observe(frame.contentWindow.document.body)};</script></html>'''.replace('SOURCE', source)
args.output.write_text(page)
print(f"HTML {raw}/32768 bytes; zlib-9 {compressed}/4096 bytes")
print(args.output)
