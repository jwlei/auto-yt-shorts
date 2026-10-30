# Rejected videos

Videos judged bad after watching them, kept to compare against. Moved here by hand;
nothing in the pipeline reads this folder. Each row records what the check or fix
was calibrated on. Scores use the definitions in `modules/lip_check.py`, measured on
the finished video, each on its worst second.

| Video | Where | Problem | Measured | Normal | What changed |
|---|---|---|---|---|---|
| `short_20261030_155503` | 00:38 | Wide, bulging, startled eyes | eye wide 0.22 | 0.10 and below | Prompt asks for relaxed eyes; `MAX_STARE` re-renders the clip |
| `short_20261030_162716` | 00:10 | Mouth frozen half open while the voice carries on | lip gap spread 0.001 | 0.009 and up | Check now scores the worst second, not the whole clip (`MIN_MOUTH_MOVEMENT`) |
| `short_20261030_172800` | 00:06 | Poor lip-sync: mouth moves but doesn't match; she winks and tilts her head | 0.015, no different from clips that pass | | **No check catches this.** Removed the gestures and camera moves that turn her head or invite a wink |
| `short_20261030_183019` | across cuts | Word endings and starts cut off | Pauses of 0.25-0.43s left in the mix, under the 0.5s crossfade | | Scenes get silence up to the crossfade length; next scene no longer fades in (`composer.py`) |
| `short_20261030_192844` | throughout | Eye colour changes: blue-grey, dark at 00:12-00:15 | iris colour shift 37-43 | 34 and below | Clips are compared with the reference image's iris colour; prompt asks for it to hold |
| `short_20261030_215411` | 00:14-00:47 | Stock shots repeat: a walking man three times across two scenes (00:14, 00:22, 00:24), a machine and a face each restart mid-shot (00:37, 00:44) | 32% of the video repeats earlier frames | 17% and below | A video uses each stock clip once; a scene with one clip plays it through instead of restarting it; clips no longer loop (`asset_manager.py`, `composer.py`) |

The workflow runs at `cfg: 1.0`, which ignores its negative prompt: listing an
unwanted expression there does nothing, so it has to go in the positive prompt.
