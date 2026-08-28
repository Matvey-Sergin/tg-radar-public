# IT Radar Quality

## Source quality

`scout.py` rejects broadcast channels, job/news/promo/crypto sources, dead chats, bot-heavy histories and weak discussions. `Chat Quality Score` uses freshness, substantive-message share, unique authors, replies, questions, IT context and bot ratio. Metadata and score details are stored in `target_chats`.

## Pain quality

A pain is an obstacle to a real goal, not merely negative sentiment or any question. The miner combines semantic LLM extraction with deterministic guardrails.

Strong evidence includes recurring manual work, time/money cost, failed tools, a homemade workaround, solution seeking and explicit payment intent. Basic syntax questions, homework, one-off bugs, beginner learning questions, ads, vacancies, generic opinions and unsupported feature requests are rejected.

Unknown values stay unknown. The source message is always stored alongside the structured signal.

## Cluster quality

A cluster must be concrete, semantically coherent and supported by at least two known independent affected authors. LLM-proposed outliers are pruned; a mixed cluster is rejected if fewer than two coherent sources remain. Counts are reported separately as unique authors, unique chats and total messages.

`Gold` additionally requires independent confirmation across at least two chats. A one-chat cluster can remain `Silver` and receives a lower Reachability score.

## Ranking

Pain Score includes frequency, severity, time/money cost, urgency, existing effort, WTP, repeatability, solvability and reachability. Lead Abundance uses chats, independent authors, recent signal flow, source quality and intent density. First Money Score combines pain, WTP, reachability, freshness, independent evidence and whether a manual result can be delivered within 48 hours.

The scoring never assumes AI, SaaS, a bot or automation as the solution.
