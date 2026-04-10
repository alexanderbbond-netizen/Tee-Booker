name: Tee Time Booker
on:
  schedule:
    - cron: '55 18 * * 6'   # 19:55 UTC every Saturday (BST = UTC+1, so this = 19:55 BST)
  workflow_dispatch:          # allows manual trigger from GitHub Actions tab

jobs:
  book:
    runs-on: ubuntu-latest
    steps:
      - name: Check out code
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: |
          pip install -r requirements.txt
          playwright install chromium
          playwright install-deps chromium

      - name: Run booking scheduler
        env:
          IG_CLUB_URL:   ${{ secrets.IG_CLUB_URL }}
          IG_USERNAME:   ${{ secrets.IG_USERNAME }}
          IG_PASSWORD:   ${{ secrets.IG_PASSWORD }}
          NOTIFY_EMAIL:  ${{ secrets.NOTIFY_EMAIL }}
          SMTP_USER:     ${{ secrets.SMTP_USER }}
          SMTP_PASSWORD: ${{ secrets.SMTP_PASSWORD }}
        run: python scheduler.py

      # Upload any screenshots taken during failure so we can inspect them
      - name: Upload debug screenshots
        if: failure()
        uses: actions/upload-artifact@v4
        with:
          name: debug-screenshots
          path: /tmp/*.png
          if-no-files-found: ignore
