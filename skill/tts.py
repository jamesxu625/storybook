# -*- coding: utf-8 -*-
"""
Picture Book TTS - generate bilingual voice-over for a story JSON using edge-tts.

Usage:
    python tts.py stories/fox-adventure.json
    python tts.py stories/fox-adventure.json --lang en        # only English
    python tts.py stories/fox-adventure.json --voice-zh zh-CN-YunxiNeural
    python tts.py stories/fox-adventure.json --force          # regenerate everything

For each page it generates one Chinese MP3 and one English MP3, caches them in
voices/ (keyed by a hash of text+voice+params so reruns are instant), then embeds
both as base64 into the JSON as `audio_zh` / `audio_en` fields. The editor and
exported book read these fields automatically; older files without them keep
falling back to the browser's speechSynthesis.

Requires: pip install edge-tts   (Microsoft neural voices, free, no API key)
"""

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

try:
    import aiohttp
    import edge_tts
except ImportError:
    sys.stderr.write(
        "Missing dependency. Install it with:\n    pip install edge-tts\n"
    )
    sys.exit(1)

# Voices picked for children's content: warm, friendly, expressive.
DEFAULT_VOICE_ZH = "zh-CN-XiaoxiaoNeural"   # 晓晓, female, gentle
DEFAULT_VOICE_EN = "en-US-JennyNeural"      # Jenny, female, friendly

# edge-tts v7 pitch is in Hz (not the 1.0/1.15 multiplier used by speechSynthesis).
# These approximate the existing player settings (rate 0.85 -> -15%, slightly brighter pitch).
DEFAULT_RATE = "-15%"      # ~0.85x of normal speed, matches existing playback
DEFAULT_PITCH_ZH = "+15Hz"
DEFAULT_PITCH_EN = "+10Hz"

# Joiners for multi-line text on one page.
ZH_JOIN = "\u3002"          # Chinese fullwidth period
EN_JOIN = ". "

# Note: this script deliberately avoids fullwidth CJK punctuation in Python
# literals on Windows source files. The two literals above use \u escapes on
# purpose; the readability cost is minor and it sidesteps encoding pitfalls.


def make_connector():
    """Build an aiohttp connector with the threaded DNS resolver.

    edge-tts 7.x uses aiohttp's DefaultResolver, which prefers aiodns. In some
    sandboxes aiodns cannot reach its configured DNS servers even though the
    system resolver works fine. The threaded resolver goes through
    socket.getaddrinfo (the same path nslookup/urllib use) and is robust here.
    """
    return aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())


def cache_key(text, voice, rate, pitch):
    """Stable hash for a single TTS job so reruns reuse the cached MP3."""
    h = hashlib.md5()
    h.update(text.encode("utf-8"))
    h.update(b"|")
    h.update(voice.encode("utf-8"))
    h.update(b"|")
    h.update(rate.encode("utf-8"))
    h.update(b"|")
    h.update(pitch.encode("utf-8"))
    return h.hexdigest()


async def synth(text, voice, rate, pitch, out_path):
    """Synthesize one MP3 file. Overwrites if present (caller caches by key)."""
    connector = make_connector()
    try:
        comm = edge_tts.Communicate(
            text, voice=voice, rate=rate, pitch=pitch, connector=connector
        )
        await comm.save(str(out_path))
    finally:
        await connector.close()

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError("edge-tts produced no audio (check network/voice)")


async def synth_cached(text, voice, rate, pitch, voices_dir, force):
    """Return base64 data-URI. Reuses cache unless `force`."""
    key = cache_key(text, voice, rate, pitch)
    mp3 = voices_dir / f"{key}.mp3"

    if force or not mp3.exists():
        # Best-effort: clear a half-written file if a previous run crashed.
        await synth(text, voice, rate, pitch, mp3)

    data = mp3.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    return "data:audio/mpeg;base64," + b64, len(data)


def clean_lines(lines):
    """Drop blank/whitespace-only lines, keep order."""
    return [ln.strip() for ln in lines if ln and ln.strip()]


async def process_page(page, voices_dir, voices, rates, pitches, force, idx, total):
    """Fill audio_zh / audio_en on one page (in place)."""
    pid = page.get("id") or f"page{idx}"

    zh_lines = clean_lines(page.get("zh", []))
    en_lines = clean_lines(page.get("en", []))

    tasks = []
    if zh_lines and "zh" in voices:
        zh_text = ZH_JOIN.join(zh_lines)
        tasks.append(("zh", synth_cached(zh_text, voices["zh"], rates["zh"],
                                         pitches["zh"], voices_dir, force)))
    else:
        page.pop("audio_zh", None)

    if en_lines and "en" in voices:
        en_text = EN_JOIN.join(en_lines)
        tasks.append(("en", synth_cached(en_text, voices["en"], rates["en"],
                                         pitches["en"], voices_dir, force)))
    else:
        page.pop("audio_en", None)

    results = await asyncio.gather(*[t[1] for t in tasks]) if tasks else []
    for (lang, _), (data_uri, _size) in zip(tasks, results):
        page[f"audio_{lang}"] = data_uri

    # Progress line for the user.
    tag = []
    if "audio_zh" in page:
        tag.append("zh")
    if "audio_en" in page:
        tag.append("en")
    print(f"  [{idx + 1}/{total}] {pid:<8} -> {('+'.join(tag)) or '-'}")

    return page


async def run(story_path, args):
    with open(story_path, "r", encoding="utf-8") as f:
        raw = f.read()
    story = json.loads(raw)

    pages = story.get("pages", [])
    if not pages:
        sys.stderr.write("No pages found in story.\n")
        return 1

    voices_dir = story_path.parent / "voices"
    voices_dir.mkdir(exist_ok=True)

    voices = {}
    rates = {}
    pitches = {}
    if "zh" in args.lang:
        voices["zh"] = args.voice_zh
        rates["zh"] = args.rate
        pitches["zh"] = args.pitch_zh
    if "en" in args.lang:
        voices["en"] = args.voice_en
        rates["en"] = args.rate
        pitches["en"] = args.pitch_en

    print(f"Voices: {', '.join(f'{v} ({voices[v]})' for v in voices) or 'none'}")
    print(f"Caching MP3s in: {voices_dir}")
    print(f"Pages: {len(pages)}")

    # Process pages with modest concurrency to avoid hammering the endpoint.
    sem = asyncio.Semaphore(args.concurrency)

    async def bounded(idx, page):
        async with sem:
            return await process_page(page, voices_dir, voices, rates, pitches,
                                      args.force, idx, len(pages))

    await asyncio.gather(*[bounded(i, p) for i, p in enumerate(pages)])

    # Back up the original file once, then write the new one with embedded audio.
    bak = story_path.with_suffix(story_path.suffix + ".bak")
    if not bak.exists():
        shutil.copy2(story_path, bak)

    with open(story_path, "w", encoding="utf-8") as f:
        json.dump(story, f, ensure_ascii=False, indent=2)

    total_kb = sum(
        (len(p.get("audio_zh", "")) + len(p.get("audio_en", ""))) for p in pages
    ) / 1024 * 0.75  # base64 is ~33% overhead
    print(f"\nDone. Audio embedded in {story_path.name} (~{total_kb:.0f} KB base64).")
    print(f"Original backed up to {bak.name}.")
    return 0


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Generate bilingual edge-tts voice-over for a picture-book story JSON."
    )
    p.add_argument("story", help="Path to the story JSON file.")
    p.add_argument("--lang", default="zh,en",
                   help="Comma-separated languages to generate (default: zh,en).")
    p.add_argument("--voice-zh", default=DEFAULT_VOICE_ZH, help="Chinese edge-tts voice.")
    p.add_argument("--voice-en", default=DEFAULT_VOICE_EN, help="English edge-tts voice.")
    p.add_argument("--rate", default=DEFAULT_RATE,
                   help="Speaking rate, e.g. -15%% (default: %(default)s).")
    p.add_argument("--pitch-zh", default=DEFAULT_PITCH_ZH, help="Chinese pitch in Hz.")
    p.add_argument("--pitch-en", default=DEFAULT_PITCH_EN, help="English pitch in Hz.")
    p.add_argument("--force", action="store_true",
                   help="Regenerate audio even if a cache hit exists.")
    p.add_argument("--concurrency", type=int, default=3,
                   help="Max parallel TTS requests (default: 3).")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])

    # Normalize --lang into a list of codes.
    langs = [l.strip() for l in args.lang.split(",") if l.strip()]
    args.lang = langs
    bad = [l for l in langs if l not in ("zh", "en")]
    if bad:
        sys.stderr.write(f"Unknown language(s): {bad}. Use zh and/or en.\n")
        return 2

    story_path = Path(args.story)
    if not story_path.is_file():
        sys.stderr.write(f"Story file not found: {story_path}\n")
        return 2

    try:
        return asyncio.run(run(story_path, args))
    except KeyboardInterrupt:
        sys.stderr.write("\nInterrupted.\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
