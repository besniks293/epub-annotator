#!/usr/bin/env python3
import argparse
import json
import os
import re
import sys
import zipfile
from pathlib import Path
from typing import List, Dict, Any
from urllib import request, error

from bs4 import BeautifulSoup, NavigableString


def read_input() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Annotate EPUB files with Gemini-generated footnotes.")
    parser.add_argument("--epub-path", required=True, help="Path to source EPUB file")
    parser.add_argument("--output-path", required=True, help="Path for annotated EPUB output")
    parser.add_argument("--gemini-api-key", required=True, help="Gemini API key")
    parser.add_argument("--gemini-model", default="gemini-2.0-flash", help="Gemini model to use")
    parser.add_argument("--chunk-size-chars", type=int, default=12000, help="Maximum chars per Gemini request")
    return parser.parse_args()


def call_gemini(api_key: str, model: str, text: str) -> List[Dict[str, str]]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    prompt = f"""
You are a careful literary editor. Read the following excerpt from an EPUB chapter and identify unfamiliar or archaic words, names, or phrases that a modern English reader may not know.

Return ONLY valid JSON in this exact shape:
[
  {"term": "word or phrase", "explanation": "plain-English explanation"},
  {"term": "another word", "explanation": "plain-English explanation"}
]

Requirements:
- Prefer words and phrases that are genuinely difficult for a modern general reader.
- Avoid common everyday vocabulary.
- Keep explanations concise, useful, and accessible.
- Use no markdown fences.
- If no terms are difficult, return []

Excerpt:
{text}
"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "topP": 0.9},
    }

    data = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")

    try:
        with request.urlopen(req, timeout=180) as resp:
            body = resp.read().decode("utf-8")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini API request failed: {exc.code} {detail}") from exc

    response = json.loads(body)
    candidates = response.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Gemini returned no candidates: {body}")

    text_response = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
    if not text_response.strip():
        return []

    cleaned = text_response.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("` ")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()

    match = re.search(r"\[[\s\S]*\]", cleaned)
    if match:
        cleaned = match.group(0)

    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Gemini returned invalid JSON: {cleaned[:500]}") from exc

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


def chunk_text(text: str, size: int) -> List[str]:
    if len(text) <= size:
        return [text]
    chunks: List[str] = []
    for start in range(0, len(text), size):
        chunks.append(text[start:start + size])
    return chunks


def normalize_annotation(term: str) -> str:
    cleaned = re.sub(r"\s+", " ", term.strip())
    return cleaned.strip(".,;:!?()[]{}\"'\n")


def add_footnotes_to_html(html: str, annotations: List[Dict[str, str]]) -> str:
    if not annotations:
        return html

    soup = BeautifulSoup(html, "html.parser")
    body = soup.body or soup

    unique_terms: List[Dict[str, str]] = []
    seen = set()
    for ann in annotations:
        term = normalize_annotation(ann["term"])
        if not term or term.lower() in seen:
            continue
        seen.add(term.lower())
        unique_terms.append({"term": term, "explanation": ann["explanation"]})

    if not unique_terms:
        return html

    for idx, ann in enumerate(unique_terms, start=1):
        note_id = f"epub-note-{idx}"
        term = ann["term"]
        explanation = ann["explanation"]

        pattern = re.compile(rf"(?<![A-Za-z]){re.escape(term)}(?![A-Za-z])", re.IGNORECASE)
        body_text = str(body)
        body_text = pattern.sub(lambda m: f"{m.group(0)}<sup class=\"epub-annotation-ref\"><a href=\"#{note_id}\">[{idx}]</a></sup>", body_text)
        body.clear()
        body.append(BeautifulSoup(body_text, "html.parser"))

        # Rebuild a proper notes section at end of body
        notes_div = soup.new_tag("section")
        notes_div["class"] = "epub-annotator-footnotes"
        notes_h2 = soup.new_tag("h2")
        notes_h2.string = "Footnotes"
        notes_div.append(notes_h2)

        notes_list = soup.new_tag("ol")
        notes_list["class"] = "epub-annotator-footnotes-list"
        item = soup.new_tag("li")
        item["id"] = note_id
        strong = soup.new_tag("strong")
        strong.string = term
        item.append(strong)
        item.append(" — " + explanation)
        notes_list.append(item)
        notes_div.append(notes_list)
        body.append(notes_div)

    # Re-render a stable HTML page once all replacements are done
    return str(soup)


def extract_epub_text_and_paths(epub_path: Path) -> Dict[str, str]:
    with zipfile.ZipFile(epub_path, "r") as archive:
        html_files = [
            name for name in archive.namelist()
            if name.lower().endswith((".xhtml", ".html", ".htm"))
        ]
        if not html_files:
            raise FileNotFoundError(f"No HTML/XHTML files found in {epub_path}")

        pages: Dict[str, str] = {}
        for name in sorted(html_files):
            content = archive.read(name)
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                text = content.decode("utf-8", errors="replace")
            pages[name] = text
        return pages


def save_modified_epub(epub_path: Path, output_path: Path, html_updates: Dict[str, str]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(epub_path, "r") as zin:
        names = zin.namelist()
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            for name in names:
                if name in html_updates:
                    zout.writestr(name, html_updates[name].encode("utf-8"))
                else:
                    zout.writestr(name, zin.read(name))


def main() -> int:
    args = read_input()
    epub_path = Path(args.epub_path)
    output_path = Path(args.output_path)

    if not epub_path.exists():
        raise FileNotFoundError(f"EPUB file not found at {epub_path}")

    html_pages = extract_epub_text_and_paths(epub_path)
    annotations_by_page: Dict[str, List[Dict[str, str]]] = {}

    for page_name, page_html in html_pages.items():
        soup = BeautifulSoup(page_html, "html.parser")
        body = soup.body or soup
        visible_text = body.get_text(" ", strip=True)
        if not visible_text:
            continue

        chunks = chunk_text(visible_text, args.chunk_size_chars)
        page_annotations: List[Dict[str, str]] = []
        for chunk in chunks:
            page_annotations.extend(call_gemini(args.gemini_api_key, args.gemini_model, chunk))

        deduped: List[Dict[str, str]] = []
        seen = set()
        for ann in page_annotations:
            term = normalize_annotation(ann["term"])
            if not term:
                continue
            key = term.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append({"term": term, "explanation": ann["explanation"]})

        annotations_by_page[page_name] = deduped

    updated_pages: Dict[str, str] = {}
    for page_name, page_html in html_pages.items():
        updated_pages[page_name] = add_footnotes_to_html(page_html, annotations_by_page.get(page_name, []))

    save_modified_epub(epub_path, output_path, updated_pages)
    print(f"Annotated EPUB written to: {output_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
