---
name: map
description: >-
  Maps one inserted disc scan to a JSON manifest with action write, stop, or
  ambiguous. Loaded by ripknife.map_agent.build_map_prompt for the OpenRouter
  map call. Use when changing title identification, track selection, or the
  ambiguous-reply contract.
---

Return one JSON object and nothing else. You have no shell and cannot write files.
Python validates the JSON and writes the manifest. Do not run commands.

Inputs (paths are for operators; the scan body is included with the request):
- Scan JSON, pending meta, the registry path, the disc id, and the ready-manifest path are in the disc context appended after this instruction.
- Pending meta carries title_count, obfuscation, recommended_title, decoy_indices, and identification.
- Python writes staging/ready/<identified-slug>.yaml. Do not write the file yourself.

Naming (critical):
- meta.disc_name = exact scan disc_name (may be a generic/uninformative volume label) — used only for disc matching
- meta.id / meta.title / output.filename / ready YAML basename = the IDENTIFIED work title, NOT the disc label
  Examples: meta.title "Example Movie (1983)", id example-movie-1983, filename "Example Movie (1983).mkv"
  Bad: using uninformative volume labels (e.g. DVD_VIDEO, DVDVolume, VIDEO_TS, LOGICAL_VOLUME_ID, untitled) as the movie/show name
- Plex editions (movies): ALWAYS set output.edition for Extended Edition, Director's Cut,
  Unrated, Special Edition, Theatrical, Final Cut, or any other non-default cut
  (single-disc AND multi-disc). Pipeline turns Title (Year).mkv into
  Title (Year) {edition-<label>}.mkv
  Do NOT put edition words in meta.title; do NOT write {edition- into output.filename
  Do NOT use a plain suffix like "Title (Year) Extended Edition.mkv" on NAS/stitch files
  Strip colons from meta.title / filenames (Plex-safe)
  https://support.plex.tv/articles/multiple-editions/
- Generic means an uninformative volume label detected by heuristic (patterns + boilerplate tokens), not an exhaustive string list.
- Generic volume labels (LOGICAL_VOLUME_ID, BDROM, DVD_VIDEO, DVDVolume, VIDEO_TS, BD_VIDEO, untitled, and any other uninformative label) are not the work title.
- Strong IDs, in order:
  - identification.fingerprint_title — authoritative known-disc match (duration + feature size and/or AACS contentID). Use as meta.title. Do not override it.
  - identification.bluray_title (BDMV/META/DL/bdmt_*.xml) — strong; prefer as meta.title when no fingerprint is set.
- identification.aacs_content_id and chapter/extra lengths are fingerprint keys, not titles. They name a film only when a known fingerprint already matched.
- identification.candidates[] are a runtime shortlist, not a pick. Runtime, extra or chapter lengths, and a studio year or disc stamp are not an identification.
- When two well-known films share the feature runtime and you have no fingerprint, bdmt title, or explicit user ID: do not pick one and do not write YAML. Return JSON with action ambiguous and manifest null.
  Example: North Film (1987) and South Film (2006) can share ~1:49:32. Extras such as a short unused-scenes clip and a trailer on a LOGICAL_VOLUME_ID disc do not choose between them. The pressing is North Film only when identification.fingerprint_title says so.
- If identification.ambiguous is true, the collision is already known. Return JSON with action ambiguous and manifest null. Do not write YAML.
- meta.title_candidates / meta.preferred_id / meta.identified_title are hints. Ignore them when they contradict a fingerprint or when the disc is ambiguous.
- If still unknown after hints + research, leave pending and do NOT invent a placeholder title. Do not write YAML.

If you identified a better slug than the disc id, set meta.id to that identified slug. Python writes staging/ready/<identified-slug>.yaml.
Do NOT write directly to configs/ — Process 2 rips from staging/ready. Do not write the file yourself.

Title research steps (do these in order):
1. Parse scan JSON: disc_name, titles[], obfuscation block (if any), identification block (if any). Read meta.json.
2. Identify the work: TV show+season+disc OR movie Title (Year). Do not treat disc_name as the title when it is generic.
3. If identification.ambiguous is true, or you find two well-known films at this runtime without a fingerprint, bdmt title, or user ID: return action ambiguous and manifest null. Do not write YAML. Else if identification.fingerprint_title is set, use it (authoritative). Else if identification.bluray_title is set, use it (with year from candidates/web if needed). A single runtime candidate is a hint only — do not pick it when another well-known film shares that runtime. Never override a fingerprint with extras, chapter lengths, or a studio stamp.
4. If title_count > 50: treat as likely Blu-ray playlist obfuscation — read duplicate_count, dominant_duration_sec, recommended_title, decoy_indices.
5. If obfuscation.detected or duplicate_count >= 5: for movies use title_selection.mode auto_feature (or fixed at recommended_title); add every decoy_indices entry to skips[] with reason "decoy playlist (duration outlier)".
6. DVD / TV scene-jump risk: skip title 0 play-all; if early indices are suspicious (COPYRIGHT/FBI/WARNING, short duration, Notes-style flags), check later titles (N+1, N+2). Use split for 2-chapter ~20min titles; extract_chapter for play-all-only episodes.
7. Cross-reference the registry path in the disc context and completed manifests in the config directory for the same show — match episode numbering and makemkv index style (TINFO vs MSG).
   MakeMKV title index is NOT episode order — do not number 09, 10, … from sequential t9, t10. Pair by playlist/title name, duration, chapter cards, and known episode lists (TheTVDB DVD order), then set makemkv_title to the matching scan index.
8. Set meta.id to identified slug, meta.disc_name from scan (exact), meta.minlength from meta.json (default 300).
9. Document title choice rationale in a YAML comment above title_selection or tracks (e.g. why recommended_title, which decoys skipped, how the movie was identified).
10. Return JSON only. Python validates it against the scan and writes staging/ready/. Do not run a command.

TV episode order (critical — title index ≠ episode number):
- MakeMKV title index is NOT episode order. Do not assign episode 09 because the title is t9.
  Bad: titles t9–t16 → files 1x09–1x16 in scan order
- Pair each title to an episode using playlist/title name, duration, chapter cards,
  box art, and known lists (TheTVDB DVD order, prior manifests in the config directory).
  Then set tracks[].makemkv_title to the matching scan index.

Theatrical shorts compilations (critical — not a TV broadcast season):
- Anthology / theatrical-shorts discs: kind tv_disc, Anime library
- Use a unique show name so Plex does not bind a giant catalog series with a different order.
  Filename prefix = show name.
  nas.share: /path/to/library/Anime/<Collection>
  output.season_dir: <Collection>/Season N
- Set plex.local: true and plex.year to the collection year.
  Pipeline writes show-folder .plexmatch (title + year only; no tvdbid/tmdbid/guid).
  Plex still needs Unmatch / Fix Match if it already bound the catalog series.
- Do not rename a library folder while a disc is still copying. .plexmatch moves with the folder.
- Season N = collection volume N. Not "the show's Season 1".
- Episode numbers continue across discs of the same volume (Vol 1 D1 = 01–25, D2 = 26–50).
  Next volume starts the next season. Do not restart 01 on every disc.
- Official disc order, not a catalog airdate or character-grouped DVD order
- Theatrical shorts are ~6–8 min: set meta.min_episode_sec: 360 (default 480 rejects real shorts)
- Skip title 0 play-all; rip individual shorts as Plex TV files (Show - 1x01 - Title.mkv)
- Flow-mapping tracks: quote any title that contains a comma (or :, {, }).
  Bad: { title: Zip, Zap } → YAML parses title as "Zip"
  Good: { title: "Zip, Zap" }

extract_chapter semantics (critical — common off-by-one):
- chapter_index / chapter_end are 0-based ffprobe indices, BOTH inclusive
- With N chapters, valid indices are 0..N-1. chapter_end must be <= N-1, NEVER N
  Bad: 13 chapters → chapter_end: 13  |  Good: chapter_end: 12 (last chapter)
- Do not treat chapter_end as exclusive or as a chapter count
- Ranges on the same makemkv_title must not overlap

NAS routing (critical — cartoon/animation always Anime, never Shows/Movies):
- Any cartoon OR animation (Western cartoons, Japanese anime, CG/animated series) → Anime share
  kind: tv_disc
  nas.share: /path/to/library/Anime/<Show>
  nas.subdir: Season N
  Example Cartoon → /path/to/library/Anime/Example Cartoon  (NOT Shows)
- Live-action TV only → Shows share
  nas.share: /path/to/library/Shows/<Show>
- Animated movies / cartoon features → Anime/Movies, NOT the live-action Movies root and NOT Anime root
  kind: movie; nas.movies: true
  nas.share: /path/to/library/Anime/Movies
  The Anime library is TV-type (Show/Season N/) and ignores feature files at the Anime root.
  Features need a separate Movies-type library whose source is Anime/Movies.
- Live-action movies → /path/to/library/Movies
- English-language cartoons still use the Anime library (stream_defaults may be eng; path is Anime)
- Do not relocate already-copied TV series; this rule is for new mapping/rip output only

Manifest requirements:
- kind tv_disc or movie as appropriate
- Map episodes using TheTVDB DVD order (TV) or official order (live-action); movies use title_selection + validation floors
- Do not treat sequential MakeMKV title numbers as episode numbers (see TV episode order above)
- skips[] for play-all, extras, decoy playlists, skipped angles, suspicious loops
- Each tracks[].makemkv_title must exist in scan, meet minlength, and match expected duration (~7 min theatrical shorts, ~11 min TV cartoons, ~47 min drama, ~23 min anime)
- Always set stream_defaults (required for correct player audio/subs):
  - English-language movies / live-action TV / English cartoons: keep_all_tracks true, audio_default eng, omit subtitle_default
  - Japanese anime: keep_all_tracks true, audio_default jpn, subtitle_default eng
  - Foreign-language film: audio_default = original language; add subtitle_default eng when English subs should default on
  - Do NOT omit stream_defaults on movies — discs often mark non-English audio as default
- Match existing manifest style (stream_defaults, nas, post blocks)
- **Size floors (`title_selection.min_size_bytes`, `validation.min_size_bytes`):**
  - Defaults are 1 GiB — enough to catch empty rips, not reject real DVDs (~2–5 GB).
  - Do NOT copy 5368709120 (5 GiB) unless the scan feature is genuinely large (BD/UHD).
  - Prefer omitting min_size_bytes (use defaults) or set ~90% of scan `size_bytes` for the feature title.
  - DVDs and SD features are often 2–4 GB; many successful rips in this library are under 5 GB.
- **Multi-disc movies** (DSC1/DSC2, feature split across discs, user hint):
  - One manifest per disc; ``meta.part: N`` (1-based); ``output.filename: Title (Year) - Disc N.mkv``
  - ``post.copy_to_nas: false`` on every part (masters stay on game drive)
  - ``stitch`` block on every part (same values): ``enabled: true``, ``group_id: title-year``, ``expected_parts: N``, ``output_filename: Title (Year).mkv`` (no ``- Disc N``, no plain edition suffix), ``copy_to_nas: true``
  - ALWAYS set ``output.edition`` for Extended/DC/Unrated/Special Edition (and similar). Edition goes on BOTH disc masters AND the stitched NAS file:
    ``Title (Year) {edition-X} - Disc N.mkv`` locally; stitch ``Title (Year) {edition-X}.mkv`` (no ``- Disc N``)
  - Rip watcher stitches automatically after all N part MKVs exist (post-rip, no makemkv lock)
JSON object:
- keys action and manifest only
- action is write, stop, or ambiguous
- write: manifest is one tv_disc or movie object
- stop or ambiguous: manifest is null
- ambiguous: do not guess a title
