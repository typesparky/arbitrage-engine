"""Lightweight Mercari JP browser scraper."""
from __future__ import annotations
import asyncio, time
from typing import Any
from loguru import logger
from src.config.settings import settings
from src.scrapers.base import BaseScraper, ScrapeResult, ScrapedItem

_MERCARI_SEARCH = "https://jp.mercari.com/search"
_MERCARI_ITEM = "https://jp.mercari.com/item/{}"

_JS_SEARCH = (
    "(function(){"
    "var r=[];"
    "var cells=document.querySelectorAll('li[data-testid=\"item-cell\"]');"
    "for(var i=0;i<cells.length;i++){"
    "var c=cells[i];"
    "var a=c.querySelector('a[data-testid=\"thumbnail-link\"]')||c.querySelector('a[href*=\"/item/\"]');"
    "if(!a)continue;"
    "var href=a.href||'';"
    "var m=href.match(/item\\/([a-zA-Z0-9]+)/);"
    "var id=m?m[1]:'';"
    "if(!id)continue;"
    "var thumbDiv=c.querySelector('div[itemtype=\"ITEM_TYPE_MERCARI\"]');"
    "var label='';"
    "if(thumbDiv)label=thumbDiv.getAttribute('aria-label')||'';"
    "if(!label)label=a.getAttribute('aria-label')||a.textContent.trim()||'';"
    "var thumb='';"
    "var img=c.querySelector('img[src]');"
    "if(img)thumb=img.src;"
    "var price=0;"
    "var pm=label.match(/([0-9,]+)円/);"
    "if(pm)price=parseInt(pm[1].replace(/,/g,''));"
    "if(price===0){var pn=label.match(/([0-9,]+)/);if(pn)price=parseInt(pn[1].replace(/,/g,''));}"
    "var title='';"
    "var te=c.querySelector('div[class*=\"title\"],span[class*=\"title\"],p[class*=\"title\"],div[class*=\"name\"],div[class*=\"item-name\"],span[class*=\"item-name\"]');"
    "if(te)title=te.textContent.trim();"
    "if(!title){var parts=label.split('の画像');if(parts.length>0)title=parts[0].trim();}"
    "if(!title&&a.textContent){title=a.textContent.trim().substring(0,80);}"
    "if(title&&price>0)r.push({id:id,title:title,price:price,thumbnail:thumb,url:href.startsWith('http')?href:'https://jp.mercari.com'+href});"
    "}"
    "return r;})()"
)

_JS_ITEM = (
    "(function(){"
    "var t=document.querySelector('h1')?document.querySelector('h1').textContent.trim():'';"
    "var pt=document.querySelector('span[class*=\"price\"]');"
    "var pText=pt?pt.textContent.trim():'0';"
    "var p=parseInt(pText.replace(/[^0-9]/g,''))||0;"
    "var de=document.querySelector('div[class*=\"description\"]');"
    "var d=de?de.textContent.trim():'';"
    "return{title:t,price:p,desc:d};})()"
)


class MercariBrowserScraper(BaseScraper):
    PLATFORM = "mercari_jp"
    def __init__(self, proxy_url="", user_agent=""):
        super().__init__(proxy_url=proxy_url or settings.proxy_url, user_agent=user_agent or settings.user_agent)
        self._pw = self._br = self._ctx = None

    async def _ensure(self):
        if self._ctx: return
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self._br = await self._pw.chromium.launch(headless=True, args=["--no-sandbox","--disable-dev-shm-usage"])
        self._ctx = await self._br.new_context(user_agent=self.user_agent, viewport={"width":1920,"height":1080})

    async def search(self, query, category="", max_items=50, **kwargs):
        t = time.monotonic(); items = []
        try:
            await self._ensure()
            page = await self._ctx.new_page()
            try:
                url = _MERCARI_SEARCH + "?keyword=" + query
                await page.goto(url, timeout=120000)
                await asyncio.sleep(8)
                for _ in range(min(4, max_items // 8)):
                    await page.evaluate("window.scrollTo(0,document.body.scrollHeight)")
                    await asyncio.sleep(2)
                raw = await page.evaluate(_JS_SEARCH)
                logger.debug("Mercari: %d items", len(raw))
                for e in raw[:max_items]:
                    try:
                        items.append(ScrapedItem(external_id=e["id"],url=e["url"],title=e["title"],
                            price=float(e["price"]),currency="JPY",thumbnail_url=e.get("thumbnail",""),
                            image_urls=[e["thumbnail"]] if e.get("thumbnail") else [],is_available=True))
                    except: pass
            finally:
                await page.close()
        except ImportError: logger.error("pip install playwright && playwright install chromium")
        except Exception as e: logger.error("Mercari failed: %s", e)
        return ScrapeResult(items=items,total_found=len(items),duration_seconds=time.monotonic()-t)

    async def get_item(self, external_id):
        try:
            await self._ensure()
            page = await self._ctx.new_page()
            try:
                await page.goto(_MERCARI_ITEM.format(external_id), timeout=60000)
                await asyncio.sleep(5)
                d = await page.evaluate(_JS_ITEM)
                if d and d.get("title") and d.get("price",0)>0:
                    return ScrapedItem(external_id=external_id,url=_MERCARI_ITEM.format(external_id),
                        title=d["title"],description=d.get("desc",""),price=float(d["price"]),currency="JPY",is_available=True)
            finally:
                await page.close()
        except Exception as e:
            logger.debug("get_item: %s", e)
        return None

    async def check_availability(self, external_id):
        try: return await self.get_item(external_id) is not None
        except: return False

    async def close(self):
        if self._ctx is not None: await self._ctx.close()
        if self._br is not None: await self._br.close()
        if self._pw is not None: await self._pw.stop()
        self._ctx = self._br = self._pw = None
