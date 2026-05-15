# Claude Handoff

## Project

Message QA Local for sales / customer-service chat quality review.

Current scope:

- Local rule-based scoring engine
- Local training/coaching recommendations
- Local team scorecards
- Local viewer dashboard
- Live Nhanh Vpage conversation review

Out of scope:

- No production auth
- No database
- No external AI/LLM in app logic
- No background jobs

## Run

```powershell
python -m unittest discover -s tests -v
python -m app.viewer
```

Viewer URLs:

- `http://127.0.0.1:8080/dashboard` for conversation review
- `http://127.0.0.1:8080/dashboard/team` for team coaching

## Main files

- `app/schemas.py`: core data models
- `app/rule_engine.py`: deterministic rule evaluation
- `app/evaluator.py`: full conversation scoring
- `app/training.py`: map failures to skill gaps and training
- `app/employee_scorecard.py`: employee-level aggregation
- `app/nhanh_client.py`: live Nhanh API helper
- `app/nhanh_adapter.py`: map Nhanh payloads into internal conversation schema
- `app/viewer.py`: local dashboard / live review
- `data/rules.json`: scoring + blacklist rules
- `data/training_modules.json`: local training content
- `data/sample_conversations.json`: sample offline data

## Local config

Live Nhanh config is expected in:

- `data/nhanh_config.local.json`

This file is intentionally excluded from the handoff zip because it may contain:

- `access_token`
- `business_id`
- `secret_key`

Use `data/nhanh_config.example.json` as the template.

## Current product shape

### Conversation Review page

- Shows recent Nhanh conversations
- Each card now includes:
  - customer name
  - conversation id
  - updated time
  - replied / unreplied
  - phone available or not
  - last message preview
  - QA summary:
    - total score
    - grade
    - failed rule count
    - blacklist count / status
- Clicking `View tin nhan` opens one live conversation and scores it

### Team Coaching page

- Employee scorecards
- Top employees needing training
- Weakest skills
- Training recommendations

## Important recent fixes

### 1. False positive on hair-photo rules

The system originally only recognized explicit phrases like:

- `gửi ảnh tóc hiện tại`
- `gửi ảnh tóc`

It was updated to also recognize real operator phrasing like:

- `chụp tóc qua cam`
- `chụp tóc qua cam thường`
- `gửi ảnh qua cam thường`

Also, the engine now treats the case as valid when:

- the customer already sent a hair image before advice started

This affects:

- `request_current_hair_photo`
- `no_current_hair_photo`

Relevant files:

- `data/rules.json`
- `app/rule_engine.py`
- `tests/test_rule_engine.py`

### 2. Viewer UX split

The dashboard was split into:

- `/dashboard`
- `/dashboard/team`

to avoid mixing transcript review and coaching analytics.

## Known concerns / review targets

Please review these areas carefully:

1. Rule correctness vs real operator language
2. Blacklist strictness, especially:
   - unanswered customer
   - missing current hair photo
   - product misinformation
3. Sequence logic:
   - what counts as "advice started"
   - what counts as "photo already available"
4. Nhanh mapping quality:
   - sender role inference
   - attachment mapping
   - order of messages
5. Viewer UX:
   - whether current live review flow is understandable
   - whether explanation/debug output is sufficient
6. Architecture:
   - whether rule definitions should stay in one JSON
   - whether rule-specific debugging should be first-class

## Questions for deep review

1. Is the current rule engine too keyword-fragile for real chat behavior?
2. Which rules should be reworked from keyword matching into structural heuristics?
3. Which blacklists are too aggressive and likely to create false positives?
4. How should "customer already provided evidence" be modeled more explicitly?
5. Should the viewer expose per-rule debug evidence by default?
6. What is the best way to evolve from local deterministic QA into a more production-ready review workflow without losing auditability?

## Test status

Current baseline:

- `python -m unittest discover -s tests -v`
- Passed: 30 tests

## Notes

- This handoff package is intended for architecture / QA / product review.
- It is sanitized for sharing with another model.
