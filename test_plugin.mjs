import fs from 'node:fs';
let interval, toasts=[],stored={};
const events=[{sequence:1,title:'Dusklight',body:'Сейв відправлено.'}];
const api={call:async(method,args)=>method==='get_events'?{latest_sequence:1,events:events.filter(x=>x.sequence>args)}:{active:true,status:{}},toaster:{toast:x=>toasts.push(x)}};
globalThis.window={__DECKY_SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED_deckyLoaderAPIInit:{connect:()=>api},
SP_REACT:{createElement:(...args)=>args,useState:()=>[null,()=>{}],useEffect:()=>{}},
localStorage:{getItem:key=>stored[key],setItem:(key,value)=>stored[key]=value}};
globalThis.setInterval=(fn)=>{interval=fn;return 1};globalThis.clearInterval=()=>{};
const source=fs.readFileSync(new URL('./plugin/dist/index.js', import.meta.url),'utf8');
const module=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const plugin=module.default();
await new Promise(resolve=>setTimeout(resolve,10));
if(toasts.length!==1)throw new Error('Expected one Steam toast');
await interval();if(toasts.length!==1)throw new Error('Repeated event produced duplicate toast');
if(!plugin.content||!plugin.onDismount)throw new Error('Invalid Decky plugin');
plugin.onDismount();console.log('PASS: Decky API event poll creates toast once; lifecycle hooks and plugin content valid.');
