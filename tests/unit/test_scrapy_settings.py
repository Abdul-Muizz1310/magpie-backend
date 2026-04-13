"""Tests for scrapy.settings constants."""

from magpie.scrapy import settings


class TestScrapySettings:
    def test_twisted_reactor(self) -> None:
        assert settings.TWISTED_REACTOR == "twisted.internet.asyncioreactor.AsyncioSelectorReactor"

    def test_bot_name(self) -> None:
        assert settings.BOT_NAME == "magpie"

    def test_robotstxt_obey(self) -> None:
        assert settings.ROBOTSTXT_OBEY is True

    def test_log_disabled(self) -> None:
        assert settings.LOG_ENABLED is False

    def test_fingerprinter_version(self) -> None:
        assert settings.REQUEST_FINGERPRINTER_IMPLEMENTATION == "2.7"
