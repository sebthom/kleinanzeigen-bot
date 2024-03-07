"""
SPDX-FileCopyrightText: © Sebastian Thomschke and contributors
SPDX-License-Identifier: AGPL-3.0-or-later
SPDX-ArtifactOfProjectHomePage: https://github.com/Second-Hand-Friends/kleinanzeigen-bot/
"""
import pytest

from kleinanzeigen_bot.web_scraping_mixin import WebScrapingMixin
from kleinanzeigen_bot import utils


@pytest.mark.asyncio
@pytest.mark.itest
async def test_webdriver_auto_init() -> None:
    web_scraping_mixin = WebScrapingMixin()
    web_scraping_mixin.browser_config.arguments = ["--no-sandbox"]

    browser_path = web_scraping_mixin.get_compatible_browser()
    utils.ensure(browser_path is not None, "Browser not auto-detected")

    web_scraping_mixin.close_browser_session()
    await web_scraping_mixin.create_browser_session()
    web_scraping_mixin.close_browser_session()
