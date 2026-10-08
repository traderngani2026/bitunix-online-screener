# Bitunix Screener — Online Web App

Mobile-friendly Streamlit web app with two tabs:
- Moving Average Screener (EMA 19, SMA 23, SMA 100, consolidation, breakout/breakdown)
- MACD Screener (12/26/9; 15m, 1H, 4H, 1D, 1W)

Uses Bitunix public market endpoints and does not require API keys. Signals use closed candles. This is a screening tool, not financial advice.

## Publish online (free option)

This ZIP is prepared for Streamlit Community Cloud. To get a public URL, you must publish the files under your own account; I cannot publish into your account without its authorized access.

1. Extract this ZIP.
2. Sign in to GitHub and create a **new repository** (for easiest setup, make it public).
3. Upload `app.py`, `requirements.txt`, and the `.streamlit` folder to the repository root.
4. Open https://share.streamlit.io/ and sign in using GitHub.
5. Choose **Create app**, select your repository and branch, set the app file path to `app.py`, then deploy.
6. Streamlit will provide a shareable `https://...streamlit.app` link. Open that link on your phone.

Official instructions: https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy

## Important hosting notes

- Your public URL will be available independently of your home PC, but a free host may sleep while idle and may restart the app.
- Scans run when the app is opened or when you press **SCAN NOW**; this is not a guaranteed 24/7 alert service.
- Public market API availability/rate limits are controlled by Bitunix. If a scan returns no data, try again later.
- The app contains no private API credentials. Do not add exchange API keys to a public repository.
