---
name: picture-book-creator
description: Creates interactive bilingual (Chinese-English) picture books. Converts story ideas into structured JSON scripts, then generates a visual HTML editor where users can upload AI-generated images, adjust text, and export standalone interactive picture book HTML files. Triggers on requests like "create a picture book", "make a story book", "绘本", "故事书", "儿童绘本", "interactive book".
version: 1.0.0
---

# Interactive Picture Book Creator

## Overview
This skill creates bilingual (Chinese + English) interactive picture books through a five-phase workflow:
1. **Story Scripting** — Generate a structured JSON story file
2. **Editor Generation** — Produce an HTML editor pre-loaded with the story
3. **Voice-Over** *(optional but recommended)* — Generate neural-voice MP3 dubbing with edge-tts
4. **Interactive Games** *(optional)* — Add per-page mini-games (choice / count) in the editor
5. **User Assembly** — User uploads AI images in the editor and exports the final book

## Phase 1: Story Scripting

When the user wants to create a picture book, gather these details first:
- Story theme / concept
- Target age group (default: 3-6 years)
- Number of pages (default: 10-12 including cover and ending)
- Visual style (default: cute watercolor children's illustration, soft pastel colors)

Then generate a story JSON file following this schema:

```json
{
  "meta": {
    "title_zh": "中文标题",
    "title_en": "English Title",
    "author": "Author Name",
    "style": "visual style description for AI image generation",
    "aspect": "9:16"
  },
  "pages": [
    {
      "id": "cover",
      "type": "cover",
      "zh": ["中文标题行"],
      "en": ["English title line"],
      "image_file": "",
      "prompt": "AI image generation prompt for this page"
    },
    {
      "id": "p1",
      "type": "story",
      "zh": ["中文第一行", "中文第二行"],
      "en": ["English line 1", "English line 2"],
      "image_file": "",
      "prompt": "detailed image description",
      "audio_zh": "data:audio/mpeg;base64,...",   // added by Phase 2.5 tts.py
      "audio_en": "data:audio/mpeg;base64,..."    // optional; omitted on un-dubbed pages
    }
  ]
}
```

> `audio_zh` / `audio_en` are optional. The editor and exported book auto-detect them; pages without them fall back to the browser's built-in `speechSynthesis`.

### Page types
- `cover` — Title page, usually 1 zh line + 1 en line
- `story` — Normal story page, 2 zh lines + 2 en lines
- `ending` — Last page, 2 zh lines + 2 en lines

### Prompt writing rules
- Always prefix with `meta.style` (the global style)
- Include: subject (the main character), action, setting, mood, key visual elements
- Keep prompts under 50 words
- Use consistent character description across pages

### Story writing rules
- Chinese text: simple, vivid language suitable for children
- English text: natural translation, not word-for-word
- Each zh/en array should have 1-2 lines max
- Use dialogue with quotes where appropriate

## Phase 2: Editor Generation

After generating the story JSON:
1. Save the JSON to `stories/{story-name}.json`
2. Copy the editor template from `skill/editor.html` (located relative to this skill directory)
3. Present both files to the user

The editor HTML is a standalone single-file application. The user opens it in a browser and loads the JSON file.

## Phase 2.5: Voice-Over (edge-tts dubbing)

This phase generates high-quality neural-voice MP3 audio for every page and embeds it into the story JSON, so the exported book plays real audio instead of relying on the browser's (often poor) `speechSynthesis`. It is optional but strongly recommended — Windows in particular ships no usable Chinese voice.

**Prerequisite:** install edge-tts once (free Microsoft neural voices, no API key):
```
pip install edge-tts
```

**Generate the dubbing** (run from the `绘本工具链` directory):
```
python skill/tts.py stories/{story-name}.json
```
This reads the story, synthesizes one Chinese and one English MP3 per page (default voices `zh-CN-XiaoxiaoNeural` + `en-US-JennyNeural`), caches them in `stories/voices/`, and writes `audio_zh` / `audio_en` fields back into the JSON. The original file is backed up to `*.json.bak` on first run.

**Useful flags:**
- `--lang en` — only generate English
- `--voice-zh zh-CN-YunxiNeural` — change a voice (run `edge-tts --list-voices` to see all)
- `--force` — regenerate even when the cache has a hit
- `--rate "-10%"` / `--pitch-zh "+20Hz"` — adjust pacing/tone

Reruns are instant: each clip is cached by a hash of `(text + voice + rate + pitch)`, so only changed pages are re-synthesized. The editor's per-page "🔊 配音状态" section shows which languages are ready and offers a preview button.

## Phase 2.6: Interactive Games (optional)

This phase lets you add per-page mini-games directly in the editor, so the exported book has activities children can play. It is entirely optional and runs in the browser — no extra tooling.

In the editor's per-page "🎮 互动小游戏" section, choose a game type:
- **无互动 (none)** — plain story page (default)
- **点选答题 (choice)** — a question with several text/emoji options; one is correct
- **数一数 / 计数 (count)** — a question answered by picking the right number (auto-generates 1..N options)

Each game is stored on the page as an optional `game` object (see schema below). The editor UI handles adding/removing options and marking the correct one (click the dot on the left). When exported, the game floats over the page center: correct answers get a green pop + a cheerful WebAudio chime and reveal a "🔊 听这个故事" button; wrong answers shake red with a soft tone. Games never block page-turning.

```json
"game": {
  "type": "choice",
  "prompt_zh": "哪个是红色的花？",
  "prompt_en": "Which is the red flower?",
  "options": [
    {"label_zh": "🌹", "label_en": "rose",  "correct": true},
    {"label_zh": "🍃", "label_en": "leaf",  "correct": false}
  ]
}
```
For `count`, `options` holds `{"label_zh":"3","label_en":"3","correct":true}` entries generated from the chosen range. Pages with no `game` field are unaffected.

## Phase 3: User Assembly (guide the user)

Walk the user through:
1. Open the editor HTML in a browser
2. Load the story JSON file (click or drag-and-drop)
3. For each page:
   a. Copy the AI prompt (click the copy button)
   b. Generate the image using Kling AI, DALL-E, Midjourney, or any AI image tool
   c. Upload the generated image in the editor
   d. Adjust text if needed
4. Use "全部预览" to review all pages
5. Click "导出绘本" to download the final interactive HTML

The exported HTML is:
- Completely standalone (all images and voice-over MP3s embedded as base64)
- Mobile-friendly with touch swipe support
- Features page-turn animations, bilingual audio (embedded MP3, falls back to TTS if absent), autoplay mode, and optional per-page interactive mini-games with WebAudio feedback
- Works offline — just open in any browser

## Quick Reference: Directory Structure

```
绘本工具链/
├── stories/          # Story JSON files
│   └── voices/       # tts.py MP3 cache (auto-created, safe to delete)
├── skill/
│   ├── editor.html   # Editor template (single file)
│   └── tts.py        # edge-tts voice-over generator (Phase 2.5)
└── output/           # Exported books
```

## Pitfalls
- Chinese fullwidth punctuation (U+FF01 etc.) must NOT appear in Python source code on Windows; use JSON files instead
- Exported HTML can be large (20-40MB) if images are high-res; the editor handles this via base64 embedding
- Web Speech API voices vary by browser/OS; Chrome has the best Chinese voice support
- For best results, AI images should be 1080x1920 (9:16 vertical)
- **edge-tts needs internet** — it streams from `speech.platform.bing.com`. `tts.py` already works around a sandbox DNS quirk by forcing `aiohttp.ThreadedResolver`; if you still see "Could not contact DNS servers", check your network/proxy. The generated MP3s are then fully offline.
- **edge-tts pitch is in Hz**, not the 0.85/1.1 multipliers that `speechSynthesis` uses. `tts.py` defaults (`-15%` rate, `+15Hz`/`+10Hz` pitch) approximate the existing player settings.
- **`generateBookHTML` is one big template literal** — any `\n` inside it becomes a real newline in the exported JS (breaking string literals). Write `\\n` when you need a literal newline character in the runtime. Same for quotes: `onclick="speak('zh')"` inside a single-quoted runtime string needs `\\'`. The exported runtime script should always be syntax-checked after edits (e.g. extract it and run `node --check`).
- **Interactive games use no external libraries** — everything (render, judge, WebAudio chimes) is hand-written JS so the exported book stays single-file and offline-capable. Audio feedback is synthesized with `AudioContext` oscillators (a C-E-G-C arpeggio for correct, a soft downward dip for wrong); no sound files are bundled.
