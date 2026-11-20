# Prodieyy AI chat + browser search

1. Open this folder in VS Code.
2. In PowerShell terminal, create and activate the environment:
   ```powershell
   python -m venv .venv
   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
   .\.venv\Scripts\Activate.ps1
   ```
3. Install packages:
   ```powershell
   pip install -r requirements.txt
   playwright install chromium
   ```
4. Start:
   ```powershell
   uvicorn main:app --reload
   ```
5. Open http://127.0.0.1:8000

This starter has a chat-style UI and uses Playwright to search DuckDuckGo HTML. Chromium opens visibly for searches. Search providers can block automation or change their page layout. It is not an LLM and does not yet extract verified product prices/ratings or perform purchases, bookings, or cancellations.
