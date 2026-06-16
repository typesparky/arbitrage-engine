#!/usr/bin/env python3.12
"""Debug Mercari JP DOM structure."""
import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
        )
        await ctx.add_init_script(
            'Object.defineProperty(navigator, "webdriver", { get: () => undefined })'
        )
        page = await ctx.new_page()
        await page.goto(
            "https://jp.mercari.com/search?keyword=anime%20figure",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await asyncio.sleep(8)

        # Check selectors using page.query_selector_all
        selectors = [
            'a[href*="/item/"]',
            'div[class*="ItemCard"]',
            'div[class*="item"]',
            'section[class*="item"]',
            'li[class*="item"]',
            'div[data-testid]',
            'div[class*="ListItem"]',
            'div[class*="search-result"]',
            'div[class*="SearchResult"]',
            'div[class*="Grid"]',
            'div[class*="list"]',
        ]
        for sel in selectors:
            elements = await page.query_selector_all(sel)
            print(f"{sel}: {len(elements)}")

        # Get DOM chain
        chain = await page.evaluate(
            """() => {
                var lnk = document.querySelector('a[href*="/item/"]');
                if (!lnk) return 'no link';
                var el = lnk;
                var chain = [];
                for (var i = 0; i < 5; i++) {
                    chain.push(el.tagName + '.' + (el.className || '').substring(0, 80));
                    el = el.parentElement;
                    if (!el) break;
                }
                return chain.join(' > ');
            }"""
        )
        print(f"\nDOM chain: {chain}")

        # HTML snippet
        snippet = await page.evaluate(
            """() => {
                var lnk = document.querySelector('a[href*="/item/"]');
                if (!lnk) return 'no link';
                return lnk.parentElement.parentElement.outerHTML.substring(0, 800);
            }"""
        )
        print(f"\nHTML snippet:\n{snippet}")

        await browser.close()


asyncio.run(main())
