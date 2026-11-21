# OpenRouter

Mapping and the three disc decisions call the OpenRouter HTTP API (`ripknife.llm`, `OPENROUTER_CHAT_URL`). The model has no shell.

Read `.env.example` and `ripknife/llm.py` before changing a model id. The names below match that code. The id for each call comes from the env vars in `.env.example`. A per-call variable wins, then `RIPKNIFE_LLM_MODEL`, then the default named in `.env.example` and `DEFAULT_MODEL` in `ripknife/llm.py`.

The key is `OPENROUTER_API_KEY` in the gitignored repo `.env` (the value is the key, with no Bearer prefix). systemd loads that file with `EnvironmentFile`. `map_agent` merges `.env` on each map, so a new map can see a key without a unit restart. Do not restart a unit mid-rip. See [agent.md](agent.md) and [scripts/systemd/README.md](../scripts/systemd/README.md).

A missing or blank key does not call OpenRouter. The map call returns stop and does not write a manifest. Each decision call returns `stop`. An action outside the allowed list is `stop`.

## Calls

| Call | Code | Env var | Role |
|------|------|---------|------|
| Map | `request_map` (`call="map"`) | `RIPKNIFE_LLM_MAP_MODEL` | Manifest JSON for the inserted disc |
| Failure classification | `classify_disc_failure` (`call="failure"`) | `RIPKNIFE_LLM_FAILURE_MODEL` | After the normal rip fails, when no jacket image is attached |
| Vision | `classify_disc_failure` (`call="vision"`) | `RIPKNIFE_LLM_VISION_MODEL` | The same failure decision, only when the volume label is a placeholder and jacket bytes are attached |
| Gap | `choose_gap_action` (`call="gap"`) | `RIPKNIFE_LLM_GAP_MODEL` | Drive loop stopped with bytes still missing |
| Verify | `choose_verify_action` (`call="verify"`) | `RIPKNIFE_LLM_VERIFY_MODEL` | Verify failed, or gaps remain |

`RIPKNIFE_LLM_MODEL` overrides any call that does not set its own variable.

Vision runs only for a placeholder volume label. `label_needs_jacket` is true for a generic label (`is_generic_disc_name`) and for the `UNKOWN` typo. Jacket types are jpeg, png, webp, and gif. Any other label keeps the failure call on `RIPKNIFE_LLM_FAILURE_MODEL` and sends no image.

Calls use strict JSON, temperature 0, and reasoning effort `none`. The ladder, speeds, retries, filenames, and library copy stay in code. The ladder itself does not call the model. Watchers do not loop these calls. At most three decision calls, none inside the byte loop. See [recovery.md](recovery.md).

## Map

The map call returns one JSON object:

- `action`: `write`, `stop`, or `ambiguous`
- `manifest`: one `tv_disc` or `movie` object when the action is `write`, otherwise `null`

Python validates the JSON against the scan and writes `staging/ready/<id>.yaml`. The model does not write the file. `ambiguous` and `stop` do not write a manifest.

Invalid JSON, an unknown action, or a missing key logs `=== ALERT === MAP_AGENT_AUTH` or `MAP_FAILED` on `pipeline.log`, `error.log`, and `map.log`, and sets watcher-state `last_error`. `make watch-status` prints that line. Put the key in `.env`, or hand-write `staging/ready/<disc-id>.yaml`. A similar-season manifest is a template only. It is not copied. Auth gets one retry after the `.env` merge.

The map prompt is `skills/map/SKILL.md`, loaded by `build_map_prompt`. Failure, gap, and verify prompts are `skills/failure/SKILL.md`, `skills/gap/SKILL.md`, and `skills/verify/SKILL.md`. An ambiguous disc is JSON `action` `ambiguous` and `manifest` null. Python writes a file only for `write`.

## Decision actions

Failure (`classify_disc_failure`): `bad_sectors`, `wrong_title`, `empty_slot`, `ambiguous_map`, `tooling`, `stop`. `bad_sectors` is the only action that may start an image. The others do not.

Gap (`choose_gap_action`): `another_disc` (another physical copy you own), `another_drive` (a different-brand drive), `accept_gap`, `stop`. Another speed is not an action.

Verify (`choose_verify_action`): `keep` (hold the file for review), `discard`, `stop`. A library copy is not an action.
