X-Trader AutoBot (Desktop)

What it does
- Reads the site signal (same as the website): https://x-trader.cloud/latest.json
- Executes Binance Margin orders + SL/TP using the GUI parameters.
- Prefetches maxBorrowable ~10s before the 5m boundary and applies Safety% (subtracts from maxBorrowable).

"SERVER: ONLINE"
- Means the latest.json read succeeded within the last ~5 seconds.
- If the site is offline or network is down, it stays OFFLINE and will not trade.

Registration / License
- Trading is enabled only with an **active license key**.
- If the key is used on multiple machines at the same time, the server blocks the license.

Run (Windows)
1) pip install -r requirements.txt
2) python app.pyw
3) Press START (ARMED)

Notes
- API Key / Secret stay ONLY inside the desktop app (never on the website).
