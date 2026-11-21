---
name: failure
description: >-
  Classifies a failed disc rip as bad sectors, a wrong title, an empty slot,
  an ambiguous map, or a tooling problem. Loaded by
  ripknife.llm.classify_disc_failure for the OpenRouter failure and vision calls.
---

Choose one action for this disc failure. Return JSON {"action":"<allowed>"} and nothing else.

bad_sectors means damaged sectors. wrong_title, empty_slot, ambiguous_map, and tooling are not a sector image.

Do not choose a speed, a filename, a playlist, or a NAS copy.

Jacket art is attached to this message only when the volume label is a placeholder. If no image is attached, ignore jacket art.

The allowed action names, the volume label, and the log follow.
