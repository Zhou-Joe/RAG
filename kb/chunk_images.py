"""Locate bounded, document-owned images associated with an already read chunk.

Text embeddings omit image markup.  Replaying the same cleaner with invisible
position markers lets old indexed chunks retain their original figure links,
without treating every image on the same PDF page as part of a text chunk.
This is a candidate directory only: callers still need to read an image before
using its pixels, and explicitly select it before publishing an image citation.
"""
from __future__ import annotations

import html
import json
import re
from html.parser import HTMLParser

MAX_LINKED_IMAGES = 8
_MIN_ANCHOR = 16
_MARKUP_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)|<img\s[^>]*>", re.IGNORECASE)
_MD_RE = re.compile(r"!\[([^\]]*)\]\((.*)\)", re.DOTALL)
_MD_TARGET_RE = re.compile(r'''\s*(?:<([^<>]+)>|([^\s]+?))(?:\s+(?:"[^"]*"|'[^']*'))?\s*''')
_NAME = r"[0-9a-f]{32}(?:[0-9a-f]{32})?\.(?:jpg|jpeg|png|webp)"
_SECTION_PREFIX_RE = re.compile(r"^【[^】]*】\s*\n?")


class _ImageTag(HTMLParser):
    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.src = self.alt = ""
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "img":
            return
        sources = [value for key, value in attrs if key.lower() == "src"]
        if len(sources) == 1:
            self.src = sources[0] or ""
        self.alt = next((value or "" for key, value in attrs if key.lower() == "alt"), "")


def _image_name(value, document):
    """Accept only exact local storage paths or this document's protected URL."""
    if not isinstance(value, str):
        return None
    value = html.unescape(value).strip()
    for prefix in ("images/", f"/kb/doc/{document.pk}/img/"):
        if value.startswith(prefix):
            name = value[len(prefix):]
            if re.fullmatch(_NAME, name, re.IGNORECASE):
                return name
    return None


def _references(text, document):
    for match in _MARKUP_RE.finditer(text):
        markup = match.group(0)
        parsed = _MD_RE.fullmatch(markup)
        if parsed:
            target = _MD_TARGET_RE.fullmatch(parsed.group(2))
            src = (target.group(1) or target.group(2)) if target else ""
            caption = parsed.group(1)
        else:
            tag = _ImageTag(markup)
            src, caption = tag.src, tag.alt
        name = _image_name(src, document)
        yield {"start": match.start(), "end": match.end(), "name": name, "caption": caption}


def _caption(value):
    if isinstance(value, (list, tuple)):
        value = " ".join(item for item in value if isinstance(item, str))
    if not isinstance(value, str):
        return ""
    value = _MARKUP_RE.sub("", value)
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()[:240]


def _nearby(body, offset):
    # Remove complete references before shortening, so a cut through a long
    # filename cannot turn part of its markup into apparent caption text.
    before = _MARKUP_RE.sub("", body[:offset])
    after = _MARKUP_RE.sub("", body[offset:])
    return _caption(before[-120:] + " " + after[:120])


def _source_links(document, body):
    """Recover original image positions after the indexing cleaner stripped refs.

    Private-use markers contribute no characters to the provenance signature.
    Require identical cleaned signatures and a unique whole-chunk alignment;
    a repeated boilerplate passage must never borrow another passage's images.
    """
    from .pipeline import _md_for_embedding
    from .provenance import _SigIndex, _sig

    source = document.md_content or ""
    refs = list(_references(source, document))
    if not any(ref["name"] for ref in refs):
        return []
    needle = _sig(body)
    if len(needle) < _MIN_ANCHOR:
        return []
    try:
        clean = _md_for_embedding(source)
    except (TypeError, ValueError):
        return []
    signature = _sig(clean)
    start = signature.find(needle)
    if start < 0 or signature.find(needle, start + 1) >= 0:
        return []
    end = start + len(needle)

    # Choose a delimiter absent from source, including after entity unescaping.
    delimiter = "\ue000"
    while delimiter in html.unescape(source):
        delimiter += "\ue000"
    marked, cursor, markers = [], 0, {}
    for number, ref in enumerate(refs):
        marked.append(source[cursor:ref["start"]])
        if ref["name"]:
            marker = f"{delimiter}{chr(0xE100 + number % 256)}{delimiter}"
            # More than 256 image tags require an unambiguous longer marker.
            marker += chr(0xE200 + number // 256)
            markers[marker] = ref
            marked.append(marker)
        cursor = ref["end"]
    marked.append(source[cursor:])
    try:
        transformed = _md_for_embedding("".join(marked))
    except (TypeError, ValueError):
        return []
    if _sig(transformed) != signature:
        return []

    chunk_index = _SigIndex(body)
    located = []
    # Markers may repeat when HTML rowspan cells are expanded into text rows.
    # Each occurrence still refers to the same grounded original source image.
    pattern = re.compile("|".join(re.escape(marker) for marker in markers))
    sig_cursor, last = 0, 0
    for match in pattern.finditer(transformed):
        sig_cursor += len(_sig(transformed[last:match.start()]))
        last = match.end()
        if not start <= sig_cursor <= end:
            continue
        ref = markers[match.group(0)]
        relative = sig_cursor - start
        char_offset = chunk_index.pos[min(relative, len(chunk_index.pos) - 1)]
        located.append(dict(ref,
            relation="embedded_reference" if start < sig_cursor < end else "adjacent_reference",
            context=_nearby(body, char_offset),
            position={"basis": "document_markdown", "start": ref["start"], "end": ref["end"]},
        ))
    return located


def _content_list_links(document, body, content_list):
    """Find extraction-only images strictly bracketed by text in the read chunk.

    Page association alone is insufficient. Both adjacent extraction text blocks
    must be fully present, in order, within the already read chunk.
    """
    from .provenance import _block_locate_text, _sig

    signature = _sig(body)
    anchors = []
    for index, block in enumerate(content_list):
        if not isinstance(block, dict):
            continue
        try:
            located = _block_locate_text(block)
        except (AttributeError, TypeError, ValueError):
            continue
        if located and len(anchor := _sig(located[0])) >= _MIN_ANCHOR:
            anchors.append((index, anchor))
    result = []
    for index, block in enumerate(content_list):
        if not isinstance(block, dict) or block.get("type") not in ("image", "chart"):
            continue
        name = _image_name(block.get("img_path"), document)
        if not name:
            continue
        before = next((text for pos, text in reversed(anchors) if pos < index), None)
        after = next((text for pos, text in anchors if pos > index), None)
        if not before or not after:
            continue
        left = signature.find(before)
        if left < 0 or signature.find(before, left + 1) >= 0:
            continue
        right = signature.find(after, left + len(before))
        if right < 0 or signature.find(after, right + 1) >= 0:
            continue
        result.append({"name": name,
            "caption": _caption(block.get("image_caption") or block.get("chart_caption")),
            "context": _caption(body), "relation": "content_list_sequence",
            "position": {"basis": "content_list", "block_index": index},
        })
    return result


def _metadata_digest(blocks):
    """Bind only the captions/pages supplied by this image's extraction rows."""
    from .publication import digest

    relevant = [{"caption": _caption(block.get("image_caption") or block.get("chart_caption")),
        "page": block["page_idx"] if type(block.get("page_idx")) is int and block["page_idx"] >= 0 else None}
        for block in blocks]
    return digest(json.dumps(relevant, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def image_metadata_current(document, row):
    """Check a linked image's caption/page stamp, independently of file bytes.

    Rows without a stamp are not validated by this helper. Legacy image callers
    can keep their existing file validation and invoke this only when the stamp
    is present. Unrelated images, prose blocks, and unused layout boxes cannot
    invalidate an image's caption/page snapshot.
    """
    from .provenance import load_content_list

    name, checksum = row.get("image"), row.get("image_metadata_digest")
    if not isinstance(name, str) or not re.fullmatch(_NAME, name, re.IGNORECASE) or not checksum:
        return False
    if ((row.get("doc_id") and str(row["doc_id"]) != str(document.pk))
            or (row.get("kb_slug") and row["kb_slug"] != document.kb.slug)):
        return False
    blocks = [block for block in load_content_list(document.pk) or []
        if isinstance(block, dict) and block.get("type") in ("image", "chart")
        and _image_name(block.get("img_path"), document) == name]
    return _metadata_digest(blocks) == checksum


def linked_images(document, row, limit=MAX_LINKED_IMAGES):
    """Return verified image candidate rows for one already read text chunk.

    This function assumes the caller has checked document access and the chunk
    snapshot. It does not read pixels or add evidence/citations. File digests are
    checked here for availability and must be checked again on image read and
    publication, using the existing image snapshot validation.
    """
    from .provenance import load_content_list
    from .publication import digest, image_source_digest

    try:
        limit = min(MAX_LINKED_IMAGES, max(0, int(limit)))
    except (TypeError, ValueError, OverflowError):
        return []
    text = row.get("text")
    if not limit or not isinstance(text, str) or not text or row.get("type") == "image":
        return []
    if ((row.get("doc_id") and str(row["doc_id"]) != str(document.pk))
            or (row.get("kb_slug") and row["kb_slug"] != document.kb.slug)
            or (row.get("source") and row["source"] != document.original_name)):
        return []
    body = _SECTION_PREFIX_RE.sub("", text, count=1)
    content_list = load_content_list(document.pk) or []
    metadata = {}
    for block in content_list:
        if not isinstance(block, dict) or block.get("type") not in ("image", "chart"):
            continue
        name = _image_name(block.get("img_path"), document)
        if name:
            metadata.setdefault(name, []).append(block)

    direct = [dict(ref, relation="embedded_reference",
        context=_nearby(text, ref["start"]),
        position={"basis": "chunk_text", "start": ref["start"], "end": ref["end"]})
        for ref in _references(text, document) if ref["name"]]
    candidates = direct + _source_links(document, body) + _content_list_links(document, body, content_list)
    rows, seen = [], set()
    for candidate in candidates:
        name = candidate["name"]
        if name in seen:
            continue
        seen.add(name)
        if not image_source_digest(document, name):
            continue
        blocks = metadata.get(name, [])
        captions = [_caption(block.get("image_caption") or block.get("chart_caption")) for block in blocks]
        caption = _caption(candidate.get("caption")) or next((cap for cap in captions if cap), "")
        item = {"doc_id": str(document.pk), "source": document.original_name,
            "kb_slug": document.kb.slug, "type": "image", "image": name,
            "chunk_id": f"img-{document.pk}-{name}",
            "text": caption or candidate["context"] or "Document image",
            "relation": candidate["relation"], "position": candidate["position"],
            "parent_chunk_id": row.get("chunk_id") or "",
            "parent_chunk_digest": digest(text),
            "image_metadata_digest": _metadata_digest(blocks),
        }
        pages = {block["page_idx"] for block in blocks
            if type(block.get("page_idx")) is int and block["page_idx"] >= 0}
        if len(pages) == 1:
            page = pages.pop()
            item.update(page_start=page, page_end=page, page_label=f"第 {page + 1} 页")
        rows.append(item)
        if len(rows) >= limit:
            break
    return rows
