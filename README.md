# IT Pain Radar

IT Pain Radar — evidence-first pipeline для поиска повторяющихся проблем русскоязычных IT-специалистов в Telegram.

Система не генерирует «идеи для стартапов» из воздуха. Единица доказательства — исходное сообщение конкретного затронутого человека. Несколько сообщений одного автора не считаются несколькими независимыми подтверждениями.

## Что делает pipeline

```text
IT chat discovery
→ Chat Quality Score
→ message collection
→ semantic pain candidate detection
→ false-pain filtering
→ structured PainSignal
→ concrete pain clustering
→ Pain/WTP/Reachability scoring
→ Lead Abundance
→ First Money ranking
→ Telegram UI + Markdown report
```

Фокус аудитории: backend/frontend/fullstack, Python/Java/Go/Node.js/.NET/React, DevOps/SRE, QA, data/ML/AI, mobile, системные администраторы, tech/team leads, engineering managers и архитекторы. Дополнительно анализируются профессиональные ситуации: remote/relocation, поиск работы, собеседования, freelancing, CI/CD, observability, debugging, legacy, документация и developer productivity.

## Быстрый старт

1. Скопируйте `.env.example` в `.env` и заполните Telegram/Groq credentials.
2. Если нет `SESSION_STRING`, создайте его командой `python login.py`.
3. Установите зависимости: `python -m pip install -r requirements.txt`.
4. Запустите постоянный режим: `python nexus.py`.

`RADAR_MODE=it` используется по умолчанию. Nexus поднимает:

- `scout.py` — ищет и оценивает профессиональные IT-чаты;
- `miner.py` — хранит сообщения и извлекает структурированные PainSignal;
- `butcher.py` — объединяет только совместимые свидетельства минимум двух известных авторов;
- `tg_bot.py` — показывает First Money ranking и первичные цитаты.

Новая база по умолчанию: `data/it_radar.db`. Старая `scout.db` с marketplace-данными не смешивается с IT-результатами.

## Один измеримый end-to-end проход

```powershell
python it_pipeline.py --max-keywords 2 --max-chats 2 --output reports\it_first_money.md
```

Команда выполняет ограниченный discovery/collection pass, кластеризацию и создаёт отчёт. Для отдельных стадий:

```powershell
python scout.py --once --max-keywords 3
python miner.py --once --max-chats 2
python butcher.py --once
python it_report.py --limit 10 --output reports\it_first_money.md
```

Первый запуск обычно даёт много одиночных сигналов. Они получают `LowSignal` и ждут независимого совпадения при следующих проходах; радар не превращает один вопрос в «рынок».

## PainSignal

Для сильного сообщения сохраняются: исходный текст и дата, чат, анонимный ID автора, сегмент, summary, desired outcome, workaround, frequency, time/money cost, urgency, emotional intensity, repeatability, manual work/tool failure/solution seeking/explicit WTP, категория, evidence quotes, confidence и Pain Score.

Неизвестные значения остаются `null/unknown`. Детектор использует ключевые маркеры как retrieval signal, но решение принимает семантически. Разовые syntax-вопросы, домашние задания, реклама, вакансии, новости, beginner-вопросы и неподтверждённые feature requests отбрасываются.

## First Money report

Для каждого кластера отчёт показывает:

- Pain Score, WTP, Reachability/Lead Abundance и First Money Score;
- independent users, unique chats, messages, recent signals и trend;
- ICP, текущий workaround и первичные цитаты;
- аргументы «за» и «против» бизнес-боли;
- manual MVP, оффер на 48 часов и price hypothesis;
- источники первых 10 и следующих 50 пользователей;
- главный риск и следующий validation experiment.

Рейтинг не предполагает заранее AI, SaaS, Telegram-бота или автоматизацию. Сначала идут сообщение → боль → кластер → проверка; ручной сервис считается нормальным результатом.

## Legacy marketplace mode

Старый LeadRadar сохранён только для обратной совместимости и запускается явно:

```env
RADAR_MODE=margin
DB_PATH=C:\path\to\legacy\scout.db
```

IT-режим не импортирует его правила, категории или scoring assumptions.

## Проверка

```powershell
python -m unittest discover -s tests -v
python -m compileall -q .
```

Тесты покрывают scoring, ложные боли, анонимизацию, тренд, семантическую связность кластера, защиту от склейки разных проблем, минимум независимых авторов и сохранение First Money полей.
