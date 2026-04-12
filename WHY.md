# Why magpie

## The obvious version

The obvious version of a scraper is a Python file per site. Every tutorial does it that way: one script, one BeautifulSoup call, one cron job.

## Why I built it differently

I built magpie because every job that touches scraping eventually asks the same question: "can a non-engineer add a new source?" YAML is not a silver bullet but it changes who can contribute. The self-healing piece exists because selectors break constantly, and the default failure mode — a cron quietly producing zero items until someone notices next week — is worse than a loud PR asking for review. I chose to open PRs instead of auto-fixing because silent auto-merges hide drift and make debugging harder six months later. Loud failures are debuggable; silent ones are how you lose data for six weeks and do not know.

## What I'd change if I did it again

I would invest earlier in a local replay mode that serves cached HTML fixtures so the full test suite runs without network access. The current integration tests hit fixture files, but the gap between fixture HTML and live HTML grows over time and tests can pass while real scrapes silently degrade.
