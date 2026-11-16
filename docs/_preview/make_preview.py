"""Package the MOM_PREVIEW=1 build as a static site: only this project's own assets (theme CSS/JS,
Pygments CSS, card icons), folders without leading underscores, and a search index.

    MOM_PREVIEW=1 python -m sphinx -W -b html docs docs/_build/preview
    python docs/_preview/make_preview.py docs/_build/preview docs/_build/site
"""
import html as htmlmod
import json
import re
import shutil
import sys
from pathlib import Path

OWN_STATIC = ["site.css", "site.js", "pygments.css", "index_getting_started.svg", "index_user_guide.svg",
              "index_api.svg", "index_contribute.svg"]
SECTIONS = {"getting_started": "Getting started", "user_guide": "User guide", "reference": "API reference",
            "development": "Development", "whatsnew": "Release notes", "modules": "Source code"}


def _text(fragment: str) -> str:
    fragment = re.sub(r"<(script|style)\b.*?</\1>", " ", fragment, flags=re.S)
    text = re.sub(r"\s+", " ", htmlmod.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()
    return re.sub(r"\(\s+", "(", re.sub(r"\s+([),.:;])", r"\1", text))


def main(src: str, dst: str) -> None:
    s, d = Path(src), Path(dst)
    if d.exists():
        shutil.rmtree(d)
    (d / "static").mkdir(parents=True)
    for name in OWN_STATIC:
        shutil.copy(s / "_static" / name, d / "static" / name)
    if (s / "_images").exists():                       # the card icons, copied by Sphinx
        (d / "images").mkdir()
        for img in (s / "_images").glob("index_*.svg"):
            shutil.copy(img, d / "images" / img.name)
    index = []
    for page in sorted(s.rglob("*.html")):
        rel = page.relative_to(s)
        if rel.name == "search.html":
            continue
        out_rel = Path(*[("modules" if part == "_modules" else part) for part in rel.parts])
        html = page.read_text(encoding="utf-8")
        # keep only our own stylesheet/script tags (the theme emits nothing else; this is a safety net)
        html = re.sub(r'<script\b(?![^>]*site\.js)[^>]*\bsrc="[^"]*"[^>]*>\s*</script>\s*', "", html)
        html = re.sub(r'<link rel="stylesheet"(?![^>]*(?:site|pygments)\.css)[^>]*>\s*', "", html)
        html = html.replace("_static/", "static/").replace("_modules/", "modules/").replace("_images/", "images/")
        out = d / out_rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        if out_rel.parts[0] == "modules" or rel.name == "genindex.html":
            continue
        art = re.search(r'<article class="bd-article">(.*?)</article>', html, flags=re.S)
        body = art.group(1) if art else html
        h1 = re.search(r"<h1[^>]*>(.*?)</h1>", body, flags=re.S)
        heads = [_text(h) for h in re.findall(r"<h[23][^>]*>(.*?)</h[23]>", body, flags=re.S)]
        section = SECTIONS.get(out_rel.parts[0], "Home") if len(out_rel.parts) > 1 else "Home"
        index.append({"u": out_rel.as_posix(), "t": _text(h1.group(1)) if h1 else out_rel.stem,
                      "s": section, "h": heads, "x": _text(body)[:6000]})
    (d / "static" / "search.json").write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")),
                                              encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
