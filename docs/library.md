# Library

Library roots come from `nas.share` on the manifest, then from `site.env`, then from these placeholders. Helpers: `ripknife.nas.classify_library` and `nas_share_for`.

| Content | Destination | Manifest |
|---------|-------------|----------|
| Cartoon or animation **series** (Western, Japanese, CG) | `/path/to/library/Anime/<Show>/Season N/` | `kind: tv_disc`, `nas.share` set to that show |
| Animated **movies** | `/path/to/library/Anime/Movies/` | `kind: movie`, `nas.movies: true`, `nas.share` set to `Anime/Movies` |
| Live-action TV | `/path/to/library/Shows/<Show>/Season N/` | `nas.share` set to that show |
| Live-action movies | `/path/to/library/Movies/` | `nas.share` set to the Movies root |

Any cartoon or animation uses the Anime library, including English-language cartoons. Live-action TV is the only content that uses `Shows/`.

Animated features need their own Movies-type library folder. A TV-type Anime library ignores feature files that sit at the Anime root.

`RIPKNIFE_ANIME_NAS`, `RIPKNIFE_SHOWS_NAS`, and `RIPKNIFE_MOVIES_NAS` are the three roots. `RIPKNIFE_NAS` is the default TV share when `nas.share` is omitted.

Do not move a series that is already in the library. These paths are for new mapping and rip output.

Do not copy a file that failed duration, size, or decode checks. A gapped file stays out of the library. See [recovery.md](recovery.md).

YouTube downloads use the same four roles. See [pipeline.md](pipeline.md#youtube).
