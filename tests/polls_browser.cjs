// Native Telegram delivery is mocked in Python tests; this checks the actual Mini App/API.
const {chromium} = require('playwright');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const assert = require('node:assert/strict');
const path = require('node:path');
(async()=>{
  const server=spawn(process.env.PYTHON||'python3',['-m','tests.miniapp_fixture'],{cwd:path.resolve(__dirname,'..'),env:{...process.env,MINIAPP_TEST_POLL:'1'},stdio:['ignore','pipe','inherit']});
  let browser;
  try{
    const [chunk]=await once(server.stdout,'data');
    const {url,auth}=JSON.parse(chunk.toString());
    browser=await chromium.launch({headless:true,channel:process.env.BROWSER_CHANNEL||'chrome'});
    const page=await browser.newPage({viewport:{width:390,height:844}});
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.route('https://telegram.org/js/telegram-web-app.js',route=>route.fulfill({contentType:'text/javascript',body:`window.Telegram={WebApp:{initData:${JSON.stringify(auth)},colorScheme:'light',ready(){},expand(){},onEvent(){},BackButton:{show(){},hide(){},onClick(){}}}};`}));
    await page.goto(url);
    const click=async(name)=>page.getByRole('button',{name,exact:true}).click();
    const submit=async(name)=>{await page.getByRole('dialog').getByRole('button',{name,exact:true}).click();await page.waitForFunction(()=>!document.querySelector('#modal').open);};
    await click('Голосования');
    await page.getByText('Настя',{exact:true}).waitFor();
    await click('+ Создать');
    assert.equal(await page.getByLabel('Варианты ответа — каждый с новой строки').inputValue(),'19:00-21:00\nThinking\nNo');
    await page.getByLabel('Варианты ответа — каждый с новой строки').fill('18:00-20:00\nThinking\nNo');
    await page.screenshot({path:'/tmp/badminton-poll-editor.png',fullPage:true});
    await submit('Опубликовать голосование');
    await page.getByText('В очереди',{exact:true}).waitFor();
    await click('Тренировки');
    await click('+ Новая');
    await page.getByLabel('Добавить участников голосования').waitFor();
    assert.equal(await page.getByLabel('Добавить участников голосования').isChecked(),true);
    assert.match(await page.getByRole('dialog').innerText(),/Настя/);
    await page.getByLabel('Полная стоимость корта, ฿').fill('200');
    await submit('Создать тренировку');
    await page.locator('.person').filter({hasText:'Настя'}).waitFor();
    assert.equal(await page.locator('.person').filter({hasText:'Настя'}).count(),1);
    await click('← Тренировки');
    await click('+ Новая');
    await page.getByRole('heading',{name:'Тренировка уже существует'}).waitFor();
    await submit('Открыть тренировку');
    await page.getByRole('heading',{name:'02 · Участники'}).waitFor();
    await click('Голосования');
    await page.locator('#notice').waitFor({state:'hidden'});
    await page.screenshot({path:'/tmp/badminton-polls.png',fullPage:true});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    assert.deepEqual(errors,[]);
    console.log('Poll editor, roster import, voter list and existing-date guard passed.');
  }finally{if(browser)await browser.close();server.kill();}
})().catch(error=>{console.error(error);process.exitCode=1;});
