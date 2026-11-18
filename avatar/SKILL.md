---
name: avatar
description: Turns a script and one avatar image into a talking-avatar video with Gemini, every line on camera, with an optional ElevenLabs voice swap that keeps one voice across every clip. Sets itself up on first use, keeps an avatar library with voice pairs, and joins the clips into one finished video. Use when the user types /avatar or $avatar, or asks to make an AI avatar video, a talking-head video from a photo, or to turn a script into an avatar video. /avatar setup runs setup again.
argument-hint: "[setup]"
allowed-tools: Read, Write, Bash(python3:*), Bash(open:*), Bash(ls:*), Bash(mkdir:*)
---

# /avatar

A script and an avatar image in, one finished talking-avatar video out, on the user's own Gemini key and, if they want it, their own ElevenLabs key.

`S` below is the folder this SKILL.md file is in. Every command runs as `python3 S/scripts/<script>.py ...` with `S` written out as the full path.

The scripts call Gemini and ElevenLabs over the internet and write to `~/.avatar-studio/` and `~/Avatar Videos/`. If a sandbox blocks the network or those folders, ask the user to allow it for these commands, then run them again.

## Rules

- The script's words are never changed, added to, dropped, fixed or reworded. It is saved exactly as the user pasted it.
- Keys are never asked for in the chat, read, printed or written by you. The user pastes them into the key file `setup.py open` opens. If the user pastes a key into the chat anyway, tell them in one line to paste it into the key file instead, and don't use it.
- Nothing renders before the user's yes at the cost step.
- Never judge a take's acting or delivery. Report what the checks found and let the user decide.
- Every question goes at the end of a message, numbered when there is more than one.

## Step 0: setup or run

If the argument is `setup`, run Setup and stop. Otherwise run `setup.py check`. If the Gemini key is missing or rejected, say "Let's get you set up first, it takes about five minutes" and run Setup, then carry on with Step 1.

## Setup

1. Run `setup.py tools`. If anything is missing, show what and the one install line it gives, normally `python3 -m pip install --user imageio-ffmpeg` (it brings its own ffmpeg; nothing else is needed), and ask whether to run it. On the user's yes, run it, then run `setup.py tools` again. If `python3` itself is missing, or running it fails with a developer tools or xcrun error, tell the user to run `xcode-select --install` in their terminal and click Install (macOS's own one-click install, which brings python3), and wait; then run `setup.py tools`.
2. Run `setup.py init`, then `setup.py open`. This opens the key file, `~/.avatar-studio/keys.env`, in a text editor.
3. Connect Gemini. Tell the user:
   - Go to https://aistudio.google.com/apikey and click Create API key.
   - Turn on billing: on the same page, click Set up billing next to the project, and pick a billing account. The avatar model has no free tier.
   - Paste the key after `GEMINI_API_KEY=` in the key file that just opened, save it, and say done.
   Then run `setup.py check`. If the Gemini key reads rejected or missing, say which in one line and repeat this step.
4. Ask: "Want to connect ElevenLabs too? It swaps one consistent voice onto every clip. Without it, Gemini voices each clip itself and the voice can drift from clip to clip." On no, skip to step 6.
5. Connect ElevenLabs. Tell the user:
   - Voice swapping through the API needs a paid ElevenLabs plan (Starter or up).
   - In ElevenLabs, open your profile menu, then API Keys, create a key, and paste it after `ELEVENLABS_API_KEY=` in the key file. Save it and say done.
   Then run `setup.py check` and report the ElevenLabs line. On rejected, repeat this step.
6. Say setup is done, and how to start: `/avatar`, then paste a script.

## Step 1: the script

Ask: "Paste your script." Make the project folder `~/Avatar Videos/<today's date>-<first three words of the script, lowercase, hyphens>/` and write the script into `script.txt` there, exactly as pasted. This folder is `DIR` below.

## Step 2: the avatar

Run `library.py list`. Ask: "Upload an avatar image, or pick one from your library:" followed by the library names, each with its paired voice or "no voice paired". To upload, the user drags the image file into this window, which pastes its path, or types the path.

- A library pick: the image is `~/.avatar-studio/library/<name>/avatar.jpg`.
- A new image: ask what to call this avatar, then run `library.py add <name> <path>`. If it refuses the image (not portrait, or under 720 pixels wide), show its one line and ask for another image.

Then run `run.py image DIR <the avatar.jpg>`.

## Step 3: line by line

Run `run.py plan DIR --script DIR/script.txt --voice native` to see the breakdown. Show the user the numbered line table: one clip a line, at most 30 words a clip, cut at sentence ends. Ask: "This is how your script breaks into clips. Good to go? Any name or word it should say a particular way? Tell me how it sounds, e.g. Nguyen = win." Respellings go to every later `plan` as `--respell '{"written": "said"}'`. They change only what is spoken, never the script.

## Step 4: the voice

Ask: "Would you like to use ElevenLabs to swap in your voice, or stick with the voice as is? ElevenLabs is what keeps the voice consistent across every clip." If the avatar has a paired voice, name it: "<avatar> is paired with <voice name>. Use it, or stick with Gemini's voice?"

- Gemini's voice: the voice is `native`.
- A paired voice: the voice is the output of `library.py show <name>`.
- ElevenLabs and not connected: run Setup step 5, then come back here.
- ElevenLabs, no pair: say "Copy your voice ID from ElevenLabs: My Voices, the three-dot menu next to the voice, Copy voice ID. Paste it here." Then ask: "Create a voice pair with <avatar> and this voice, so it's used every time you pick <avatar>?" On yes, run `library.py pair <name> <voice id>` and use `library.py show <name>`. On no, the voice is `'{"voice_id": "<voice id>"}'`. If pairing reports the voice id isn't found, show its one line and ask for the id again.

## Step 5: the cost

Run `run.py plan DIR --script DIR/script.txt --voice <the voice> [--respell ...]`, then again with `--tries 3` for the worst case. Show the clip count, the first-try estimate and the worst case if every clip takes three tries. Each clip is checked (the words said, no cuts, the same person and room) and redone if it fails. Ask: "Render it? go (up to 3 tries a clip), go 1 (each clip once, cheapest), or stop."

## Step 6: the render

Tell the user it's rendering, three clips at a time, and you'll show them when it's done. Run `run.py render DIR --tries <n>` and wait for it to finish, in the background if you can; it takes a few minutes. Exits:

| Exit | Do |
|---|---|
| 0 | every clip passed: go to Step 7 |
| 1 | some clips are flagged: go to Step 7 and name each flagged clip with the check it failed |
| 2 | bad input: show its one line, fix it, run again |
| 3 | Gemini or ElevenLabs refused (spend cap, quota, key, or plan). Show which in one line and what to fix. When the user says go on, run `run.py recheck DIR --tries <n>`, then `render` again; finished clips are not paid for twice |
| 5 | stopped at a spend limit: say so |

## Step 7: the takes

Show each clip's path in `DIR/clips/`, flagged ones with the check they failed. Ask: "approve, or redo: <clip numbers>?" A redo runs `run.py render DIR --only <clip numbers> --tries <n>` the same way, then this step again.

## Step 8: the finished video

Run `run.py stitch DIR`. Tell the user where `final.mp4` is, that the single clips are in `clips/`, and what the render cost from the plan's numbers. Then end.
