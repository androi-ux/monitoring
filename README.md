# iSpace.ge → Telegram monitor

Watches category pages on ispace.ge (iPad, Open box, etc.) and pings you on
Telegram when the listing changes: a new product appears, something goes
back in stock, or something disappears.

## 1. Get a Telegram bot + chat id

1. Message **@BotFather** → `/newbot` → copy the token.
2. Message your new bot anything (e.g. `/start`) so it can message you back.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser,
   find `"chat":{"id":...}` — that's your `TELEGRAM_CHAT_ID`.

(Skip this if you already have a bot from GuideAPI — you can reuse it,
just use a different `TELEGRAM_CHAT_ID`/chat if you don't want the
notifications mixed with the other bot's messages.)

## 2. Run locally first

```bash
cd ispace-monitor
python -m venv venv && source venv/bin/activate   # optional but recommended
pip install -r requirements.txt
cp .env.example .env   # then fill in your token/chat id
export $(cat .env | xargs)   # or use python-dotenv / your shell's env loading
python monitor.py
```

First run just saves `snapshot.json` (baseline, no notification).
Run it again — if nothing changed on the site, you'll see `No changes.`
in the logs. Change nothing, just confirm it runs clean twice before
scheduling it.

**If parsing logs `0 products parsed`**: the site's markup differs from
what was inspected when this was written. Open the page in a browser,
check the dev tools for how product tiles are structured, and adjust
`parse_products()` in `monitor.py` accordingly — the current selector
logic looks for `<a href="...product/...">` links and reads
"Add to cart" / "Notify me" text near them.

## 3. Schedule it (pick one)

### Option A — Render Cron Job (recommended, matches your existing GuideAPI/Render setup)
1. Push this folder to a GitHub repo (or a subfolder of an existing one).
2. Render dashboard → New → **Cron Job**.
3. Build command: `pip install -r requirements.txt`
4. Command: `python monitor.py`
5. Schedule: `*/2 * * * *` for every 2 minutes (Render's cron minimum is
   1 minute on paid, but free-tier cron jobs are typically capped —
   check current limits; every 1–2 min is a reasonable start for a
   fast-selling item).
6. Add `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `WATCH_URLS` as
   environment variables in the Render service settings.
7. **Snapshot persistence**: Render cron jobs don't have a persistent
   disk on the free tier by default — `snapshot.json` would reset every
   run, which means you'd get a false "new product" alert every time.
   Fix: either attach a persistent disk (paid), or store the snapshot
   in Supabase (you already have it) instead of a local file — swap
   `load_snapshot`/`save_snapshot` for two small Supabase calls
   (a single row with a JSON column is enough).

### Option B — GitHub Actions (free, simplest for this specific case)
Actions can commit the snapshot file back to the repo between runs, so
you get free persistence with no extra database:

```yaml
# .github/workflows/monitor.yml
name: ispace monitor
on:
  schedule:
    - cron: '*/2 * * * *'   # every 2 minutes (GitHub's practical minimum ~5 min due to queueing)
  workflow_dispatch:
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install -r requirements.txt
      - run: python monitor.py
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
      - run: |
          git config user.name "monitor-bot"
          git config user.email "actions@github.com"
          git add snapshot.json
          git diff --staged --quiet || git commit -m "update snapshot"
          git push
```
Note: GitHub Actions' scheduled cron isn't guaranteed to run exactly on
time under load — expect a few minutes of jitter. Fine for this use
case, not fine if you need sub-minute reaction time.

### Option C — Your own always-on box
If you have a small VPS anyway, a plain `cron` entry calling
`python monitor.py` every 1 minute is the most predictable option and
avoids all the "free tier persistence" headaches above.

## 4. Tuning

- **Interval**: start at 1–2 minutes. If you start seeing repeated
  timeouts or Cloudflare challenge pages instead of normal HTML,
  back off to 5 minutes — getting blocked entirely is worse than
  checking slightly less often.
- **Multiple pages**: add more URLs to `WATCH_URLS`, comma-separated.
- **Only care about a specific model**: filter in `parse_products` or
  post-filter in `diff_products` by checking `"iPad" in info["name"]`
  or a specific SKU substring.
