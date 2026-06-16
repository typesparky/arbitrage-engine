#!/usr/bin/env python3.12
"""Test if Playwright can reach Mercari JP."""
import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        br = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        ctx = await br.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        )
        await ctx.add_init_script(
            'Object.defineProperty(navigator,"webdriver",{get:()=>undefined});'
        )
        page = await ctx.new_page()
        print("Navigating to Mercari JP...")
        try:
            resp = await page.goto(
                "https://jp.mercari.com/search?keyword=anime%20figure",
                timeout=120000
            )
            print(f"Status: {resp.status if resp else 'none'}")
            await asyncio.sleep(5)
            title = await page.title()
            print(f"Title: {title}")
            links = await page.evaluate(
                "document.querySelectorAll('a[href*=\"/item/\"]').length"
            )
            print(f"Item links: {links}")
            if links == 0:
                # Check what's on the page
                body_text = await page.evaluate("document.body.innerText.substring(0,500)")
                print(f"Body text: {body_text}")
        except Exception as e:
            print(f"Error: {e}")
        await br.close()

asyncio.run(main())
