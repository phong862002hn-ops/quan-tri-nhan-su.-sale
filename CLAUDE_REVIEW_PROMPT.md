Use this repo as a product and code review target.

What I need from you:

1. Read the app architecture and data flow end-to-end.
2. Review the rule engine critically, especially false positives / false negatives.
3. Review the Nhanh live-review integration and whether the mapping is reliable.
4. Review the UX of the local viewer:
   - conversation review
   - live review
   - team coaching
5. Tell me what should be redesigned before this becomes a serious internal QA tool.

Please focus on:

- rule quality
- edge cases
- maintainability
- product clarity
- operator trust in the score

Important domain example:

- In hair consultation, the system previously mis-flagged a conversation as "did not request current hair photo" even though:
  - the operator asked the customer to send / capture current hair via camera wording
  - or the customer had already sent the hair photo before advice

Please audit whether the current fix is sufficient or still fragile.

Deliverable format:

1. Biggest product risks
2. Biggest technical risks
3. Rules likely to cause wrong scoring
4. Recommended architecture changes
5. Recommended UX changes
6. Suggested next implementation steps in priority order
