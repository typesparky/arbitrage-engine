#!/usr/bin/env python3.12
"""Debug which selectors work on Mercari JP."""
import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        br = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await br.new_context(user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36")
        page = await ctx.new_page()
        await page.goto("https://jp.mercari.com/search?keyword=anime%20figure", timeout=120000)
        await asyncio.sleep(8)

        selectors = [
            'li[data-testid="item-cell"]',
            'a[href*="/item/"]',
            'div[data-testid*="item"]',
            'div[class*="ItemCard"]',
            'div[class*="ListItem"]',
            'section[class*="item"]',
            'div[class*="item-cell"]',
            'div[class*="search-result"]',
            'div[class*="SearchResult"]',
            'div[class*="Grid"]',
            'div[class*="grid"]',
            'ul li',
        ]
        for sel in selectors:
            count = await page.evaluate(
                "document.querySelectorAll('" + sel + "').length"
            )
            print(f"{sel}: {count}")

        # Check what data-testid values exist
        testids = await page.evaluate(
            "Array.from(document.querySelectorAll('[data-testid]')).map(e => e.getAttribute('data-testid')).filter((v,i,a) => a.indexOf(v) === i).slice(0,20)"
        )
        print(f"\nAll data-testid values: {testids}")

        # Check class names containing "item"
        classes = await page.evaluate(
            "Array.from(document.querySelectorAll('[class]')).map(e => e.className).filter(c => c.toLowerCase().includes('item')).slice(0,10)"
        )
        print(f"\nClasses with 'item': {classes}")

        await br.close()

asyncio.run(main())
