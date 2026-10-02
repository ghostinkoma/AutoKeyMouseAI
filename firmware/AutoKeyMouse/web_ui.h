#pragma once
#include <pgmspace.h>

// GET / で返す単一ページ UI (HTML/CSS/JS 埋め込み)
static const char kIndexHtml[] PROGMEM = R"HTML(<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AutoKeyMouse</title>
<style>
:root{--bg:#f5f6f8;--card:#fff;--fg:#1d2330;--mute:#6b7280;--line:#e3e6eb;--ok:#15803d;--ng:#b91c1c;--acc:#2563eb}
@media (prefers-color-scheme:dark){:root{--bg:#111418;--card:#1b1f26;--fg:#e6e8eb;--mute:#9aa3ae;--line:#2c323b;--ok:#4ade80;--ng:#f87171;--acc:#60a5fa}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:860px;margin:0 auto;padding:16px}h1{font-size:20px;margin:4px 0 12px}h2{font-size:15px;margin:0 0 10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:14px}
.st{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px}
.st div{font-size:13px;color:var(--mute)}.st b{display:block;color:var(--fg);font-size:15px;word-break:break-all}
.ok{color:var(--ok)!important}.ng{color:var(--ng)!important}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
button{font:inherit;padding:10px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--fg);cursor:pointer}
button:hover{border-color:var(--acc)}button.p{background:var(--acc);border-color:var(--acc);color:#fff}button.s{background:var(--ng);border-color:var(--ng);color:#fff}
input,select,textarea{font:inherit;width:100%;padding:8px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--fg)}
textarea{min-height:120px;font-family:ui-monospace,monospace;font-size:13px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:8px 0}.row>*{flex:1 1 140px}
#msg{font-family:ui-monospace,monospace;font-size:13px;white-space:pre-wrap;color:var(--mute)}
details summary{cursor:pointer;color:var(--mute)}code{font-size:12px}
</style></head><body><main>
<h1>AutoKeyMouse</h1>
<div class="card st">
 <div>BLE<b id="ble">-</b></div><div>Wi-Fi (STA)<b id="wifi">-</b></div>
 <div>実行中<b id="cur">-</b></div><div>前回の結果<b id="last">-</b></div>
</div>
<div class="card"><h2>マクロ</h2>
 <div class="row"><label><input type="checkbox" id="loop" style="width:auto"> 停止するまで繰り返す</label>
 <button class="s" onclick="api('/stop')">停止</button></div>
 <div class="grid" id="btns"></div>
 <div id="msg"></div>
</div>
<div class="card"><h2>マクロ編集</h2>
 <div class="row"><select id="slot" onchange="pick()"></select><input id="name" placeholder="名前"></div>
 <textarea id="script" spellcheck="false"></textarea>
 <div class="row"><button class="p" onclick="save()">保存</button><button onclick="runNow()">この内容をすぐ実行</button></div>
 <details><summary>スクリプト書式</summary><p><code>k:a</code> キー / <code>k:ctrl+c</code> 同時押し / <code>kd:</code> <code>ku:</code> 押下・解放 /
 <code>t:文字列</code> 入力 / <code>m:dx,dy</code> 相対移動 / <code>a:x,y</code> 絶対移動(0-32767) /
 <code>c:L</code> <code>c:R,2</code> クリック / <code>bd:L</code> <code>bu:L</code> / <code>s:-3</code> ホイール /
 <code>w:500</code> 待機ms / <code>d:20</code> イベント間隔 / <code>ra</code> 全解放 / <code>#</code> コメント。
 区切りは <code>;</code> か改行 (文字列中の ; は <code>\;</code>)。</p></details>
</div>
<div class="card"><h2>設定</h2>
 <div class="row"><label>イベント間隔 (5-50ms)<input id="delay" type="number" min="5" max="50"></label>
 <label>ターゲットのキー配列<select id="layout"><option value="jp">日本語 (109)</option><option value="us">英語 (104)</option></select></label>
 <button onclick="setts()">適用</button></div>
 <div class="row"><input id="ssid" placeholder="Wi-Fi SSID"><input id="pass" type="password" placeholder="パスワード">
 <button onclick="wifi()">Wi-Fi 保存</button></div>
</div>
</main><script>
let M=[];const $=id=>document.getElementById(id);
function msg(t){$('msg').textContent=t}
async function api(u,body){try{const r=await fetch(u,body?{method:'POST',body:new URLSearchParams(body)}:{});const t=await r.text();msg(r.status+' '+t);return r.ok}catch(e){msg('通信エラー: '+e)}}
async function load(){M=await (await fetch('/macros')).json();
 $('btns').innerHTML='';$('slot').innerHTML='';
 for(const m of M){const b=document.createElement('button');b.textContent=m.id+'. '+m.name;
  b.onclick=()=>api('/macro?id='+m.id+($('loop').checked?'&repeat=0':''));$('btns').appendChild(b);
  const o=document.createElement('option');o.value=m.id;o.textContent=m.id+'. '+m.name;$('slot').appendChild(o)}
 pick()}
function pick(){const m=M.find(x=>x.id==$('slot').value);if(m){$('name').value=m.name;$('script').value=m.script}}
async function save(){const id=$('slot').value;if(await api('/save',{id,name:$('name').value,s:$('script').value})){await load();$('slot').value=id;pick()}}
function runNow(){api('/run',{s:$('script').value})}
function setts(){api('/settings?delay='+$('delay').value+'&layout='+$('layout').value)}
function wifi(){api('/wifi',{ssid:$('ssid').value,pass:$('pass').value})}
let first=true;
async function poll(){try{const s=await (await fetch('/status')).json();
 $('ble').textContent=s.ble?'接続済み':'未接続';$('ble').className=s.ble?'ok':'ng';
 $('wifi').textContent=s.wifi?s.ssid+' / '+s.ip:'未接続 (AP '+s.apIp+')';$('wifi').className=s.wifi?'ok':'ng';
 $('cur').textContent=s.busy?s.current:'待機中';$('last').textContent=s.last;
 if(first){$('delay').value=s.delay;$('layout').value=s.layout;$('ssid').value=s.ssid;first=false}
}catch(e){$('ble').textContent='?'}}
load();poll();setInterval(poll,1000);
</script></body></html>)HTML";
