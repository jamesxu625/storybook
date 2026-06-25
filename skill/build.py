# -*- coding: utf-8 -*-
"""
Picture Book Builder - one-command interactive assembly.

Walks you through the whole book in the terminal, then dubs + exports:
  1. Pick a story JSON (argument or choose from stories/)
  2. Per page: paste an image path (or skip), optionally configure a game
  3. Auto-generate zh+en voice-over with edge-tts (cached, fast reruns)
  4. Export the final standalone HTML book

Usage:
    python skill/build.py                       # choose story interactively
    python skill/build.py stories/fox-adventure.json
    python skill/build.py stories/fox.json --skip-tts     # keep existing audio
    python skill/build.py stories/fox.json --no-games     # skip game prompts

Requires: pip install edge-tts   (for dubbing)
          node available on PATH (for HTML export)

Note: this script avoids fullwidth CJK punctuation in Python literals on
Windows. The one \u3002 joiner below uses an escape on purpose.
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Make sibling tts.py importable so we reuse its dubbing logic (single source).
SKILL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SKILL_DIR))
import tts as tts_mod  # noqa: E402

ROOT = SKILL_DIR.parent            # 绘本工具链/
STORIES_DIR = ROOT / "stories"
EDITOR_HTML = SKILL_DIR / "editor.html"


# ---------- small terminal helpers ----------
def ask(prompt, default=""):
    """Line input with optional default shown in brackets."""
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        val = ""
    return val if val else default


def ask_yesno(prompt, default=True):
    d = "Y/n" if default else "y/N"
    val = input(f"{prompt} [{d}]: ").strip().lower()
    if not val:
        return default
    return val in ("y", "yes", "1", "true")


def pick_from_list(items, prompt):
    """Show a numbered list, return chosen item (or None)."""
    if not items:
        return None
    print(f"\n{prompt}")
    for i, it in enumerate(items, 1):
        print(f"  {i}. {it}")
    while True:
        val = input(f"选择 (1-{len(items)}, 回车跳过): ").strip()
        if not val:
            return None
        if val.isdigit() and 1 <= int(val) <= len(items):
            return items[int(val) - 1]
        print("  无效，重选。")


# ---------- image embedding ----------
def embed_image(path_str):
    """Read an image file, return a data: URI (or None on failure)."""
    p = Path(path_str.strip().strip('"').strip("'"))
    if not p.is_absolute():
        # Try relative to cwd then to root for convenience.
        for base in (Path.cwd(), ROOT):
            cand = base / p
            if cand.exists():
                p = cand
                break
    if not p.exists():
        print(f"    ! 文件不存在: {p}")
        return None
    ext = p.suffix.lower().lstrip(".")
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/jpeg")
    data = p.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    print(f"    ✓ 嵌入图片 ({len(data) // 1024} KB)")
    return f"data:{mime};base64,{b64}"


# ---------- game config ----------
def configure_game(page):
    """Interactively configure a game on a page. Returns the game dict or None."""
    print(f"\n    --- 游戏配置 (当前页: {page['id']}) ---")
    gtype = pick_from_list(["choice 点选答题", "count 数一数"], "选游戏类型")
    if not gtype:
        print("    跳过游戏")
        if "game" in page:
            del page["game"]
        return None
    gtype = gtype.split(" ")[0]  # 'choice' or 'count'

    prompt_zh = ask("    问题(中文)", "")
    prompt_en = ask("    Question(EN)", "")

    options = []
    print("    录入选项（直接回车结束）：")
    default_first = gtype == "count"
    idx = 0
    while True:
        label = ask(f"    选项{idx + 1}(中文/emoji)", "")
        if not label:
            break
        label_en = ask(f"    选项{idx + 1}(EN)", label)
        options.append({"label_zh": label, "label_en": label_en,
                        "correct": idx == 0})
        idx += 1

    if len(options) < 2:
        print("    ! 至少需要 2 个选项，游戏已取消")
        return None

    # Mark the correct one.
    print("    选项：")
    for i, o in enumerate(options):
        mark = " ✓" if o["correct"] else ""
        print(f"      {i + 1}. {o['label_zh']}{mark}")
    correct = ask("    哪个是正确答案？(输入序号，默认 1)", "1")
    try:
        ci = max(1, min(len(options), int(correct))) - 1
    except ValueError:
        ci = 0
    for i, o in enumerate(options):
        o["correct"] = (i == ci)

    game = {"type": gtype, "prompt_zh": prompt_zh, "prompt_en": prompt_en,
            "options": options}
    page["game"] = game
    print(f"    ✓ 游戏已配置: {gtype}, {len(options)} 个选项, 正确=#{ci + 1}")
    return game


# ---------- HTML export via Node (single source: editor.html) ----------
EXPORT_RUNNER = r"""
// Extracts generateBookHTML from editor.html and runs it on a story JSON.
// Usage: node _export_runner.js <editor.html> <story.json> <out.html>
const fs = require('fs');
const editor = fs.readFileSync(process.argv[2], 'utf8');
const start = editor.indexOf('function generateBookHTML(data) {');
const end = editor.indexOf('// ========== Utils');
let funcSrc = editor.slice(start, end).trim();
if (!funcSrc.endsWith('}')) funcSrc += '\n}';
const story = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const data = { meta: story.meta, pages: story.pages.map(p => ({...p, image: p.image || ''})) };
// eslint-disable-next-line no-new-func
const generateBookHTML = new Function('data', funcSrc.replace(/^function generateBookHTML\(data\)\s*\{/, '').replace(/\}$/, ''));
const out = generateBookHTML(data);
fs.writeFileSync(process.argv[4], out);
console.log('exported ' + (out.length / 1024).toFixed(0) + ' KB');
"""


def export_html(story_path, out_path):
    """Run Node on the extracted generateBookHTML to produce the final book."""
    runner = SKILL_DIR / "_export_runner.js"
    runner.write_text(EXPORT_RUNNER, encoding="utf-8")
    try:
        r = subprocess.run(
            ["node", str(runner), str(EDITOR_HTML), str(story_path), str(out_path)],
            capture_output=True, text=True, encoding="utf-8",
        )
        if r.returncode != 0:
            sys.stderr.write(r.stderr or r.stdout)
            return False
        print("    " + (r.stdout.strip() or "done"))
        return True
    finally:
        if runner.exists():
            runner.unlink()


# ---------- per-page interactive assembly ----------
def assemble_pages(story):
    pages = story["pages"]
    total = len(pages)
    print(f"\n=== 逐页配置 ({total} 页) ===")
    print("每页可以: 粘贴图片路径(可拖入文件)、配游戏、或回车跳过。\n")

    for i, p in enumerate(pages):
        kind = {"cover": "封面", "ending": "结尾"}.get(p.get("type"), f"第{i}页")
        print(f"[{i + 1}/{total}] {kind}  id={p['id']}")
        print(f"    中文: {' / '.join(p.get('zh', []))}")
        print(f"    EN  : {' / '.join(p.get('en', []))}")

        # Image
        img_path = ask("    图片路径(拖入文件或粘贴, 回车跳过)", "")
        if img_path:
            uri = embed_image(img_path)
            if uri:
                p["image"] = uri

        # Game
        existing = "有" if p.get("game") else "无"
        if ask_yesno(f"    配置游戏? (当前{existing})", default=False):
            configure_game(p)
        elif "game" in p and not ask_yesno("    保留现有游戏?", default=True):
            del p["game"]

        print()


# ---------- main ----------
def choose_story(arg):
    if arg:
        p = Path(arg)
        if not p.exists():
            sys.stderr.write(f"故事文件不存在: {p}\n")
            sys.exit(2)
        return p
    jsons = sorted(STORIES_DIR.glob("*.json"))
    jsons = [j for j in jsons if not j.name.endswith(".bak")]
    if not jsons:
        sys.stderr.write(f"在 {STORIES_DIR} 没找到故事 JSON\n")
        sys.exit(2)
    chosen = pick_from_list([j.name for j in jsons], "选择故事")
    if not chosen:
        sys.exit(0)
    return STORIES_DIR / chosen


def main(argv=None):
    ap = argparse.ArgumentParser(description="一站式构建绘本：配图 + 游戏 + 配音 + 导出")
    ap.add_argument("story", nargs="?", help="故事 JSON 路径（不填则交互选择）")
    ap.add_argument("--skip-tts", action="store_true", help="跳过配音（保留 JSON 里已有的音频）")
    ap.add_argument("--no-games", action="store_true", help="跳过游戏配置")
    ap.add_argument("--no-images", action="store_true", help="跳过图片配置")
    ap.add_argument("--out", help="导出 HTML 路径（默认 output/<title_en>.html）")
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])

    story_path = choose_story(args.story)
    print(f"\n故事: {story_path.name}")

    with open(story_path, "r", encoding="utf-8") as f:
        story = json.load(f)
    print(f"标题: {story['meta'].get('title_zh','?')} / {story['meta'].get('title_en','?')}")
    print(f"页数: {len(story['pages'])}")

    # 1. Images + games
    if not args.no_images or not args.no_games:
        # Back up the original once before we modify it. Skip if this is
        # already a .bak (don't chain backups) or a backup already exists.
        if story_path.suffix == ".json":
            bak = story_path.with_suffix(".json.bak")
            if not bak.exists():
                import shutil
                shutil.copy2(story_path, bak)

    if not args.no_images or not args.no_games:
        if args.no_images:
            print("\n(跳过图片配置)")
        if args.no_games:
            print("\n(跳过游戏配置)")
        # We still run the loop for whichever is enabled.
        if not args.no_images or not args.no_games:
            assemble_pages(story)

    # Save JSON with images + games (so tts step and export see them).
    with open(story_path, "w", encoding="utf-8") as f:
        json.dump(story, f, ensure_ascii=False, indent=2)
    print("已保存图片/游戏配置到 JSON")

    # 2. Dubbing
    if not args.skip_tts:
        print("\n=== 生成配音 ===")
        # Reuse tts.py's async pipeline by calling its run() with a fake args.
        class TArgs:
            lang = ["zh", "en"]
            voice_zh = tts_mod.DEFAULT_VOICE_ZH
            voice_en = tts_mod.DEFAULT_VOICE_EN
            rate = tts_mod.DEFAULT_RATE
            pitch_zh = tts_mod.DEFAULT_PITCH_ZH
            pitch_en = tts_mod.DEFAULT_PITCH_EN
            force = False
            concurrency = 3
        import asyncio
        try:
            asyncio.run(tts_mod.run(story_path, TArgs()))
        except SystemExit:
            raise
        except Exception as e:
            sys.stderr.write(f"配音失败: {e}\n")
            sys.stderr.write("可用 --skip-tts 跳过配音，或检查 edge-tts 安装/网络\n")
            # Reload story with whatever audio got embedded before failure.
            with open(story_path, "r", encoding="utf-8") as f:
                story = json.load(f)
    else:
        print("\n(跳过配音)")

    # 3. Export HTML
    print("\n=== 导出绘本 HTML ===")
    out_dir = ROOT / "output"
    out_dir.mkdir(exist_ok=True)
    title_en = story["meta"].get("title_en") or "picture-book"
    out_path = Path(args.out) if args.out else out_dir / f"{title_en}.html"
    ok = export_html(story_path, out_path)
    if ok:
        print(f"\n✅ 完成！绘本已导出:\n   {out_path}")
        print(f"   直接在浏览器打开即可（含图片+配音+游戏，离线可用）")
    else:
        sys.stderr.write("\n导出失败，请确认 node 已安装并在 PATH 中\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
