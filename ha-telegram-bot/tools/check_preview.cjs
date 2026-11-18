// Run with a locally installed Playwright; no Telegram or HA requests.
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({headless:true, executablePath:process.env.CHROMIUM_PATH || '/usr/bin/chromium', args:['--no-sandbox']});
  const page = await browser.newPage({viewport:{width:1100,height:900}});
  const errors=[];
  page.on('pageerror', e=>errors.push(e.message));
  await page.goto(process.argv[2] || 'http://127.0.0.1:8765/ha-widget-preview.html');
  const widget = page.frameLocator('#widget');
  await widget.getByRole('button', {name:'Свет',exact:true}).click();
  await widget.getByRole('button', {name:'Включить',exact:true}).click();
  await widget.getByRole('button', {name:'Выключить',exact:true}).waitFor();
  for(const theme of ['Светлая','Тёмная']) {
    await page.getByRole('button',{name:theme,exact:true}).click();
    for(const width of [200,320,430,600]) {
      await page.evaluate(n=>size(n),width);
      const bounds=await widget.locator('body').evaluate(el=>({height:el.scrollHeight,width:el.scrollWidth,client:el.clientWidth}));
      if(bounds.height>600 || bounds.width>bounds.client) throw new Error('Widget clips: '+JSON.stringify(bounds));
    }
  }
  await page.evaluate(()=>size(360));
  await page.screenshot({path:'/tmp/ha-widget-preview.png',fullPage:true});
  await page.getByRole('button',{name:'Другой пользователь'}).click();
  if(!await widget.getByRole('button',{name:'Выключить',exact:true}).isDisabled()) throw new Error('Foreign viewer enabled');
  if(errors.length) throw new Error(errors.join('\n'));
  await browser.close();
  console.log('Preview: light/dark, 200–600 px, actions and ownership passed');
})();
