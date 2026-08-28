# Operations Guide

Practical commands for running and maintaining PainSearchRadar.

## Start

```powershell
cd C:\Users\user\PycharmProjects\PainSearchRadar
python nexus.py
```

With `RADAR_MODE=margin`, Nexus starts only `margin_leak_radar.py`.
With `RADAR_MODE=classic`, it starts the four classic workers.

Telegram, Telegram Bot API, and Groq use the normal system connection.

Nexus prints request/lead counts and the `Годный` feedback rate every minute.

In LeadRadar mode the dashboard also prints the 20-minute, hourly, and daily
candidate funnel, rejection reasons, both account budgets, live membership,
the Hot queue, and recent source yield.

## Stop

Press `Ctrl+C` in the PowerShell window where `nexus.py` is running.

In classic mode Nexus should stop:

- `tg_bot.py`
- `scout.py`
- `miner.py`
- `butcher.py`

In margin mode it should stop `margin_leak_radar.py`.

## Check Running Processes

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.Name -like 'python*' -and $_.CommandLine -match '(nexus\.py|margin_leak_radar\.py|tg_bot\.py|scout\.py|miner\.py|butcher\.py)' } |
  Select-Object ProcessId,ParentProcessId,CreationDate,CommandLine
```

There should normally be one `nexus.py` and either one margin child or four
classic children.

## Logs

Current logs are under:

```text
logs/YYYY-MM-DD/
```

Useful files:

- `nexus.log` - overall dashboard and daemon restarts.
- `scout.log` - Telegram search and chat acceptance/rejection.
- `miner.log` - chat reading and pain extraction.
- `butcher.log` - clustering, Gold/Silver/Trash decisions.
- `tg_bot.log` - Telegram bot UI.
- `margin_leak_radar.log` - safe polling, live events, verification, and leads.

PowerShell examples:

```powershell
Get-Content .\logs\2026-06-13\nexus.log -Encoding UTF8 -Tail 80
Get-Content .\logs\2026-06-13\miner.log -Encoding UTF8 -Tail 120
```

## Reset Generated Data But Keep Chats

Use this when you want to reprocess known chats from scratch.

```powershell
python reset_db_keep_chats.py --yes --include-invalid
```

This keeps chat targets but clears generated pains/insights.

## Clean Quality State

Use this after changing quality filters.

```powershell
python cleanup_quality_state.py --yes
```

It clears generated outputs, resets keywords, and requeues chats that still look like seller chats.

## Regrade Existing Insights

Use this after changing Gold/Silver/Trash rules.

```powershell
python regrade_insights.py --yes
```

It does not delete insights. It updates quality fields so the bot can avoid Trash.

## DB Browser Lock

If a command hangs or fails with SQLite lock/schema errors:

1. Close DB Browser for SQLite.
2. Stop `nexus.py` with `Ctrl+C`.
3. Run the command again.

DB Browser can hold `scout.db`, `scout.db-wal`, or `scout.db-shm` open.

## Typical "It Is Not Working" Diagnosis

Check Nexus dashboard first.

If it says accounts are cooling down:

```text
accounts=0/2
```

That usually means Telegram accounts are resting. It is not a crash.

If `New=0` and `Pending=0`, the system has eaten the current queue and is waiting for:

- new search keywords,
- new chats,
- new messages in existing chats,
- available Telegram accounts.

If `tg_bot.log` shows `TelegramConflictError`, another bot process with the same bot token is running somewhere. Stop the duplicate.

## Backups

Before risky DB changes, create a backup:

```powershell
Copy-Item .\scout.db ".\backups\db\scout.db.backup_$(Get-Date -Format yyyyMMdd_HHmmss)"
```

Several scripts already create backups automatically, but manual backups are cheap.

## Recommended Daily Routine

1. Run `fast` mode overnight.
2. Review `nexus.log` and top insights in the bot.
3. If too much noise appears, improve `quality_rules.py`.
4. Run:

```powershell
python cleanup_quality_state.py --yes
python nexus.py
```

5. Use `turbo` only for short bursts when you need more data quickly.
