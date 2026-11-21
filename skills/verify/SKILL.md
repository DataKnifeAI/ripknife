---
name: verify
description: >-
  Chooses whether to keep or discard a file after verify fails or gaps remain.
  Loaded by ripknife.llm.choose_verify_action. A library copy is not an action.
---

Verify failed or the file still has gaps. Return JSON {"action":"<allowed>"} and nothing else.

keep holds the file for review. discard drops it. Never a NAS copy. Not another speed.

The allowed action names and the situation follow.
