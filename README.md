# Avatar Studio

Turn a script and one photo into a talking-avatar video, every line on camera, from inside Claude Code.

Paste your script, upload your avatar, and it breaks the script into clips line by line, renders each one with Google's Gemini, checks every clip (the right words, no jump cuts, the same person and room), redoes the ones that fail, and joins them into one finished video. Connect ElevenLabs and it swaps one voice onto every clip, so your avatar sounds the same from the first line to the last.

## Install

**The easy way:** paste this into Claude Code:

> Install this skill for me: https://github.com/jackjamgo/avatar-studio

**By hand:** copy the `avatar` folder into `~/.claude/skills/`.

**Using Codex?** Paste the same line into Codex, and approve network access when it asks.

Then type `/avatar` (in Codex, `$avatar`). The first time, it walks you through setup.

## What you need

- A Mac.
- A Gemini API key with billing turned on. The avatar model has no free tier. Setup shows you where to get it.
- Optional: an ElevenLabs API key on a paid plan, for the voice swap.

Your keys stay in a file on your computer, `~/.avatar-studio/keys.env`. They never go into the chat.

## Use it

1. Type `/avatar` and paste your script.
2. Upload an avatar image, or pick one from your library.
3. Check how your script breaks into clips.
4. Choose your ElevenLabs voice or Gemini's own. Pair a voice with an avatar once and it's used every time.
5. See the cost, say go, and get `final.mp4` plus every single clip.

It shows you the cost before anything renders, and nothing renders until you say go. Gemini bills about $0.10 a second of video, so a 30-second script runs about $3, more if clips need redoing.

## What's inside

- `avatar/SKILL.md`: the steps your agent follows.
- `avatar/scripts/`: the render, the clip checks, the voice swap, the avatar library and setup.
