# TradingBot V15 Master

V11-V15 zusammen:
- Scanner Engine
- Setup Engine
- Trade Manager
- Dashboard Pro
- Performance Brain
- Macro Intelligence
- News Impact
- Top Setups
- Pine Signal / Scanner / Macro

Installation im bestehenden Git-Ordner:

```bash
cd /opt/tradingbot_v9
cp .env /tmp/tradingbot.env
docker compose down
find . -mindepth 1 ! -name ".git" ! -name ".env" -exec rm -rf {} +
unzip /opt/tradingbot_v15_master.zip
cp /tmp/tradingbot.env .env
docker compose up -d --build
```
