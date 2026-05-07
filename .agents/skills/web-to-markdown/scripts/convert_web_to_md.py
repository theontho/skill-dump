import os
import re
import sys
import argparse
import subprocess
from pathlib import Path

try:
    from bs4 import BeautifulSoup
except ImportError:
    # LLM-friendly error
    print("Dependency missing: beautifulsoup4. Please install it to use this script.")
    sys.exit(1)

def check_pandoc():
    """Check if pandoc is installed and available."""
    try:
        subprocess.run(["pandoc", "--version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        print("Dependency missing: pandoc. Please install it to use this script.")
        print("  macOS: brew install pandoc")
        print("  Ubuntu/Debian: sudo apt-get install pandoc")
        print("  Windows: choco install pandoc")
        sys.exit(1)

def get_indent_classes(soup):
    classes = set()
    for style in soup.find_all('style'):
        if style.string:
            matches = re.findall(r'\.(c\d+)\s*\{[^}]*margin-left\s*:\s*(?:3[0-9]|4[0-5])pt', style.string)
            classes.update(matches)
    return classes

def clean_and_format_html(html_content):
    soup = BeautifulSoup(html_content, 'html.parser')
    indent_classes = get_indent_classes(soup)
    for tag in soup(['script', 'style', 'header', 'footer', 'nav', 'iframe', 'noscript']):
        tag.decompose()
    content = soup.find('div', id=re.compile(r'content|main|article', re.I)) or soup.body
    if not content: return str(soup)
    all_p = content.find_all('p')
    current_bq = None
    for p in all_p:
        classes = p.get('class', [])
        is_indented = any(cls in indent_classes for cls in classes)
        if is_indented:
            if current_bq is None:
                current_bq = soup.new_tag('blockquote')
                p.replace_with(current_bq)
                current_bq.append(p)
            else:
                current_bq.append(p)
        else:
            current_bq = None
    for tag in content.find_all(True):
        tag.attrs = {k: v for k, v in tag.attrs.items() if k == 'href'}
    return str(content)

def convert_to_md(html_file, output_path):
    with open(html_file, 'r', encoding='utf-8') as f:
        cleaned_html = clean_and_format_html(f.read())
    temp_html = Path("temp_for_pandoc.html")
    temp_html.write_text(cleaned_html, encoding='utf-8')
    try:
        subprocess.run(["pandoc", str(temp_html), "-f", "html", "-t", "commonmark_x", "-o", str(output_path), "--wrap=none"], check=True)
    finally:
        if temp_html.exists(): temp_html.unlink()

def post_process_md(md_path):
    md_text = md_path.read_text(encoding='utf-8')
    md_text = md_text.replace('\\[', '[').replace('\\]', ']')
    md_text = re.sub(r'\{[.#][^}]+\}', '', md_text)
    md_text = re.sub(r'\n-- ', r'\n\n-- ', md_text)
    md_text = re.sub(r'<[^>]+>', '', md_text)
    md_path.write_text(md_text.strip(), encoding='utf-8')

def main():
    check_pandoc()
    parser = argparse.ArgumentParser(description="Convert HTML to high-quality Markdown.")
    parser.add_argument("input", help="HTML file or directory.")
    parser.add_argument("--output", help="Output file or directory.")
    args = parser.parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output) if args.output else None
    if input_path.is_file():
        if not output_path: output_path = input_path.with_suffix('.md')
        convert_to_md(input_path, output_path)
        post_process_md(output_path)
        print(f"Success: {input_path} -> {output_path}")
    elif input_path.is_dir():
        if not output_path: output_path = input_path.parent / (input_path.name + "-markdown")
        output_path.mkdir(parents=True, exist_ok=True)
        for html_file in input_path.rglob("*.html"):
            rel_path = html_file.relative_to(input_path)
            dest_md = (output_path / rel_path).with_suffix('.md')
            dest_md.parent.mkdir(parents=True, exist_ok=True)
            convert_to_md(html_file, dest_md)
            post_process_md(dest_md)
        print(f"Success: Converted directory to {output_path}")

if __name__ == "__main__":
    main()
