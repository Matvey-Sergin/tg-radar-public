# Project Structure

```text
PainSearchRadar/
  nexus.py               # 24/7 supervisor; IT mode is default
  scout.py               # IT chat discovery + Chat Quality Score
  miner.py               # collection + structured PainSignal extraction
  butcher.py             # concrete clustering + independent-author guardrails
  it_radar_rules.py      # IT ontology, search queries and deterministic scoring
  quality_rules.py       # chat, noise and false-pain filters
  it_report.py           # Markdown First Money ranking
  it_pipeline.py         # bounded end-to-end pass
  tg_bot.py              # First Money UI and primary evidence viewer

  database.py            # additive SQLite schema/migrations
  account_manager.py     # reusable Telegram account rotation
  ai_engine.py           # reusable LLM client
  runtime_settings.py    # speed profiles
  text_encoding.py       # defensive Cyrillic JSON repair

  margin_leak_radar.py   # opt-in legacy marketplace mode
  lead_radar_rules.py    # legacy-only rules
  margin_radar_policy.py # legacy-only polling policy
```

Universal Telegram, storage, LLM, logging and supervision components remain shared. Marketplace prompts, search terms, ontology, filters, scores and reports are isolated from the default IT pipeline.
