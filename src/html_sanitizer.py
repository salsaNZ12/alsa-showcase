"""
Allow-list HTML sanitizer for the rich-text "Custom" weekly summary.

Why it exists: formatted Custom text (bold, tables, links, colors) is stored on the server and
rendered for ALL workspace members through innerHTML. Without sanitizing, one hijacked account
could plant <script>, onerror= handlers or javascript: links that run in every member's
browser (stored XSS).

Rules:
  * Only tags in ALLOWED_TAGS pass; other tags are dropped but their text content is kept.
  * Dangerous tags (script, style, iframe, object, embed, svg, math, form, ...) are dropped
    TOGETHER WITH their content.
  * <img> is dropped (base64 images bloat the database). The number removed is reported.
  * Attributes: only those listed in ALLOWED_ATTRS. Every on* handler and unknown attribute is dropped.
  * href accepts only http/https/mailto, and always gets target=_blank and rel="noopener noreferrer".
  * style: only properties in ALLOWED_CSS with simple values (no url(), expression, etc.).
  * Unclosed tags are closed automatically, so the output is always balanced HTML.
Uses only the Python standard library.
"""
import re
from html import escape
from html.parser import HTMLParser

ALLOWED_TAGS = {
    "p", "div", "span", "br", "hr", "b", "strong", "i", "em", "u", "s", "strike", "del",
    "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote",
    "table", "thead", "tbody", "tr", "th", "td", "a", "font", "sub", "sup", "code", "pre",
}
VOID_TAGS = {"br", "hr"}
DROP_WITH_CONTENT = {
    "script", "style", "iframe", "object", "embed", "svg", "math", "form", "textarea",
    "select", "option", "button", "noscript", "template", "title", "head", "link", "meta",
    "audio", "video", "canvas", "applet", "frameset", "frame",
}
ALLOWED_ATTRS = {
    "a": {"href"},
    "font": {"face", "size", "color"},
    "td": {"colspan", "rowspan", "style"},
    "th": {"colspan", "rowspan", "style"},
    "span": {"style"}, "p": {"style"}, "div": {"style"}, "li": {"style"},
    "ul": {"style"}, "ol": {"style"}, "table": {"style"}, "tr": {"style"},
    "h1": {"style"}, "h2": {"style"}, "h3": {"style"}, "h4": {"style"}, "h5": {"style"}, "h6": {"style"},
    "blockquote": {"style"}, "b": {"style"}, "strong": {"style"}, "i": {"style"}, "em": {"style"}, "u": {"style"},
}
ALLOWED_CSS = {
    "color", "background-color", "font-size", "font-family", "font-weight", "font-style",
    "text-decoration", "text-align", "border", "border-collapse", "border-color", "border-width",
    "border-style", "padding", "margin", "width", "min-width", "vertical-align", "line-height",
}
_CSS_VALUE_OK = re.compile(r"^[#a-zA-Z0-9\s.,%()\-\"'+/]*$")
_CSS_BAD = ("url(", "expression", "javascript", "@import", "\\", "behavior", "-moz-binding")
_FACE_OK = re.compile(r"^[A-Za-z0-9 ,'\"\-]{1,80}$")
_COLOR_OK = re.compile(r"^(#[0-9a-fA-F]{3,8}|[a-zA-Z]{3,20}|rgba?\([0-9 ,.%]{3,40}\))$")
_SAFE_HREF = re.compile(r"^(https?://|mailto:)", re.I)
MAX_OPEN_DEPTH = 60


def _bersihkan_css(nilai):
    keluar = []
    for dekl in str(nilai).split(";"):
        if ":" not in dekl:
            continue
        prop, _, val = dekl.partition(":")
        prop, val = prop.strip().lower(), val.strip()
        if prop not in ALLOWED_CSS or not val or len(val) > 120:
            continue
        low = val.lower()
        if any(b in low for b in _CSS_BAD) or not _CSS_VALUE_OK.match(val):
            continue
        keluar.append("%s:%s" % (prop, val))
    return ";".join(keluar)


def _bersihkan_href(nilai):
    v = re.sub(r"[\x00-\x20]+", "", str(nilai))  # strip whitespace/control chars used to smuggle "java\tscript:"
    if len(v) > 2000 or not _SAFE_HREF.match(v):
        return None
    return v


class _Pembersih(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.stack = []
        self.skip = 0          # nesting depth inside a tag that is dropped together with its content
        self.gambar_dibuang = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.skip:
            if tag in DROP_WITH_CONTENT:
                self.skip += 1
            return
        if tag == "img":
            self.gambar_dibuang += 1
            return
        if tag in DROP_WITH_CONTENT:
            self.skip = 1
            return
        if tag not in ALLOWED_TAGS:
            return
        if tag not in VOID_TAGS and len(self.stack) >= MAX_OPEN_DEPTH:
            return
        izin = ALLOWED_ATTRS.get(tag, set())
        bagian = []
        for nama, nilai in attrs:
            nama = (nama or "").lower()
            if nama not in izin or nilai is None:
                continue
            if nama == "style":
                v = _bersihkan_css(nilai)
                if v:
                    bagian.append('style="%s"' % escape(v, quote=True))
            elif nama == "href":
                v = _bersihkan_href(nilai)
                if v:
                    bagian.append('href="%s"' % escape(v, quote=True))
            elif nama in ("colspan", "rowspan"):
                if re.fullmatch(r"[1-9][0-9]?", str(nilai).strip()):
                    bagian.append('%s="%s"' % (nama, str(nilai).strip()))
            elif nama == "face":
                if _FACE_OK.match(str(nilai)):
                    bagian.append('face="%s"' % escape(str(nilai), quote=True))
            elif nama == "size":
                if re.fullmatch(r"[1-7]", str(nilai).strip()):
                    bagian.append('size="%s"' % str(nilai).strip())
            elif nama == "color":
                if _COLOR_OK.match(str(nilai).strip()):
                    bagian.append('color="%s"' % escape(str(nilai).strip(), quote=True))
        if tag == "a":
            bagian.append('target="_blank"')
            bagian.append('rel="noopener noreferrer"')
        self.out.append("<%s%s>" % (tag, (" " + " ".join(bagian)) if bagian else ""))
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        tag = tag.lower()
        if tag not in VOID_TAGS and tag in ALLOWED_TAGS and not self.skip and self.stack and self.stack[-1] == tag:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.skip:
            if tag in DROP_WITH_CONTENT:
                self.skip -= 1
            return
        if tag not in ALLOWED_TAGS or tag in VOID_TAGS or tag not in self.stack:
            return
        while self.stack:                       # close up to the matching tag so the output stays balanced
            t = self.stack.pop()
            self.out.append("</%s>" % t)
            if t == tag:
                break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(escape(data, quote=False))

    def handle_comment(self, data):
        pass

    def handle_decl(self, decl):
        pass

    def unknown_decl(self, data):
        pass

    def handle_pi(self, data):
        pass


def bersihkan_html(html, maks_masukan=200000):
    """Return (clean_html, number_of_images_removed). Non-string input -> ("", 0)."""
    if not isinstance(html, str):
        return "", 0
    p = _Pembersih()
    p.feed(html[:maks_masukan])
    p.close()
    while p.stack:
        p.out.append("</%s>" % p.stack.pop())
    return "".join(p.out), p.gambar_dibuang


def teks_polos(html):
    """Text with all tags removed, used to measure the real content length."""
    bersih, _ = bersihkan_html(html)
    return re.sub(r"<[^>]+>", "", bersih)