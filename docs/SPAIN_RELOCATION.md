# Spain Relocation Radar

The Spain radar continuously discovers public Telegram sources about relocation to Spain, visas, Digital Nomad, freelance work and residence permits. It incrementally reads only new messages and keeps evidence related to applications through Saint Petersburg.

## Continuous standalone mode

```powershell
python spain_relocation_radar.py
```

The process remains active. Press `Ctrl+C` to stop it.

## Nexus mode

Set this in `.env`:

```env
RADAR_MODE=spain
```

Then run:

```powershell
python nexus.py
```

Nexus supervises and restarts the Spain radar if it crashes.

When `SPAIN_BOT_TOKEN` and `ADMIN_ID` are set, Nexus also starts the Spain Radar
bot. Its menu is available to every Telegram user who opens the bot. It shows
radar status, all discovered chats, confirmed cases,
review candidates and source-message links. New findings are delivered
automatically to `ADMIN_ID`; previously unsent findings are delivered once after
the bot starts.

Bot menu:

- `📊 Статус радара` — whether the radar is active and current totals;
- `💬 Все чаты` — paginated list of discovered public Telegram sources;
- `✅ Подтверждённые` — direct and professional Saint Petersburg cases;
- `🧐 Кандидаты` — items that still require manual verification;
- `🆕 Последние` — the newest findings of both kinds.

## One cycle

```powershell
python spain_relocation_radar.py --once
```

State is stored in `data/spain_relocation.db`. Reports are refreshed at `reports/spain_spb_cases.md` and `reports/spain_spb_cases.json`.

Discovery runs every three hours by default. Known chats with new traffic are revisited after 15 minutes and idle chats after one hour. The process wakes every five minutes to check which work is due.
