"""Read-only drawing references and original PDF locations for this workspace.

No vector stores, retriever, keyword-index connections or model configuration are
opened. Every list, metadata and image request checks the existing KB permissions.
"""
from __future__ import annotations

from collections import defaultdict
from functools import lru_cache
import hashlib
from pathlib import Path
import re
import unicodedata

from django.http import Http404
from django.db.models import Q, Case, When, Value, IntegerField

MAX_DOCUMENTS = 80
MAX_PAGES = 500


def drawing_code(value):
    if not isinstance(value, str):
        raise ValueError("请输入有效图号。")
    value = unicodedata.normalize("NFKC", value).strip().upper()
    # Preserve the printed label in the UI; an assembly item suffix is not part
    # of its drawing number (e.g. SDLDWARMV0822 Item 50).
    value = re.sub(r"\s+ITEM\s*\d+\s*$", "", value)
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{2,39}", value):
        raise ValueError("图号仅支持3–40位字母、数字和短横线。")
    return value


def visible_documents(user):
    from kb.models import Document
    from kb import access
    qs = Document.objects.select_related("kb").filter(status=Document.Status.COMPLETED)
    qs = qs.filter(access.kb_q(user,'kb__',admin_all=True))
    return qs


def document_for(user, doc_id):
    from kb import access
    doc = visible_documents(user).filter(pk=doc_id).first()
    if not doc or not access.kb_accessible(user, doc.kb):
        raise Http404("文档不存在或无权查看。")
    return doc


def pdf_path(doc):
    if doc.file_type != "pdf" or not doc.file or not doc.file.name:
        return None
    try:
        path = Path(doc.file.path)
        return path if path.is_file() else None
    except (NotImplementedError, ValueError, OSError):
        return None


def file_version(path):
    stat = path.stat()
    return hashlib.sha256(f"{stat.st_mtime_ns}:{stat.st_size}".encode()).hexdigest()[:16]


def match_pattern(code):
    return re.compile(r"(?<![A-Z0-9])" + re.escape(code) + r"(?![A-Z0-9])", re.I)


def normalized_box(rect, page):
    import pymupdf
    rect = pymupdf.Rect(rect) * page.rotation_matrix
    rect = rect & page.rect
    if rect.is_empty or page.rect.width <= 0 or page.rect.height <= 0:
        return None
    return [round(rect.x0 / page.rect.width * 1000, 2), round(rect.y0 / page.rect.height * 1000, 2),
            round(rect.x1 / page.rect.width * 1000, 2), round(rect.y1 / page.rect.height * 1000, 2)]


@lru_cache(maxsize=48)
def scan_pdf(path_name, version, code):
    import pymupdf
    pattern = match_pattern(code)
    occurrences = []
    with pymupdf.open(path_name) as pdf:
        page_count = len(pdf)
        for number in range(min(page_count, MAX_PAGES)):
            page = pdf[number]
            boxes, quotes = [], []
            for block in page.get_text("rawdict").get("blocks", []):
                for line in block.get("lines", []):
                    chars = [char for span in line.get("spans", []) for char in span.get("chars", [])]
                    text = "".join(char.get("c", "") for char in chars)
                    for match in pattern.finditer(text):
                        selected = chars[match.start():match.end()]
                        if not selected:
                            continue
                        rect = pymupdf.Rect(selected[0]["bbox"])
                        for char in selected[1:]:
                            rect |= pymupdf.Rect(char["bbox"])
                        rect.x0 -= 1;rect.y0 -= 1;rect.x1 += 1;rect.y1 += 1
                        bbox = normalized_box(rect, page)
                        if bbox:
                            boxes.append(bbox)
                            quotes.append(text[max(0, match.start()-80):match.end()+100])
            if boxes:
                occurrences.append({"page": number, "boxes": boxes[:30], "precision": "text",
                                    "quote": " · ".join(dict.fromkeys(quotes))[:500]})
    return {"page_count": page_count, "occurrences": occurrences,
            "limited_pages": page_count > MAX_PAGES, "version": version}


def parsed_locations(doc, code, page_count, native_pages):
    # The existing provenance loader reads MinerU JSON only. It never opens a
    # vector store or changes the retrieval configuration.
    from kb.provenance import load_content_list, valid_bbox
    pattern = match_pattern(code)
    grouped = defaultdict(list)
    try:
        items = load_content_list(str(doc.id)) or []
    except (OSError, ValueError, TypeError):
        items = []
    for item in items:
        if not isinstance(item, dict):
            continue
        page = item.get("page_idx")
        if type(page) is not int or not 0 <= page < page_count or page in native_pages:
            continue
        values = [item.get(key, "") for key in ("text", "table_body", "table_caption", "img_caption")]
        text = " ".join(value if isinstance(value, str) else " ".join(str(x) for x in value) if isinstance(value, list) else "" for value in values)
        bbox = item.get("bbox")
        if pattern.search(text) and valid_bbox(bbox):
            grouped[page].append({"bbox": bbox, "text": re.sub(r"<[^>]*>", " ", text)[:500]})
    return [{"page": page, "boxes": [row["bbox"] for row in rows[:30]], "precision": "region",
             "quote": rows[0]["text"]} for page, rows in sorted(grouped.items())]


def locations(doc, code):
    path = pdf_path(doc)
    if not path:
        return {"page_count": 0, "occurrences": [], "limited_pages": False, "version": "", "has_pdf": False}
    try:
        data = scan_pdf(str(path), file_version(path), code)
        data = {**data, "occurrences": [dict(row) for row in data["occurrences"]], "has_pdf": True}
        native = {row["page"] for row in data["occurrences"]}
        data["occurrences"] += parsed_locations(doc, code, data["page_count"], native)
        data["occurrences"].sort(key=lambda row: row["page"])
        return data
    except (OSError, RuntimeError, ValueError):
        return {"page_count": 0, "occurrences": [], "limited_pages": False, "version": "", "has_pdf": False}


def search(user, value):
    code = drawing_code(value)
    qs = visible_documents(user)
    # PDFs can contain native drawing text even when their old OCR text lacks it.
    qs = qs.filter(Q(file_type="pdf") | Q(md_content__icontains=code) | Q(original_name__icontains=code))
    total = qs.count()
    items = []
    qs = qs.annotate(drawing_priority=Case(
        When(Q(md_content__icontains=code) | Q(original_name__icontains=code), then=Value(0)),
        default=Value(1), output_field=IntegerField()))
    for doc in qs.order_by("drawing_priority", "original_name", "id")[:MAX_DOCUMENTS]:
        data = locations(doc, code)
        match = match_pattern(code).search(doc.md_content or "")
        filename_match = match_pattern(code).search(doc.original_name)
        if not data["occurrences"] and not match and not filename_match:
            continue
        excerpt = (doc.md_content or "")[max(0, match.start()-80):match.end()+180] if match else ""
        items.append({"id": str(doc.id), "name": doc.original_name, "knowledge_base": doc.kb.name,
                      **data, "excerpt": excerpt, "hit_count": sum(len(row["boxes"]) for row in data["occurrences"])})
    return {"drawing": code, "documents": items, "scanned_documents": min(total, MAX_DOCUMENTS),
            "total_documents": total, "limited": total > MAX_DOCUMENTS}


def page_metadata(user, doc_id, code, number, expected_version=""):
    doc = document_for(user, doc_id)
    data = locations(doc, drawing_code(code))
    if not data["has_pdf"]:
        raise Http404("原PDF不可用。")
    if expected_version and expected_version != data["version"]:
        raise ValueError("原文已更新，请刷新图号出处后重新定位。")
    if not 0 <= number < data["page_count"]:
        raise ValueError("页码超出原PDF范围。")
    match = next((row for row in data["occurrences"] if row["page"] == number), None)
    return {"id": str(doc.id), "name": doc.original_name, "page": number, "page_count": data["page_count"],
            "boxes": match["boxes"] if match else [], "precision": match["precision"] if match else "none",
            "quote": match["quote"] if match else "此页未匹配到该图号，可查看相邻页的上下文。",
            "version": data["version"]}


def render_page(user, doc_id, number, expected_version=""):
    import pymupdf
    doc = document_for(user, doc_id)
    path = pdf_path(doc)
    if not path:
        raise Http404("原PDF不可用。")
    if expected_version and expected_version != file_version(path):
        raise ValueError("原文已更新，请刷新出处后重试。")
    with pymupdf.open(path) as pdf:
        if not 0 <= number < len(pdf):
            raise ValueError("页码超出原PDF范围。")
        page = pdf[number]
        scale = min(2, 2400 / max(page.rect.width, page.rect.height, 1))
        return page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes("png")
