"""Default Scrapy settings for magpie spiders."""

# Use asyncio reactor to avoid Twisted/asyncio conflicts
TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"

BOT_NAME = "magpie"
ROBOTSTXT_OBEY = True
LOG_ENABLED = False
REQUEST_FINGERPRINTER_IMPLEMENTATION = "2.7"
