# Telegram Bot Setup Guide

Follow these steps to enable real-time price spike and volume alerts on your Telegram application.

## 1. Create a Telegram Bot
1. Open Telegram and search for **@BotFather**.
2. Start a chat and send the command: `/newbot`
3. Follow the instructions to name your bot (e.g., `MyScreenerBot`).
4. **Important**: Copy the **API Token** provided (it looks like `123456789:ABCdefGHIjklMNOpqrSTUvwxYZ`).

## 2. Get your Chat ID
1. Search for **@userinfobot** in Telegram.
2. Start the chat. It will immediately reply with your **Id** (a 9 or 10-digit number).
3. Copy this number.

## 3. Configure the Application
Open your `.env` file (or create one in the `backend` folder) and add the following lines:

```env
ENABLE_TELEGRAM=True
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrSTUvwxYZ
TELEGRAM_CHAT_ID=987654321
```

## 4. Test the Connection
Before waiting for a real alert, you can verify your bot is working by visiting this URL in your browser while the app is running:
`http://localhost:8000/api/test-telegram`
*(Or replace localhost with your VPS IP address)*

If successful, you will receive a 🔔 **Test Alert** on your Telegram app.

## 5. VPS Deployment (SmarterASP.NET)
If you are running on a VPS, make sure to add these variables to your environment settings or the `.env` file in the same folder as `main.py`. The `run_vps.bat` script will automatically pick them up.
