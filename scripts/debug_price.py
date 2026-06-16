#!/usr/bin/env python3.12
"""Debug where the actual yen price is in Mercari DOM."""
import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        br = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await br.new_context(user_agent="Mozilla/5.0")
        page = await ctx.new_page()
        await page.goto("https://jp.mercari.com/search?keyword=anime%20figure", timeout=120000)
        await asyncio.sleep(8)

        # Get the first item-cell and dump all text/attributes
        result = await page.evaluate("""() => {
            var cell = document.querySelector('li[data-testid="item-cell"]');
            if (!cell) return 'no cell';
            var data = {};
            data.html = cell.innerHTML.substring(0, 2000);
            data.allText = cell.innerText.substring(0, 500);
            // Find all elements with numbers
            var prices = [];
            var all = cell.querySelectorAll('*');
            for (var i = 0; i < all.length; i++) {
                var t = all[i].textContent.trim();
                if (t.match(/[0-9,]+円/) || t.match(/€[0-9.]+/) || t.match(/¥[0-9,]+/)) {
                    prices.push({tag: all[i].tagName, cls: all[i].className.substring(0,50), text: t.substring(0,50)});
                }
            }
            data.prices = prices;
            // Check data attributes
            var attrs = {};
            for (var i = 0; i < cell.attributes.length; i++) {
                attrs[cell.attributes[i].name] = cell.attributes[i].value;
            }
            data.cellAttrs = attrs;
            return JSON.stringify(data);
        }""")
        import json
        d = json.loads(result)
        print("=== CELL ATTRIBUTES ===")
        for k, v in d.get("cellAttrs", {}).items():
            print(f"  {k}: {v}")
        print("\n=== PRICE ELEMENTS ===")
        for p in d.get("prices", []):
            print(f"  <{p['tag']} class='{p['cls']}'> {p['text']}")
        print("\n=== ALL TEXT ===")
        print(d.get("allText", "")[:300])
        print("\n=== HTML SNIPPET ===")
        print(d.get("html", "")[:1000])

        await br.close()

asyncio.run(main())
