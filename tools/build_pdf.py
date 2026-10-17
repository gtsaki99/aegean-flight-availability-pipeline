"""Build WRITEUP.pdf from docs/WRITEUP.md, with the profiling and demo reports as appendices.

Packaging tool, not part of the pipeline: needs `markdown-it-py` (CommonMark, the same rules as
GitHub and most Markdown viewers) and Microsoft Edge (headless printing), neither of which is
required to run the project.
    python tools/build_pdf.py
"""
import re
import subprocess
from pathlib import Path

from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parent.parent
DOCS, OUTPUT = ROOT / "docs", ROOT / "output"
MD = MarkdownIt("commonmark", {"html": True}).enable("table")
AUTHOR = "Georgios Tsakiridis"
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")

CSS = """
@page { size: A4; margin: 16mm 15mm 16mm 15mm; }
body { font-family: "Segoe UI", system-ui, sans-serif; font-size: 10pt; line-height: 1.45; color: #0b0b0b; }
h1 { font-size: 20pt; margin: 0 0 4pt; }
h2 { font-size: 14pt; margin: 18pt 0 6pt; border-bottom: 1px solid #e1e0d9; padding-bottom: 3pt; }
h3 { font-size: 11.5pt; margin: 14pt 0 4pt; }
p, li { margin: 4pt 0; }
table { border-collapse: collapse; margin: 6pt 0 10pt; font-size: 8.6pt; width: 100%; page-break-inside: auto; }
tr { page-break-inside: avoid; }
th { text-align: left; background: #f3f2ee; }
th, td { border: 1px solid #e1e0d9; padding: 3pt 5pt; vertical-align: top; }
code { font-family: Consolas, monospace; font-size: 8.8pt; background: #f3f2ee; padding: 0 2pt; border-radius: 2pt; }
pre { background: #f3f2ee; padding: 6pt; font-size: 8.5pt; }
img { max-width: 100%; }
hr { border: 0; border-top: 1px solid #e1e0d9; margin: 14pt 0; }
.appendix { page-break-before: always; }
.appendix h1 { font-size: 13pt; }
.appendix h2 { font-size: 11.5pt; }
.appendix table { font-size: 7.8pt; }
"""


def appendix(title, path):
    """A report as an appendix: its own headings demoted one level."""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"^# .*\n", "", text, count=1)
    text = re.sub(r"^(#+) ", lambda m: "#" + m.group(1) + " ", text, flags=re.M)
    body = MD.render(text)
    return f'<section class="appendix"><h2>{title}</h2>{body}</section>'


def main():
    text = (DOCS / "WRITEUP.md").read_text(encoding="utf-8")
    main_part = text.split("## Appendix A")[0]
    html_body = MD.render(main_part)
    html_body += appendix("Appendix A: Profiling report", OUTPUT / "profiling_report.md")
    html_body += appendix("Appendix B: Demo report", OUTPUT / "demo_report.md")
    # keep ISO dates on one line in table cells
    html_body = re.sub(r"<td>(\d{4}-\d{2}-\d{2})</td>", r'<td style="white-space:nowrap">\1</td>', html_body)
    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>Daily flights pipeline and aircraft availability</title>
<style>{CSS}</style></head><body>{html_body}</body></html>"""
    page = DOCS / "WRITEUP.html"          # image paths in WRITEUP.md are relative to docs/
    page.write_text(html, encoding="utf-8")
    target = ROOT / "WRITEUP.pdf"
    subprocess.run([str(EDGE), "--headless", "--disable-gpu", "--no-pdf-header-footer",
                    f"--print-to-pdf={target}", page.as_uri()], check=True, capture_output=True)
    page.unlink()
    try:                                   # document properties (optional: needs PyMuPDF)
        import fitz
        doc = fitz.open(target)
        doc.set_metadata({**doc.metadata, "author": AUTHOR,
                          "title": "Daily flights pipeline and aircraft availability",
                          "subject": "Aegean data engineering exercise"})
        doc.saveIncr()
    except ImportError:
        pass
    print(f"written: {target} ({target.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
