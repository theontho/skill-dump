---
name: web-to-markdown
description: High-quality conversion of web pages or mirrored HTML sites to clean Markdown. Use when the user wants to preserve document structure like blockquotes and relative links while stripping boilerplate (nav, scripts, styles).
---

# Web to Markdown Skill

This skill converts HTML files (individually or in batches) into clean, high-quality Markdown. It is designed to handle common website structures by detecting indentation-based quotes and stripping away navigation, sidebars, and scripts.

## Core Features

- **Boilerplate Removal**: Automatically strips <nav>, <header>, <footer>, and <script> tags.
- **Smart Blockquotes**: Detects CSS-indented paragraphs (e.g., margin-left: 36pt) and converts them into Markdown blockquotes (> ).
- **Link Preservation**: Maintains internal document links while converting targets from .html to .md.
- **Batch Processing**: Recursively converts entire directories of HTML files, preserving folder structure.

## Usage

### Converting a Single File
python3 scripts/convert_web_to_md.py <input.html> [--output <output.md>]

### Converting a Directory
python3 scripts/convert_web_to_md.py <html-directory/> [--output <md-directory/>]

## Dependencies
- **Pandoc**: Must be installed on the system.
- **BeautifulSoup4**: Required for HTML cleaning. Install via pip install beautifulsoup4.
