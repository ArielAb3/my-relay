'use strict';
const byId = id => document.getElementById(id);
const screenCanvas = byId('screen');
const ctx = screenCanvas.getContext('2d', {alpha:false});
const statusLine = byId('status');
let visitor = null, pointer = null, last = null, motion = 0;
const say = text => {statusLine.textContent = text;};


const savedKey = 'remote-desktop.named-computers.v1';
function readNames() {
    try {
        const value = JSON.parse(localStorage.getItem(savedKey) || '{}');
        if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
        return Object.fromEntries(Object.entries(value).filter(([code, name]) =>
            /^[0-9]{6}$/.test(code) && typeof name === 'string' && name.trim() && name.length <= 40));
    } catch { return {}; }
}

function refreshNames() {
    const names = readNames(), code = byId('code').value;
    const list = byId('saved');
    list.replaceChildren(new Option('בחירת מחשב', ''));
    for (const [id, name] of Object.entries(names).sort((a,b)=>a[1].localeCompare(b[1], 'he'))) {
        list.add(new Option(name + ' · ' + id, id));
    }
    list.value = Object.hasOwn(names, code) ? code : '';
    byId('savedList').hidden = list.length <= 1;
    byId('alias').value = names[code] || '';
    byId('forgetAlias').hidden = !Object.hasOwn(names, code);
    byId('aliasStatus').textContent = '';
}
function editName(remove) {
    const names = readNames(), code = byId('code').value, name = byId('alias').value.trim();
    if (!/^[0-9]{6}$/.test(code) || (!remove && (!name || name.length > 40 || /[\p{C}]/u.test(name)))) {
        byId('aliasStatus').textContent = 'הזן קוד מחשב בן שש ספרות ושם באורך 1–40 תווים.';
        return;
    }
    if (remove) delete names[code]; else names[code] = name;
    try { localStorage.setItem(savedKey, JSON.stringify(names)); }
    catch { byId('aliasStatus').textContent = 'הדפדפן לא הצליח לשמור במכשיר הזה.'; return; }
    refreshNames();
    byId('aliasStatus').textContent = remove ? 'המחשב הוסר מהרשימה.' : 'השם והקוד נשמרו במכשיר הזה.';
}
byId('saved').addEventListener('change', () => {
    byId('code').value = byId('saved').value;
    refreshNames();
    byId('pass').focus();
});
byId('code').addEventListener('input', refreshNames);
byId('saveAlias').onclick = () => editName(false);
byId('forgetAlias').onclick = () => editName(true);
window.addEventListener('storage', event => { if (!event.key || event.key === savedKey) refreshNames(); });
refreshNames();

function endVisit(message) {
    const old = visitor;
    visitor = null;
    if (old) { old.abort.abort(); clearTimeout(old.timer); if (old.ws) old.ws.close(); }
    cancelAnimationFrame(motion); motion = 0; pointer = null; last = null;
    screenCanvas.width = screenCanvas.height = 1;
    byId('pass').value = byId('entry').value = '';
    byId('login').hidden = false; byId('join').disabled = false;
    byId('viewer').hidden = byId('typing').hidden = byId('end').hidden = true;
    byId('keyboard').setAttribute('aria-expanded','false');
    say(message);
}

function jpegSize(bytes) {
    if (bytes[0] !== 255 || bytes[1] !== 216) throw Error();
    let pos = 2;
    while (pos + 4 <= bytes.length && bytes[pos++] === 255) {
        while (bytes[pos] === 255) pos++;
        const marker = bytes[pos++];
        if (marker === 218 || marker === 217) break;
        const length = bytes[pos] * 256 + bytes[pos+1];
        if (length < 2 || pos + length > bytes.length) break;
        if ([192,193,194].includes(marker) && length >= 8) {
            return [bytes[pos+5]*256+bytes[pos+6], bytes[pos+3]*256+bytes[pos+4]];
        }
        pos += length;
    }
    throw Error();
}

function validSize(w,h) { return w>0 && h>0 && w<=8192 && h<=8192 && w*h<=16777216; }

async function paint(buffer, visit) {
    if (visitor !== visit) return;
    const data = new DataView(buffer);
    if (buffer.byteLength < 5 || buffer.byteLength > 12*1024*1024 || data.getUint32(1) !== buffer.byteLength-5) throw Error();
    const kind = data.getUint8(0);
    let width, height, tiles = [], pixels = 0;
    function tile(pos,length,x,y) {
        if (length<=0 || pos+length>buffer.byteLength) throw Error();
        const bytes = new Uint8Array(buffer,pos,length);
        const [w,h] = jpegSize(bytes);
        pixels += w*h;
        if (!validSize(w,h) || x+w>width || y+h>height || pixels>width*height) throw Error();
        tiles.push({bytes,x,y,w,h});
    }
    if (kind === 1) {
        [width,height] = jpegSize(new Uint8Array(buffer,5));
        tile(5,buffer.byteLength-5,0,0);
    } else if (kind === 8 && buffer.byteLength >= 15) {
        width=data.getUint16(5); height=data.getUint16(7);
        const tw=data.getUint16(9), th=data.getUint16(11), n=data.getUint16(13);
        if (!validSize(width,height) || !tw || tw>width || !th || th>height || !n || n>1024) throw Error();
        const seen = new Set();
        let pos=15;
        for(let i=0;i<n;i++) {
            if(pos+6>buffer.byteLength) throw Error();
            const col=data.getUint8(pos), row=data.getUint8(pos+1), length=data.getUint32(pos+2);
            const index=256*row+col;
            if(seen.has(index)) throw Error();
            seen.add(index); pos+=6;
            tile(pos,length,col*tw,row*th); pos+=length;
        }
        if(pos!==buffer.byteLength) throw Error();
    } else throw Error();
    if(screenCanvas.width!==width || screenCanvas.height!==height) {
        screenCanvas.width=width; screenCanvas.height=height;
    }
    for(const t of tiles) {
        if(visitor!==visit) return;
        const blob = new Blob([t.bytes], {type:'image/jpeg'});
        let bitmap, url;
        try {
            if(typeof createImageBitmap==='function') bitmap=await createImageBitmap(blob);
            else {
                bitmap=new Image(); url=URL.createObjectURL(blob);
                await new Promise((ok,fail)=>{bitmap.onload=ok; bitmap.onerror=fail; bitmap.src=url;});
            }
            if(bitmap.width!==t.w || bitmap.height!==t.h) throw Error();
            if(visitor===visit) ctx.drawImage(bitmap,t.x,t.y);
        } finally { if(bitmap?.close) bitmap.close(); if(url) URL.revokeObjectURL(url); }
    }
    if(!visit.painted && visitor===visit) {
        visit.painted=true; say('המסך מחובר. הקשה ללחיצה, גרירה להזזה.');
    }
}

byId('credentials').addEventListener('submit',async e=>{
    e.preventDefault(); if(visitor) return;
    const visit={abort:new AbortController(), chain:Promise.resolve(), frames:0, bytes:0, active:false};
    visitor=visit;
    byId('join').disabled=true; byId('end').hidden=false; say('מתחבר לשרת…');
    visit.timer=setTimeout(()=>{if(visitor===visit) endVisit('זמן החיבור הסתיים. אפשר לנסות שוב.');},60000);
    const body=JSON.stringify({code:byId('code').value,password:byId('pass').value});
    byId('pass').value='';
    try {
        const r=await fetch('./login',{method:'POST',credentials:'same-origin',cache:'no-store',redirect:'error',
            headers:{'Content-Type':'application/json','X-Requested-With':'RemoteDesktop'},body,signal:visit.abort.signal});
        if(visitor!==visit) return;
        if(!r.ok) return endVisit(({401:'בדקו את הקוד והסיסמה וודאו שהמחשב זמין.',409:'נדרש עדכון Host במחשב כדי לאשר חיבור מהטלפון.',
            429:'היו כמה ניסיונות. נסו שוב בעוד דקה.'})[r.status] || 'השרת אינו זמין כעת.');
        const url=new URL('./ws',location.href); url.protocol=location.protocol==='https:'?'wss:':'ws:';
        const ws=new WebSocket(url); visit.ws=ws; ws.binaryType='arraybuffer';
        ws.onmessage=event=>{
            if(visitor!==visit) return;
            if(typeof event.data==='string') {
                try {
                    const m=JSON.parse(event.data);
                    if(m.type==='waiting') {byId('login').hidden=true; say('ממתין לאישור במחשב המשתף…');}
                    else if(m.type==='connected' && m.protocol===2 && m.allow_files===false) {
                        clearTimeout(visit.timer); visit.active=true; byId('viewer').hidden=false; say('החיבור אושר. ממתין למסך…');
                    } else endVisit(m.type==='denied'?'הבקשה נדחתה במחשב.':m.type==='expired'?'לא התקבל אישור בזמן.':'השיתוף הסתיים.');
                } catch {endVisit('התקבלה תשובה לא תקינה מהשרת.');}
                return;
            }
            const buffer=event.data;
            if(!visit.active || !(buffer instanceof ArrayBuffer) || ++visit.frames>12 ||
                    (visit.bytes+=buffer.byteLength)>24*1024*1024) return endVisit('לא ניתן להציג את המסך בקצב הנוכחי. נסו להתחבר שוב.');
            visit.chain=visit.chain.then(()=>paint(buffer,visit)).catch(()=>{
                if(visitor===visit) endVisit('לא ניתן לפענח את תמונת המסך.');
            }).finally(()=>{visit.frames--; visit.bytes-=buffer.byteLength;});
        };
        ws.onclose=ws.onerror=()=>{if(visitor===visit) endVisit('החיבור הסתיים. אפשר להתחבר מחדש.');};
    } catch {if(visitor===visit) endVisit('לא ניתן להגיע לשרת. בדקו את החיבור ונסו שוב.');}
});

function send(kind,payload) {
    const ws=visitor?.ws;
    if(!visitor?.painted || ws?.readyState!==WebSocket.OPEN) return;
    if(ws.bufferedAmount>65536) return endVisit('החיבור איטי מדי. השיתוף הסתיים.');
    const bytes=new Uint8Array(5+payload.length);
    bytes[0]=kind; new DataView(bytes.buffer).setUint32(1,payload.length);
    bytes.set(payload,5); ws.send(bytes);
}
function mouse(action,pt,button=0) {
    const point=pt || last || {x:screenCanvas.width>>1,y:screenCanvas.height>>1};
    const payload=new Uint8Array(10), view=new DataView(payload.buffer);
    payload[0]=action; view.setInt32(1,point.x); view.setInt32(5,point.y); view.setInt8(9,button);
    send(2,payload);
}
function point(e) {
    const r=screenCanvas.getBoundingClientRect();
    return {x:Math.max(0,Math.min(screenCanvas.width-1,Math.floor((e.clientX-r.left)*screenCanvas.width/r.width))),
            y:Math.max(0,Math.min(screenCanvas.height-1,Math.floor((e.clientY-r.top)*screenCanvas.height/r.height)))};
}
screenCanvas.onpointerdown=e=>{
    if(pointer!==null || !visitor?.painted) return;
    e.preventDefault();pointer=e.pointerId;screenCanvas.setPointerCapture(pointer);last=point(e);mouse(1,last);
};
screenCanvas.onpointermove=e=>{
    if(pointer!==e.pointerId) return;
    last=point(e);
    if(!motion) motion=requestAnimationFrame(()=>{motion=0;if(pointer!==null)mouse(0,last);});
};
function release(e) {
    if(pointer!==e.pointerId) return;
    cancelAnimationFrame(motion);motion=0;
    if(e.type==='pointerup') last=point(e);
    pointer=null;mouse(2,last);
}
screenCanvas.onpointerup=screenCanvas.onpointercancel=screenCanvas.onlostpointercapture=release;
screenCanvas.oncontextmenu=e=>e.preventDefault();
byId('up').onclick=()=>mouse(5,last,3);byId('down').onclick=()=>mouse(5,last,-3);
byId('right').onclick=()=>mouse(3,last,2);
function key(action,text) {
    const chars=new TextEncoder().encode(text), payload=new Uint8Array(1+chars.length);
    payload[0]=action;payload.set(chars,1);send(3,payload);
}
byId('keyboard').onclick=()=>{
    const f=byId('typing');f.hidden=!f.hidden;byId('keyboard').setAttribute('aria-expanded',String(!f.hidden));
    if(!f.hidden) byId('entry').focus();
};
byId('typing').onsubmit=e=>{
    e.preventDefault();
    const input=byId('entry'), chars=Array.from(input.value);
    if(chars.length && !/[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Zl}\p{Zp}]/u.test(input.value)) {
        for(let i=0;i<chars.length;i+=32) key(2,chars.slice(i,i+32).join(''));
        input.value='';
    }
};
for(const [id,name] of [['enter','enter'],['back','backspace']]) byId(id).onclick=()=>{key(1,name);key(0,name);};
byId('end').onclick=()=>endVisit('השיתוף הסתיים.');
window.addEventListener('pagehide',()=>endVisit('השיתוף הסתיים.'));
document.addEventListener('visibilitychange',()=>{if(document.hidden && visitor) endVisit('השיתוף הסתיים כשהאפליקציה עברה לרקע.');});
