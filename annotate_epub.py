#!/usr/bin/env python3
"""EPUB annotator script for Gemini-generated footnotes."""

import json
import os
import re
import sys
import zipfile
from pathlib import Path
from typing import Dict, List
from urllib import error, request

from bs4 import BeautifulSoup, NavigableString, Tag


def call_gemini(api_key: str, model: str, text: str) -> List[Dict[str, str]]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    prompt = f"""You are a careful literary editor helping modern readers understand older texts.

Read the following excerpt and identify words, phrases, and references that a contemporary English reader may not be familiar with. Focus on:
- Archaic or obsolete words
- Historical references they may not know
- Uncommon literary vocabulary
- Old-fashioned idioms or expressions
- Technical terms specific to the historical period

Return ONLY a valid JSON array. Each item must have exactly two fields:
- "term": the unfamiliar word or phrase as it appears in the text
- "explanation": a concise, plain-English explanation (2-3 sentences max)

If you find no difficult terms, return an empty array: []

Do NOT include markdown formatting or code fences. Return ONLY the JSON array.

Excerpt:
{text}"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "topP": 0.9, "maxOutputTokens": 2048},
    }

    data = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=180) as resp:
            body = resp.read().decode("utf-8")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini API request failed: HTTP {exc.code}\n{detail}") from exc

    response = json.loads(body)
    if "error" in response:
        raise RuntimeError(f"Gemini returned an error: {response['error']}")

    candidates = response.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Gemini returned no candidates: {body}")

    text_response = (
        candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
    )
    if not text_response.strip():
        return []

    cleaned = text_response.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("` \n")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()

    match = re.search(r"\[[\s\S]*\]", cleaned)
    if match:
        cleaned = match.group(0)

    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Gemini returned invalid JSON:\n{cleaned[:500]}") from exc

    if not isinstance(result, list):
        return []

    normalized: List[Dict[str, str]] = []
    for item in result:
        if not isinstance(item, dict):
            continue
        term = str(item.get("term", "")).strip()
        explanation = str(item.get("explanation", "")).strip()
        if term and explanation:
            normalized.append({"term": term, "explanation": explanation})

    return normalized


def chunk_text(text: str, max_size: int) -> List[str]:
    if len(text) <= max_size:
        return [text]
    return [text[i:i + max_size] for i in range(0, len(text), max_size)]


def normalize_term(term: str) -> str:
    cleaned = re.sub(r"\s+", " ", term.strip())
    return cleaned.strip('.,;:!?()[]{}"\'\n')


def extract_text_from_html(html_content: str) -> str:
    soup = BeautifulSoup(html_content, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    return re.sub(r"\s+", " ", text)


def extract_epub_chapters(epub_path: Path) -> Dict[str, str]:
    with zipfile.ZipFile(epub_path, "r") as archive:
        html_files = [
            name for name in archive.namelist()
            if name.lower().endswith((".xhtml", ".html", ".htm"))
        ]
        if not html_files:
            raise FileNotFoundError(f"No HTML/XHTML files found in {epub_path}")

        chapters: Dict[str, str] = {}
        for name in sorted(html_files):
            try:
                content = archive.read(name).decode("utf-8")
            except UnicodeDecodeError:
                content = archive.read(name).decode("utf-8", errors="replace")
            chapters[name] = content
        return chapters


def add_footnotes_to_html(html_content: str, annotations: List[Dict[str, str]]) -> str:
    if not annotations:
        return html_content

    soup = BeautifulSoup(html_content, "html.parser")
    body = soup.body if soup.body else soup

    unique_annotations: List[Dict[str, str]] = []
    seen = set()
    for ann in annotations:
        term = normalize_term(ann["term"])
        if not term or term.lower() in seen:
            continue
        seen.add(term.lower())
        unique_annotations.append({"term": term, "explanation": ann["explanation"]})

    if not unique_annotations:
        return html_content

    footnote_map = {ann["term"]: i + 1 for i, ann in enumerate(unique_annotations)}

    def replace_term_text(node: Tag) -> None:
        for child in list(node.children):
            if isinstance(child, NavigableString):
                text = str(child)
                matches: List[tuple[int, int, str, int]] = []
                for term, num in footnote_map.items():
                    pattern = rf"\b{re.escape(term)}\b"
                    for match in re.finditer(pattern, text, re.IGNORECASE):
                        matches.append((match.start(), match.end(), term, num))
                if not matches:
                    continue
                matches.sort(key=lambda x: x[0], reverse=True)
                new_text = text
                for start, end, term, num in matches:
                    note_id = f"epub-note-ref-{num}"
                    replacement = f"{new_text[start:end]}<sup class=\"epub-note\"><a href=\"#{note_id}\">[{num}]</a></sup>"
                    # The above is intentionally not a direct replacement because offsets change across iterations.
                    # We instead rebuild with a safer text transform below.
                    new_text = new_text[:start] + new_text[start:end] + f"<sup class=\"epub-note\"><a href=\"#{note_id}\">[{num}]</a></sup>" + new_text[end:]
                node.replace_with(BeautifulSoup(new_text, "html.parser"))
            elif isinstance(child, Tag):
                if child.name not in ["script", "style", "a"]:
                    replace_term_text(child)

    # The recursive string replacement above is fragile, so handle body text by converting the whole body HTML.
    body_html = str(body)
    for term, num in sorted(footnote_map.items(), key=lambda kv: len(kv[0]), reverse=True):
        pattern = rf"\b{re.escape(term)}\b"
        body_html = re.sub(
            pattern,
            lambda m: f"{m.group(0)}<sup class=\"epub-note\"><a href=\"#epub-note-ref-{num}\">[{num}]</a></sup>",
            body_html,
            flags=re.IGNORECASE,
        )

    body.clear()
    body.append(BeautifulSoup(body_html, "html.parser"))

    notes_section = soup.new_tag("section")
    notes_section["class"] = "epub-annotator-footnotes"
    heading = soup.new_tag("h2")
    heading.string = "Footnotes"
    notes_section.append(heading)

    notes_list = soup.new_tag("ol")
    notes_list["class"] = "epub-annotator-notes-list"
    for ann in unique_annotations:
        num = footnote_map[ann["term"]]
        li = soup.new_tag("li")
        li["id"] = f"epub-note-ref-{num}"
        strong = soup.new_tag("strong")
        strong.string = ann["term"]
        li.append(strong)
        li.append(" — ")
        li.append(ann["explanation"])
        notes_list.append(li)

    notes_section.append(notes_list)
    body.append(notes_section)

    style_tag = soup.new_tag("style")
    style_tag.string = """
    .epub-annotator-footnotes { margin-top: 2rem; border-top: 1px solid #ddd; padding-top: 1rem; }
    .epub-annotator-notes-list { margin-left: 1.5rem; }
    .epub-note a { text-decoration: none; color: #0066cc; }
    .epub-note a:hover { text-decoration: underline; }
    """
    if soup.head is None:
        head = soup.new_tag("head")
        soup.insert(0, head)
    soup.head.append(style_tag)

    return str(soup)


def save_annotated_epub(epub_path: Path, output_path: Path, modified_chapters: Dict[str, str]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(epub_path, "r") as zin:
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            for name in zin.namelist():
                if name in modified_chapters:
                    zout.writestr(name, modified_chapters[name].encode("utf-8"))
                else:
                    zout.writestr(name, zin.read(name))


def main() -> int:
    epub_path_str = os.environ.get("INPUT_EPUB_PATH")
    output_path_str = os.environ.get("OUTPUT_EPUB_PATH")
    api_key = os.environ.get("GEMINI_API_KEY")
    model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
    chunk_size = int(os.environ.get("CHUNK_SIZE_CHARS", "12000"))

    if not all([epub_path_str, output_path_str, api_key]):
        print("ERROR: Missing required environment variables", file=sys.stderr)
        return 1

    epub_path = Path(epub_path_str)
    output_path = Path(output_path_str)

    if not epub_path.exists():
        print(f"ERROR: EPUB file not found: {epub_path}", file=sys.stderr)
        return 1

    chapters = extract_epub_chapters(epub_path)
    modified_chapters: Dict[str, str] = {}

    for index, (chapter_name, chapter_html) in enumerate(chapters.items(), start=1):
        text = extract_text_from_html(chapter_html)
        if not text or len(text.strip()) < 50:
            continue

        chapter_annotations: List[Dict[str, str]] = []
        for chunk in chunk_text(text, chunk_size):
            try:
                chunk_annotations = call_gemini(api_key, model, chunk)
                chapter_annotations.extend(chunk_annotations)
            except RuntimeError as exc:
                print(f"WARNING: chunk failed for {chapter_name}: {exc}", file=sys.stderr)
                continue

        deduped: List[Dict[str, str]] = []
        seen = set()
        for ann in chapter_annotations:
            term = normalize_term(ann["term"])
            if not term or term.lower() in seen:
                continue
            seen.add(term.lower())
            deduped.append({"term": term, "explanation": ann["explanation"]})

        if not deduped:
            continue

        modified_chapters[chapter_name] = add_footnotes_to_html(chapter_html, deduped)

    save_annotated_epub(epub_path, output_path, modified_chapters)
    print(f"Annotated EPUB written to: {output_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
