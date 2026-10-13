"""
Markdown → Word 转换（够用就好，不追求排版完美）

跑法：
    python scripts/md_to_docx.py 输入.md [输出.docx]

支持：
    # / ## / ### 标题、| 表格 |、``` 代码块、- 列表、段落、**粗体**、`代码`
不支持（遇到会当普通段落处理）：嵌套列表、图片、HTML 标签。

为什么自己写而不装 pandoc：pandoc 是外部二进制，服务器与本机都要单独装；
本项目的交付物只需要「能看、能投递、能打印」，python-docx 足够。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor


def _set_cjk(doc: Document) -> None:
    """正文与标题都指定中文字体，否则 Word 里中文会回退成宋体以外的怪字体。"""
    style = doc.styles["Normal"]
    style.font.name = "Microsoft YaHei"
    style.font.size = Pt(10.5)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    style.paragraph_format.space_after = Pt(4)
    style.paragraph_format.line_spacing = 1.35


def _add_rich(par, text: str):
    """处理 **粗体** 与 `代码`，其余按普通文字。"""
    for seg in re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", text):
        if not seg:
            continue
        if seg.startswith("**") and seg.endswith("**"):
            r = par.add_run(seg[2:-2])
            r.bold = True
        elif seg.startswith("`") and seg.endswith("`"):
            r = par.add_run(seg[1:-1])
            r.font.name = "Consolas"
            r.font.size = Pt(9.5)
            r.font.color.rgb = RGBColor(0x8A, 0x3C, 0x1D)
        else:
            par.add_run(seg)


def convert(md_path: str, out_path: str | None = None) -> str:
    md = Path(md_path)
    out = Path(out_path) if out_path else md.with_suffix(".docx")

    doc = Document()
    _set_cjk(doc)
    for s in doc.sections:
        s.left_margin = s.right_margin = Pt(48)

    lines = md.read_text(encoding="utf-8").splitlines()
    i, n = 0, len(lines)
    in_code = False
    code_buf: list[str] = []

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # 代码块
        if stripped.startswith("```"):
            if in_code:
                p = doc.add_paragraph()
                p.paragraph_format.left_indent = Pt(14)
                p.paragraph_format.space_after = Pt(6)
                r = p.add_run("\n".join(code_buf))
                r.font.name = "Consolas"
                r.font.size = Pt(9)
                r.font.color.rgb = RGBColor(0x22, 0x22, 0x22)
                in_code, code_buf = False, []
            else:
                in_code = True
                code_buf = []
            i += 1
            continue
        if in_code:
            code_buf.append(line)
            i += 1
            continue

        # 空行
        if not stripped:
            i += 1
            continue

        # 表格：当前行是 |...| 且下一行是分隔行
        if stripped.startswith("|") and i + 1 < n and re.match(r"^\|[\s:\-|]+\|$", lines[i + 1].strip()):
            rows: list[list[str]] = []
            while i < n and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not re.match(r"^[\s:\-]+$", "".join(cells)):
                    rows.append(cells)
                i += 1
            if rows:
                cols = max(len(r) for r in rows)
                t = doc.add_table(rows=len(rows), cols=cols)
                t.style = "Light Grid Accent 1"
                t.alignment = WD_TABLE_ALIGNMENT.CENTER
                for ri, row in enumerate(rows):
                    for ci in range(cols):
                        cell = t.cell(ri, ci)
                        cell.text = ""
                        p = cell.paragraphs[0]
                        p.paragraph_format.space_after = Pt(2)
                        _add_rich(p, row[ci] if ci < len(row) else "")
                        if ri == 0:
                            for r in p.runs:
                                r.bold = True
                doc.add_paragraph()
            continue

        # 引用
        if stripped.startswith(">"):
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Pt(16)
            _add_rich(p, stripped.lstrip("> ").strip())
            for r in p.runs:
                r.font.color.rgb = RGBColor(0x5F, 0x5E, 0x5A)
            i += 1
            continue

        # 标题
        m = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if m:
            level = len(m.group(1))
            h = doc.add_heading(level=min(level, 4))
            _add_rich(h, m.group(2))
            i += 1
            continue

        # 列表
        if re.match(r"^[-*]\s+", stripped):
            p = doc.add_paragraph(style="List Bullet")
            p.paragraph_format.left_indent = Pt(18)
            _add_rich(p, re.sub(r"^[-*]\s+", "", stripped))
            i += 1
            continue
        if re.match(r"^\d+[.)]\s+", stripped):
            p = doc.add_paragraph(style="List Number")
            p.paragraph_format.left_indent = Pt(18)
            _add_rich(p, re.sub(r"^\d+[.)]\s+", "", stripped))
            i += 1
            continue

        # 分隔线
        if re.match(r"^-{3,}$", stripped):
            doc.add_paragraph("─" * 40).alignment = WD_ALIGN_PARAGRAPH.CENTER
            i += 1
            continue

        # 普通段落
        p = doc.add_paragraph()
        _add_rich(p, stripped)
        i += 1

    doc.save(str(out))
    return str(out)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    print("已生成:", convert(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
