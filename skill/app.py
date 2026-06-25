# -*- coding: utf-8 -*-
"""
Picture Book Workbench - local backend server.

Serves the workbench frontend and provides APIs for:
  - generating a story JSON from keywords via ZhipuAI GLM
  - reading / listing / saving story JSON
  - generating zh+en voice-over via edge-tts (reuses tts.py)
  - exporting the final standalone HTML (reuses generateBookHTML via Node)

Run with:  python skill/app.py    (or double-click start.bat)
Then open: http://localhost:5000

Requires: pip install flask edge-tts zhipuai   and   node on PATH.
"""

import asyncio
import base64
import io
import json
import os
import subprocess
import sys
from pathlib import Path

from flask import Flask, request, jsonify, send_file, send_from_directory

# Make sibling tts.py importable.
SKILL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SKILL_DIR))
import tts as tts_mod  # noqa: E402

ROOT = SKILL_DIR.parent            # 绘本工具链/
STORIES_DIR = ROOT / "stories"
OUTPUT_DIR = ROOT / "output"
EDITOR_HTML = SKILL_DIR / "editor.html"
ENV_FILE = SKILL_DIR / ".env"      # stores the GLM API key (gitignored)

app = Flask(__name__, static_folder=None)


# ---------- API key / config ----------
def get_api_key():
    """Read the GLM API key from skill/.env (line: ZHIPU_API_KEY=xxx)."""
    if not ENV_FILE.exists():
        return ""
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("ZHIPU_API_KEY="):
            return line.split("=", 1)[1].strip()
    return ""


def set_api_key(key):
    """Persist the GLM API key to skill/.env."""
    ENV_FILE.write_text(f"ZHIPU_API_KEY={key}\n", encoding="utf-8")


# ---------- image compression ----------
MAX_W, MAX_H = 1080, 1920   # target 9:16 vertical
JPEG_QUALITY = 85


def compress_image_data_uri(data_uri):
    """Decode a data:image URI, resize to <=1080x1920, re-encode as JPEG q85.

    Returns a new data:image/jpeg;base64,... URI. Skips (returns original)
    if it can't decode or isn't an image. A 11MB 4K PNG typically -> ~300KB.
    """
    try:
        from PIL import Image
    except ImportError:
        return data_uri  # Pillow missing — export uncompressed
    try:
        if not data_uri.startswith("data:image/"):
            return data_uri
        header, b64 = data_uri.split(",", 1)
        raw = base64.b64decode(b64)
        img = Image.open(io.BytesIO(raw))
        img = img.convert("RGB")
        # Resize only if larger than target; keep aspect ratio.
        img.thumbnail((MAX_W, MAX_H), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        out_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return "data:image/jpeg;base64," + out_b64
    except Exception:
        return data_uri  # don't let a bad image break export


# ---------- config (API key) ----------
@app.route("/api/config", methods=["GET"])
def api_get_config():
    return jsonify({"has_key": bool(get_api_key())})


@app.route("/api/config", methods=["POST"])
def api_set_config():
    body = request.get_json(force=True)
    key = (body.get("key") or "").strip()
    if not key:
        return jsonify({"error": "empty key"}), 400
    set_api_key(key)
    return jsonify({"ok": True, "has_key": True})


# ---------- story generation (ZhipuAI GLM) ----------
STORY_PROMPT = """You are a bilingual children's picture book author. Create a picture book story in BOTH Chinese and English based on the user's request.

Return ONLY a valid JSON object (no markdown, no explanation) with this exact schema:
{{
  "meta": {{
    "title_zh": "Chinese title",
    "title_en": "English title",
    "author": "AI Storyteller",
    "style": "{style}",
    "aspect": "9:16"
  }},
  "pages": [
    {{
      "id": "cover",
      "type": "cover",
      "zh": ["Chinese title line"],
      "en": ["English title line"],
      "prompt": "detailed English image generation prompt for this page, under 50 words, describing subject/action/setting/mood"
    }},
    {{
      "id": "p1",
      "type": "story",
      "zh": ["Chinese line 1", "Chinese line 2"],
      "en": ["English line 1", "English line 2"],
      "prompt": "detailed English image prompt under 50 words"
    }}
  ]
}}

Rules:
- Total {pages} pages: 1 cover, {pages_minus_2} story pages, 1 ending (id "p10"/type "ending" for the last).
- Story page ids: p1, p2, ... in order.
- Chinese: simple vivid language for age {age}.
- English: natural translation, not word-for-word.
- Each zh/en array: 1-2 lines max.
- Each "prompt": describe the scene for AI image generation, consistent character across pages.
- Use dialogue with quotes where appropriate.
- The story should be cohesive with a clear beginning, middle, and happy ending.

User request: {theme}"""


@app.route("/api/generate", methods=["POST"])
def api_generate():
    """Generate a story JSON from keywords via ZhipuAI GLM."""
    key = get_api_key()
    if not key:
        return jsonify({"error": "no_api_key"}), 400

    body = request.get_json(force=True)
    theme = (body.get("theme") or "").strip()
    age = body.get("age", "3-6")
    pages = int(body.get("pages", 10))
    style = body.get("style", "cute watercolor children's illustration, soft pastel colors, vertical 9:16 composition")
    if not theme:
        return jsonify({"error": "theme required"}), 400
    pages = max(4, min(20, pages))

    prompt = STORY_PROMPT.format(
        style=style, pages=pages, pages_minus_2=max(1, pages - 2),
        age=age, theme=theme,
    )

    try:
        from zhipuai import ZhipuAI
        client = ZhipuAI(api_key=key)
        resp = client.chat.completions.create(
            model="glm-4-plus",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.8,
        )
        content = resp.choices[0].message.content.strip()
        # GLM may wrap JSON in ```json ... ``` fences; strip them.
        if content.startswith("```"):
            content = content.split("\n", 1)[1] if "\n" in content else content[3:]
            if content.endswith("```"):
                content = content[:-3]
            content = content.strip()
        story = json.loads(content)
        # Basic validation.
        if "meta" not in story or "pages" not in story:
            raise ValueError("missing meta/pages in response")
        return jsonify(story)
    except ImportError:
        return jsonify({"error": "zhipuai not installed: pip install zhipuai"}), 500
    except json.JSONDecodeError as e:
        return jsonify({"error": f"LLM returned invalid JSON: {e}"}), 502
    except Exception as e:
        msg = str(e)
        if "api_key" in msg.lower() or "auth" in msg.lower():
            return jsonify({"error": "invalid API key"}), 401
        return jsonify({"error": f"generation failed: {msg}"}), 500


# ---------- story files ----------
@app.route("/api/stories")
def api_list_stories():
    """List available story JSON files (excluding .bak)."""
    STORIES_DIR.mkdir(exist_ok=True)
    names = sorted(p.name for p in STORIES_DIR.glob("*.json")
                   if not p.name.endswith(".bak"))
    return jsonify(names)


@app.route("/api/story")
def api_get_story():
    """Read a story JSON by name (?name=xxx.json)."""
    name = request.args.get("name", "")
    path = STORIES_DIR / name
    if not path.exists() or not name.endswith(".json"):
        return jsonify({"error": "story not found"}), 404
    with open(path, "r", encoding="utf-8") as f:
        return jsonify(json.load(f))


@app.route("/api/story", methods=["POST"])
def api_save_story():
    """Save a story JSON. Body: {name, story}."""
    body = request.get_json(force=True)
    name = body.get("name", "")
    story = body.get("story")
    if not name.endswith(".json") or story is None:
        return jsonify({"error": "bad request"}), 400
    path = STORIES_DIR / name
    STORIES_DIR.mkdir(exist_ok=True)
    # Back up once on first save.
    bak = path.with_suffix(".json.bak")
    if not bak.exists() and path.exists():
        import shutil
        shutil.copy2(path, bak)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(story, f, ensure_ascii=False, indent=2)
    return jsonify({"ok": True})


# ---------- dubbing ----------
@app.route("/api/tts", methods=["POST"])
def api_tts():
    """Generate zh+en voice-over for a story. Body: {name, lang?, force?}.

    Reuses tts.py's async pipeline. Returns the updated story JSON with
    audio_zh / audio_en embedded.
    """
    body = request.get_json(force=True)
    name = body.get("name", "")
    langs = body.get("lang", ["zh", "en"])
    force = bool(body.get("force", False))
    path = STORIES_DIR / name
    if not path.exists():
        return jsonify({"error": "story not found"}), 404

    class TArgs:
        pass
    a = TArgs()
    a.lang = langs if isinstance(langs, list) else [langs]
    a.voice_zh = tts_mod.DEFAULT_VOICE_ZH
    a.voice_en = tts_mod.DEFAULT_VOICE_EN
    a.rate = tts_mod.DEFAULT_RATE
    a.pitch_zh = tts_mod.DEFAULT_PITCH_ZH
    a.pitch_en = tts_mod.DEFAULT_PITCH_EN
    a.force = force
    a.concurrency = 3

    try:
        asyncio.run(tts_mod.run(path, a))
    except Exception as e:
        return jsonify({"error": f"tts failed: {e}"}), 500

    with open(path, "r", encoding="utf-8") as f:
        story = json.load(f)
    return jsonify(story)


# ---------- export ----------
EXPORT_RUNNER = r"""
// Extract generateBookHTML from editor.html, run on a story, write out HTML.
// Usage: node _export_runner.js <editor.html> <story.json> <out.html> <flags>
// flags: JSON array of strings like ["no-image","no-audio-zh",...]
const fs = require('fs');
const editor = fs.readFileSync(process.argv[2], 'utf8');
const start = editor.indexOf('function generateBookHTML(data) {');
const end = editor.indexOf('// ========== Utils');
let funcSrc = editor.slice(start, end).trim();
if (!funcSrc.endsWith('}')) funcSrc += '\n}';
const body = funcSrc.replace(/^function generateBookHTML\(data\)\s*\{/, '').replace(/\}$/, '');
const generateBookHTML = new Function('data', body);

let story = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const flags = JSON.parse(process.argv[4] || '[]');
// Apply content toggles by stripping fields (does NOT mutate the saved JSON).
if (flags.includes('no-image')) story.pages.forEach(p => { delete p.image; });
if (flags.includes('no-audio-zh')) story.pages.forEach(p => { delete p.audio_zh; });
if (flags.includes('no-audio-en')) story.pages.forEach(p => { delete p.audio_en; });
if (flags.includes('no-game')) story.pages.forEach(p => { delete p.game; });

const data = { meta: story.meta, pages: story.pages.map(p => ({...p, image: p.image || ''})) };
const out = generateBookHTML(data);
fs.writeFileSync(process.argv[5], out);
console.log('exported ' + (out.length / 1024).toFixed(0) + ' KB');
"""


@app.route("/api/export", methods=["POST"])
def api_export():
    """Export the final HTML. Body: {name, flags?}. Returns the file."""
    body = request.get_json(force=True)
    name = body.get("name", "")
    flags = body.get("flags", [])
    path = STORIES_DIR / name
    if not path.exists():
        return jsonify({"error": "story not found"}), 404

    OUTPUT_DIR.mkdir(exist_ok=True)
    with open(path, "r", encoding="utf-8") as f:
        story = json.load(f)
    title_en = story.get("meta", {}).get("title_en") or "picture-book"
    out_path = OUTPUT_DIR / f"{title_en}.html"

    # Compress images for export (don't mutate the saved JSON).
    if "no-image" not in flags:
        for p in story["pages"]:
            if p.get("image"):
                p["image"] = compress_image_data_uri(p["image"])

    # Write a temporary compressed story JSON for Node to read.
    tmp_story = OUTPUT_DIR / ".export_tmp.json"
    with open(tmp_story, "w", encoding="utf-8") as f:
        json.dump(story, f, ensure_ascii=False)

    runner = SKILL_DIR / "_export_runner.js"
    runner.write_text(EXPORT_RUNNER, encoding="utf-8")
    try:
        r = subprocess.run(
            ["node", str(runner), str(EDITOR_HTML), str(tmp_story),
             json.dumps(flags), str(out_path)],
            capture_output=True, text=True, encoding="utf-8",
        )
        if r.returncode != 0:
            return jsonify({"error": r.stderr or r.stdout}), 500
    finally:
        if runner.exists():
            runner.unlink()
        if tmp_story.exists():
            tmp_story.unlink()

    return send_file(str(out_path), as_attachment=True,
                     download_name=out_path.name)


# ---------- serve frontend ----------
@app.route("/")
def index():
    return send_from_directory(str(SKILL_DIR), "workbench.html")


@app.route("/<path:p>")
def static_files(p):
    return send_from_directory(str(SKILL_DIR), p)


if __name__ == "__main__":
    OUTPUT_DIR.mkdir(exist_ok=True)
    print("=" * 50)
    print("  绘本工作台已启动")
    print("  浏览器打开: http://localhost:5000")
    print("  按 Ctrl+C 停止")
    print("=" * 50)
    app.run(host="127.0.0.1", port=5000, debug=False)
