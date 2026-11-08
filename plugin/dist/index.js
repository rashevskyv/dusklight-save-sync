// Decky API connection bootstrap, matching the API bundled by installed plugins.
const init = window.__DECKY_SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED_deckyLoaderAPIInit;
if (!init) throw new Error('Decky API is unavailable');
let api;
try { api = init.connect(2, 'Dusklight Save Sync'); }
catch { api = init.connect(1, 'Dusklight Save Sync'); }
const React = window.SP_REACT;
const h = React.createElement;
const cursorKey = 'dusklight-save-sync:event-cursor';
let cursor = Number(window.localStorage.getItem(cursorKey) || 0);
let polling = false;
let latestStatus = null;
const listeners = new Set();
function toast(body, title = 'Dusklight — сейви') {
  api.toaster.toast({title, body, duration: 6500, playSound: false});
}
async function poll() {
  if (polling) return;
  polling = true;
  try {
    const response = await api.call('get_events', cursor);
    for (const event of response.events || []) toast(event.body, event.title);
    cursor = response.latest_sequence || cursor;
    window.localStorage.setItem(cursorKey, String(cursor));
    latestStatus = await api.call('get_status');
    for (const listener of listeners) listener(latestStatus);
  } catch (error) { console.warn('[Dusklight Save Sync]', String(error)); }
  finally { polling = false; }
}
const buttonStyle = {width:'100%',margin:'8px 0',padding:'12px',border:'1px solid #566273',
                     borderRadius:'4px',background:'#263545',color:'white',fontSize:'15px'};
function Panel() {
  const [status, setStatus] = React.useState(latestStatus);
  React.useEffect(() => { listeners.add(setStatus); return () => listeners.delete(setStatus); }, []);
  const details = status?.status;
  return h('div',{style:{padding:'12px',lineHeight:'1.5',color:'white'}},
    h('div',{style:{fontSize:'18px',fontWeight:'bold'}},'Dusklight Save Sync'),
    h('div',null,status?.active ? 'Watcher працює у фоні' : 'Watcher ще не підтвердив роботу'),
    details?.checked_at && h('div',{style:{fontSize:'12px',opacity:.8}},'Перевірка: '+new Date(details.checked_at).toLocaleTimeString()),
    details?.error && h('div',{style:{color:'#ffcf80'}},'Немає синхронізації. Сейв залишається на Deck; спробу буде повторено.'),
    details?.result?.deferred?.length > 0 && h('div',null,'Новіший сейв із хмари чекає закриття гри.'),
    h('button',{style:buttonStyle,tabIndex:0,onClick:async()=>{
      const result=await api.call('sync_now'); if(!result.success) toast(result.message);
    }},'Перевірити синхронізацію'),
    h('button',{style:buttonStyle,tabIndex:0,onClick:async()=>{
      const result=await api.call('test_notification'); if(!result.success) toast('Не вдалося створити тестове сповіщення.');
    }},'Тест попапа'),
    h('div',{style:{fontSize:'12px',opacity:.8}},'Новий локальний сейв надсилається під час гри. Новіший хмарний сейв установлюється після її закриття.')
  );
}
export default function () {
  const interval=setInterval(poll,2000); poll();
  return {name:'Dusklight Save Sync',titleView:h('div',null,'Dusklight Save Sync'),
          content:h(Panel),icon:h('span',null,'☁'),alwaysRender:true,
          onDismount(){clearInterval(interval);listeners.clear();}};
}
