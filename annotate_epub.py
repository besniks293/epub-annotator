# epub-annotator

A GitHub Action that takes an EPUB, extracts its HTML/XHTML content, sends chapter text chunks to Gemini, and injects modern-reader footnotes for unfamiliar words and phrases.

## What it does

- Reads an EPUB from your repo
- Extracts the HTML/XHTML chapters
- Sends chapter text to Gemini
- Asks Gemini to identify uncommon or archaic words/phrases that a modern reader may not know
- Adds footnote references and a note section to each HTML chapter
- Rebuilds a new EPUB file with the annotations embedded

## Required inputs

- `epub_path`: Path to the source EPUB file
- `gemini_api_key`: Your Gemini API key

Optional inputs:

- `output_path`: Output file path, default: `annotated.epub`
- `gemini_model`: Gemini model, default: `gemini-2.0-flash`
- `chunk_size_chars`: Character limit per Gemini call, default: `12000`

## Example workflow

```yaml
name: Annotate EPUB

on:
  workflow_dispatch:
    inputs:
      epub_path:
        description: Path to your EPUB in the repo
        required: true
        default: books/sample.epub
      output_path:
        description: Output filename
        required: true
        default: books/sample-annotated.epub

jobs:
  annotate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Annotate EPUB with Gemini
        uses: besniks293/epub-annotator@main
        with:
          epub_path: ${{ github.workspace }}/${{ inputs.epub_path }}
          output_path: ${{ github.workspace }}/${{ inputs.output_path }}
          gemini_api_key: ${{ secrets.GEMINI_API_KEY }}
```

## Example using the repo directly

```bash
python annotate_epub.py \
  --epub-path books/sample.epub \
  --output-path books/sample-annotated.epub \
  --gemini-api-key "$GEMINI_API_KEY"
```

## Notes

This works best for EPUBs that are plain XHTML/HTML under the hood. It uses Gemini to generate explanatory notes, but it does not create a reader-specific annotation format like a PDF comment layer.

The action is intentionally conservative: it asks Gemini to return only unfamiliar terms and short, readable explanations, and it keeps the generated notes easy to understand for modern readers.

## Notes about the prompt

The model is prompted to produce terms like:

- archaic words
- literary vocabulary
- uncommon historical references
- field-specific phrases
- unusual idioms

The generated notes are then inserted as footnotes in the EPUB chapter HTML.

## Future enhancements

Potential upgrades include:

- preserving chapter order more precisely
- smarter whitespace/markup handling for complex EPUBs
- per-section prompts to target specific genres or reading levels
- generating a separate glossary page instead of inline notes
- adding a `notes_only` mode that emits a glossary without modifying the chapter HTML

```