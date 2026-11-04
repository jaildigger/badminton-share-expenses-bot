// Real local API and UI, disposable database; no Telegram messages are sent.
const {chromium} = require('playwright');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const {randomUUID} = require('node:crypto');
const assert = require('node:assert/strict');
const path = require('node:path');
(async()=>{
  const server=spawn(process.env.PYTHON||'python3',['-m','tests.miniapp_fixture'],{cwd:path.resolve(__dirname,'..'),stdio:['ignore','pipe','inherit']});
  let browser;
  try{
    const [chunk]=await once(server.stdout,'data');
    const {url,auth}=JSON.parse(chunk.toString());
    const headers={'X-Telegram-Init-Data':auth,'Content-Type':'application/json'};
    let state=await (await fetch(url+'/api/state',{headers})).json();
    async function change(action,values={}){
      const response=await fetch(url+'/api/action',{method:'POST',headers,body:JSON.stringify({action,tid:state.training?.id||0,revision:state.revision,request_id:randomUUID(),...values})});
      state=await response.json();assert.equal(response.status,200,JSON.stringify(state));
    }
    await change('create',{date:state.today,location_id:state.locations[0].id,court_cost:'300'});
    for(const name of ['Андрей','Мария','Борис'])await change('add',{name,saved:true});
    const maria=state.training.participants.find(p=>p.name==='Мария').person_id;
    const andrey=state.training.participants.find(p=>p.name==='Андрей').person_id;
    await change('participant',{pid:maria,hours:'2',shuttle_count:'6',shuttle_price:'100'});
    await change('payer',{pid:andrey,amount:'300'});
    browser=await chromium.launch({headless:true,channel:process.env.BROWSER_CHANNEL||'chrome'});
    const page=await browser.newPage({viewport:{width:390,height:844}});
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.route('https://telegram.org/js/telegram-web-app.js',r=>r.fulfill({contentType:'text/javascript',body:`window.Telegram={WebApp:{initData:${JSON.stringify(auth)},ready(){},expand(){},onEvent(){},BackButton:{show(){},hide(){},onClick(){}}}};`}));
    await page.goto(url);
    await page.getByRole('button',{name:'Тренировки',exact:true}).click();
    await page.locator('.training-featured').click();
    await page.getByRole('button',{name:'Посмотреть расчёт →',exact:true}).click();
    const dialog=page.getByRole('dialog');
    await dialog.getByText('Баланс: -300 ฿',{exact:true}).waitFor();
    await dialog.getByText(/Плательщик корта «Андрей» должен вернуть участнику «Мария» 300.00/).waitFor();
    await page.screenshot({path:'/tmp/badminton-collector-preview.png',fullPage:true});
    await dialog.getByRole('button',{name:'Зафиксировать расчёт',exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector('#modal').open);
    const refund=page.locator('.transfer').filter({hasText:'Андрей → Мария'});
    await refund.getByRole('button',{name:'Отметить оплату'}).click();
    await page.getByLabel('Полученная сумма, ฿').fill('100');
    await dialog.getByRole('button',{name:'Отметить оплату',exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector('#modal').open);
    await page.getByText(/Плательщик корта «Андрей» должен вернуть участнику «Мария» 200.00/).waitFor();
    assert.deepEqual(errors,[]);
    console.log('Collector routing, negative balance, refund comment and partial payment UI passed.');
  }finally{if(browser)await browser.close();server.kill();}
})().catch(e=>{console.error(e);process.exitCode=1;});
