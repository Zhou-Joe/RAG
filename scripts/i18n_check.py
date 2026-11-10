"""Replicates static/js/interface_language.js translate() to find untranslated UI strings.

Renders pages, walks the DOM the same way FOSI18n.render() does (text nodes +
the same attribute list, honouring the protected selectors), and reports any
Chinese string that stays Chinese in EN mode.
"""
import glob
import html.parser
import json
import re
import sys

NUMERIC = {"count", "total", "current", "page", "pages", "index"}
ATTRS = ["title", "placeholder", "aria-label", "aria-description", "alt",
         "data-confirm", "data-confirm-title", "data-confirm-ok"]
# Mirrors protectedSelector in interface_language.js (tags + attrs + classes).
PROTECTED_TAGS = {"script", "style", "noscript", "code", "pre"}
PROTECTED_ATTRS = {"translate=no", "data-i18n-ignore", "contenteditable=true"}
PROTECTED_CLASSES = {
    "notranslate", "answer", "msg", "bubble", "rag-chat-message-content", "cx-user-text",
    "cx-answer", "cx-sample", "graph-live-excerpt", "vt-ai-text", "ev-quote", "slice-snip",
    "slice-content", "doc-body", "doc-viewer", "markdown-body", "kb-mono", "conv-title",
    "conv-meta", "dash-kb-name", "da-title", "da-meta", "slice-doc", "doc-name-text",
    "eval-q-text", "eval-text", "eval-kb",
}
# applyText/untranslated also skip text whose DIRECT parent is one of these.
SKIP_TEXT_PARENT = [("class", "kb-name"), ("attr", "data-i18n-preserve-text")]
CJK = re.compile(r"[\u3400-\u9fff]")


def normalize(v):
    return re.sub(r"\s+", " ", str(v if v is not None else "")).strip()


def escape(v):
    return re.sub(r"([.*+?^${}()|[\]\\])", r"\\\1", v)


def compile_pattern(source, target):
    names, last, expr = [], 0, ""
    for m in re.finditer(r"\{([A-Za-z_]\w*)\}", source):
        expr += escape(source[last:m.start()])
        expr += "([0-9][0-9,.]*)" if m.group(1) in NUMERIC else "([\\s\\S]+?)"
        names.append(m.group(1))
        last = m.end()
    expr += escape(source[last:])
    expr = re.sub(r"\s+", r"\\s+", expr)
    fixed_len = len(normalize(re.sub(r"\{[A-Za-z_]\w*\}", "", source)))
    if not names or not fixed_len or (fixed_len < 2 and not all(n in NUMERIC for n in names)):
        return None
    return {"regex": re.compile("^" + expr + "$", re.U), "names": names, "target": target,
            "translated": [], "weight": fixed_len}


class Catalogue:
    def __init__(self, directory="static/i18n"):
        messages, patterns = {}, []
        for f in glob.glob(f"{directory}/*.json"):
            data = json.load(open(f, encoding="utf-8"))
            messages.update(data.get("messages", {}))
            patterns.extend(data.get("patterns", []))
        self.normalized = {normalize(k): v for k, v in messages.items()}
        rows = sorted(patterns, key=lambda r: -len(normalize(re.sub(r"\{[A-Za-z_]\w*\}", "", r["source"]))))
        self.patterns = [c for c in (compile_pattern(normalize(r["source"]), r["target"]) for r in rows) if c]

    def translate(self, value):
        key = normalize(value)
        if not key:
            return value
        result = self.normalized.get(key)
        if result is None and len(key) < 4000 and CJK.search(key):
            for rule in self.patterns:
                m = rule["regex"].match(str(value).strip())
                if not m:
                    continue
                values = dict(zip(rule["names"], m.groups()))
                result = re.sub(r"\{([A-Za-z_]\w*)\}",
                                lambda x: values.get(x.group(1), ""), rule["target"])
                break
        if result is None:
            return value
        lead = re.match(r"^\s*", value).group(0)
        tail = re.search(r"\s*$", value).group(0)
        return lead + result + tail


class Extractor(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.text, self.attrs = [], [], []

    def _protected(self, tag, attrs):
        d = {k.lower(): v for k, v in attrs}
        classes = set((d.get("class") or "").split())
        if tag in PROTECTED_TAGS or classes & PROTECTED_CLASSES:
            return True
        for spec in PROTECTED_ATTRS:
            name, _, want = spec.partition("=")
            if name in d and (not want or d[name] == want):
                return True
        return bool(self.stack and self.stack[-1]["protected"])

    def handle_starttag(self, tag, attrs):
        d = {k.lower(): v for k, v in attrs}
        protected = self._protected(tag, attrs)
        skip_text = (tag == "kb-name" or "data-i18n-preserve-text" in d or
                     "kb-name" in (d.get("class") or "").split())
        self.stack.append({"tag": tag, "protected": protected, "skip_text": skip_text})
        if not protected:
            for name in ATTRS:
                if d.get(name) and CJK.search(d[name]):
                    self.attrs.append((name, d[name], tag))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.stack.pop()

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i]["tag"] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if not data.strip() or not CJK.search(data):
            return
        top = self.stack[-1] if self.stack else None
        if not top or top["protected"] or top["skip_text"]:
            return
        self.text.append(data)


def find_missing(paths, catalogue):
    """Chinese strings that stay Chinese in EN mode.

    User data (library names, tracker field labels, row values) shows up too and
    is expected — those must NOT be translated; read the report with that in mind.
    """
    missing = {}
    for path in paths:
        parser = Extractor()
        with open(path, encoding="utf-8") as handle:
            parser.feed(handle.read())
        candidates = list(parser.text) + [value for _name, value, _tag in parser.attrs]
        for value in candidates:
            if not CJK.search(value) or catalogue.translate(value) != value:
                continue
            missing.setdefault(normalize(value), set()).add(path.split("/")[-1])
    return missing


if __name__ == "__main__":
    cat = Catalogue()
    missing = find_missing(sys.argv[1:] or glob.glob("/tmp/pg_*.html"), cat)
    print(f"untranslated in EN mode: {len(missing)}\n")
    for text, where in sorted(missing.items()):
        print(f"  · {text[:110]!r}   [{', '.join(sorted(w.split('.')[0] for w in where))}]")
