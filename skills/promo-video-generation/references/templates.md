# Prompt templates — place promo video

Principles: ① live-action images first, then video ② logos only where they really are ③ the small imperfections of reality (no overdone HDR / plastic gloss) ④ one continuous music track 0→20s, no human voices.

## Image prompt (for Codex image_gen)

```
Using the attached real photo, create a 16:9 live-action advertising photograph as if shot on a cinema camera at this place.
Main subject: [space / product / people / activity]; the moment to convey: [meaning and action].
Keep [architecture, layout, objects] exactly as in the original. Do not crop the tops or bottoms of towers, buildings, or sculptures.
Composition and camera height: [composition]; light: [real light source and direction].
If people appear: [specific action, outfit, count] at full-body distance, matching the space's perspective; faces and eyes toward [target].
Keep skin and material texture, natural reflections, restrained sharpness.
Visible text: reproduce [verified text] exactly. Logo / signage appears at [position], near-frontal, crisp down to small letters. Do not carry over watermarks or credit marks.
It must look like a photo with the small imperfections of reality — do not substitute overdone HDR or plastic gloss.
```

## Video prompt (for Seedance 2.5 R2V)

```
A cinematic live-action promo video introducing [place]. 16:9, 20 seconds.
Story: [one-line story].
Direction: [protagonist, mood, rhythm, why cuts connect].
References: [image numbers and the real space/object each shows].
Cut list (one row per cut):
[cut · start–end s] | [image ref] | [subject · action · expression, gaze target] | [camera move type · direction, what moves with it] | [link to next scene]
Preserve: each cut keeps the space, architecture, objects, light, and color of its reference image. Recurring people keep the same look and outfit. Logos and signage keep their form, spelling, and size.
Sound: one new instrumental track suited to this place — [genre, lead instrument, tempo, mood]. [Track arc and ending]. It runs 0→20s as a single piece with no stop or restart at cuts. Effects sit under the music, timed to [action, moment]. No human voices (dialogue or narration).
```

## Example (a national museum, excerpt)

- Image 1: summer afternoon exterior — grand architecture + blue sky, 16:9 wide, warm natural light.
- Image 2: entrance name stone — near-frontal, lettering crisp.
- Cut example: `[01 · 0–2s] | [image 1] | visitor walking toward the building, seen from behind | slow push-in | cut to the entrance`
- Sound example: gentle strings + piano, mid-tempo, one 20s piece; only visitor footsteps as effects.

## Q&A example (chat)

1. "Which place is this? (name, branch)" → "Example Museum, main building"
2. "Do you have photos? If not, I'll collect some from the web." → "no photos"
3. "Include a logo/signage shot? If you have the official logo file, send it." → "your call"
4. "I'll go with 20s · 16:9 · 720p, model Seedance 2.5 (R2V) by default."
5. "I'll show you a quote and get your approval before generating."
