# Speed Guide

Speed is controlled through `.env`.

```env
RADAR_SPEED=fast
DB_LOGS=0
```

Restart `nexus.py` after changing speed settings.

## Profiles

| Profile | Use When | Tradeoff |
| --- | --- | --- |
| `safe` | Long careful runs, few Telegram accounts | Slow, lowest risk |
| `fast` | Default 24/7 mode | Good speed, acceptable risk |
| `turbo` | Short aggressive bursts | More flood-waits/rate limits |

## What The Profiles Change

| Setting | safe | fast | turbo |
| --- | ---: | ---: | ---: |
| `MINER_MAX_HISTORY` | 250 | 350 | 500 |
| `MINER_CHATS_PER_ACCOUNT` | 3 | 6 | 10 |
| `MINER_ACCOUNT_COOLDOWN` | 900s | 480s | 300s |
| `MINER_JITTER_MIN/MAX` | 15-45s | 6-18s | 3-9s |
| `SCOUT_REQUESTS_PER_ACCOUNT` | 5 | 10 | 14 |
| `SCOUT_ACCOUNT_COOLDOWN` | 1800s | 900s | 600s |
| `SCOUT_JITTER_MIN/MAX` | 180-420s | 60-150s | 30-90s |
| `BUTCHER_EMPTY_SLEEP` | 300s | 120s | 60s |
| `BUTCHER_CYCLE_SLEEP` | 60s | 30s | 15s |

## Recommended Use

For normal work:

```env
RADAR_SPEED=fast
DB_LOGS=0
```

For a short push when you want more data now:

```env
RADAR_SPEED=turbo
DB_LOGS=0
```

Run `turbo` for a few hours, then return to `fast`.

## Exact Overrides

You can override individual values in `.env`. Overrides beat the selected profile.

Example: keep `fast`, but read more chats per Telegram account:

```env
RADAR_SPEED=fast
MINER_CHATS_PER_ACCOUNT=8
MINER_ACCOUNT_COOLDOWN=420
```

Example: make scout search harder:

```env
SCOUT_REQUESTS_PER_ACCOUNT=12
SCOUT_JITTER_MIN=45
SCOUT_JITTER_MAX=120
```

Example: make butcher produce insights faster:

```env
BUTCHER_EMPTY_SLEEP=60
BUTCHER_NOT_ENOUGH_SLEEP=60
BUTCHER_CYCLE_SLEEP=15
```

## What To Change For Specific Problems

More chats found:

- Increase `SCOUT_REQUESTS_PER_ACCOUNT`.
- Decrease `SCOUT_JITTER_MIN` / `SCOUT_JITTER_MAX`.
- Use `turbo` for a short run.

More messages read from each chat:

- Increase `MINER_MAX_HISTORY`.

Faster mining:

- Increase `MINER_CHATS_PER_ACCOUNT`.
- Decrease `MINER_JITTER_MIN` / `MINER_JITTER_MAX`.
- Decrease `MINER_ACCOUNT_COOLDOWN`.

Faster insight creation:

- Decrease `BUTCHER_EMPTY_SLEEP`.
- Decrease `BUTCHER_NOT_ENOUGH_SLEEP`.
- Decrease `BUTCHER_CYCLE_SLEEP`.
- Keep `BUTCHER_BATCH_LIMIT` moderate. Bigger batches can hit AI token limits.

Too many rate limits or Telegram pauses:

- Switch to `safe`.
- Increase account cooldowns.
- Increase jitter.
- Add more Telegram session accounts.

## Important

More speed is not always more output. The real bottlenecks are:

- Telegram account limits.
- AI provider rate limits.
- Number and quality of seller chats.
- Too much noise in source chats.

The sweet spot for 24/7 is usually `fast`.
