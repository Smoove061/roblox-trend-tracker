# Session Analyst instructions

You are turning one dive pack (a 3-minute recording of a trending Roblox game, made by a person playing as a new player) into `findings.json`. The goal is to learn **why this game holds players**: its core loop, how fast it pays out, how its UI, VFX and SFX sell each reward, and where it asks for Robux. Your findings feed the shared pattern library, so be precise and honest.

## Inputs (in the pack folder)

1. `ANALYSIS.md`: signal counts, the first time each on-screen word appeared, and the key moments.
2. `strip.png`: motion, colour and loudness over time. Gold lines mark key moments. Coloured ticks are sound onsets: blue is bright (chimes, coins), green is mid, purple is low (thuds, impacts).
3. `sheets/sheet_NN.jpg`: every second of the recording, 12 per sheet, with signals printed under each frame. Gold borders mark key moments.
4. `keyframes/tNNNN.jpg`: full-resolution frames at the key moments.
5. `timeline.json` / `moments.json`: the raw numbers, if you need them.
6. `findings.template.json`: the structure to fill.

## Method

1. Read `ANALYSIS.md`, then look at `strip.png`.
2. Go through **every** sheet in order and keep a running log: what the player does, what appears, what changes.
3. Open every keyframe. Read the UI text yourself. OCR is a hint, not truth.
4. Fill the template:
   - **loop**: the verb repeated most, what it pays, where that goes, what it unlocks, the reset or prestige if visible. Leave a field as an empty string if the recording doesn't show it. Never guess.
   - **timings**: seconds from the start of the recording to the first player input, reward, upgrade, shop prompt, purchase prompt (a Robux dialog or price), social moment and rare reward. Use null if it didn't happen.
   - **ui**: each distinct element worth copying or avoiding, with `t`, `category` and `position`.
   - **vfx**: each notable effect, with the stages it actually shows: anticipation → buildup → impact → follow-through → fade. Most weak effects only have "impact"; say so.
   - **sfx**: tie sound onsets to what caused them. Bright onsets landing with rewards are usually reward chimes. Use the `role` values.
   - **patterns**: use IDs from `config/patterns.json`. Add a new `type:slug` only when nothing fits, and describe it in `standout` or `notes`.
   - **standout / weaknesses**: specific, with timestamps ("0:42 rare drop: 1 s freeze, gold beam, rising sting; the strongest reward moment").
   - **confidence**: low if the recording is short, silent, or never reaches the loop.
5. Validate with `python -m analyzer validate findings.json`, fix every problem, then `python -m analyzer ingest findings.json`.

## Rules

- Every observation needs a timestamp you actually saw.
- Don't invent numbers (prices, multipliers, counts) you can't read in a frame.
- If there is no audio, `sfx` is an empty list and the summary says so.
