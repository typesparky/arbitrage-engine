#!/usr/bin/env python3.12
"""Test the exact JS from the scraper."""
import asyncio
from playwright.async_api import async_playwright

_JS = (
    '(function(){'
    'var r=[];'
    'var cells=document.querySelectorAll(\'li[data-testid="item-cell"]\');'
    'console.log("cells:", cells.length);'
    'for(var i=0;i<cells.length;i++){'
    'var c=cells[i];'
    'var a=c.querySelector(\'a[data-testid="thumbnail-link"]\')||c.querySelector(\'a[href*="/item/"]\');'
    'if(!a)continue;'
    'var href=a.href||"";'
    'var it=a.querySelector(\'[itemtype="ITEM_TYPE_MERCARI"]\');'
    'var id=it?it.id:"";'
    'if(!id){var m=href.match(/item\\/([a-zA-Z0-9]+)/);id=m?m[1]:"";}'
    'if(!id)continue;'
    'var img=c.querySelector(\'img[src]\');var thumb=img?img.src:"";'
    'var label=a.getAttribute(\'aria-label\')||"";'
    'var pm=label.match(/([\\d,]+)円/);'
    'var price=pm?parseInt(pm[1].replace(/,/g,"")):0;'
    'var title="";'
    'title=a.textContent.trim();'
    'if(title&&price>0)r.push({id:id,title:title,price:price});'
    '}'
    'return r;})()'
)

async def main():
    async with async_playwright() as p:
        br = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await br.new_context(user_agent="Mozilla/5.0")
        page = await ctx.new_page()
        await page.goto("https://jp.mercari.com/search?keyword=anime%20figure", timeout=120000)
        await asyncio.sleep(8)

        # Test 1: simple selector count
        count = await page.evaluate("document.querySelectorAll('li[data-testid=\"item-cell\"]').length")
        print(f"Direct selector: {count}")

        # Test 2: our JS
        result = await page.evaluate(_JS)
        print(f"JS result: {len(result)} items")
        if result:
            print(f"First: {result[0]}")

        # Test 3: simpler JS
        simple = await page.evaluate('''(function(){
            var r=[];
            var cells=document.querySelectorAll('li[data-testid="item-cell"]');
            for(var i=0;i<Math.min(cells.length,3);i++){
                var c=cells[i];
                var a=c.querySelector('a');
                r.push({href:a?a.href:'',text:a?a.textContent.trim().substring(0,50):''});
            }
            return r;
        })()''')
        print(f"Simple test: {simple}")

        await br.close()

asyncio.run(main())
