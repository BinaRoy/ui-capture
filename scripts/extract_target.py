#!/usr/bin/env python3
"""
Target-side UI extractor: parse Cangjie / ArkUI page files and emit UI-IR + HTML
in the same shape as the Android-side capture, so the two can be compared 1:1.

This is a STATIC analysis — no compiler, no runtime. We tokenize `build()` bodies
and chase `@Builder` references inside the same class. The grammar we recognize is
a deliberate subset of Cangjie, focused on declarative UI:

  Component(...args...) [{ body }] [.modifier(...)]*
  this.builderRef()                ← inline @Builder body from same class
  ForEach(seq, fn)                 ← emit a "list" node with one stand-in child
  if (cond) { ... } [else { ... }] ← emit a "branch" node with both arms

Anything else (statements, helpers) is silently ignored — the output is a
visual structure, not a faithful AST.

The skill's render_html.render() is reused so the HTML format matches the
source side. Bounds aren't available statically, so the layout pane omits the
absolute-positioned overlay and falls back to indented blocks.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _paths import TARGET_PROJECT_ROOT, UI_ROOT, ensure_ui_dirs  # noqa: E402


# Map ArkUI component names to the platform-agnostic `kind` used in UI-IR.
# Kinds are aligned with the Android side so HTML node classes light up the
# same colours (text/image/button/list etc.).
COMPONENT_KIND = {
    "NavDestination": "navdest",
    "Navigation": "navdest",
    "Scroll": "scroll",
    "Column": "column",
    "Row": "row",
    "Stack": "stack",
    "Flex": "flex",
    "List": "list",
    "ListItem": "list_item",
    "Grid": "grid",
    "GridItem": "grid_item",
    "Tabs": "tabs",
    "TabContent": "tab_content",
    "Text": "text",
    "Span": "text",
    "Button": "button",
    "Image": "image",
    "TextInput": "input",
    "TextArea": "input",
    "Search": "input",
    "Checkbox": "checkable",
    "Radio": "checkable",
    "Toggle": "checkable",
    "Slider": "view",
    "Progress": "view",
    "Divider": "view",
    "Blank": "view",
    "ForEach": "list",
    "LazyForEach": "list",
}


@dataclass
class CangjieComponent:
    """A node in the build() tree."""
    name: str
    kind: str
    args: str = ""            # raw constructor args, e.g. "\"Search\""
    text: Optional[str] = None
    modifiers: dict = field(default_factory=dict)
    children: list["CangjieComponent"] = field(default_factory=list)
    source_line: Optional[int] = None
    builder_ref: Optional[str] = None  # if this node was a `this.foo()` reference

    def to_ir(self) -> dict:
        out: dict = {"kind": self.kind, "class": self.name}
        if self.text:
            out["text"] = self.text
        if self.args and self.args.strip():
            out["args"] = _trim(self.args, 80)
        if self.modifiers:
            out["modifiers"] = {k: _trim(v, 60) for k, v in self.modifiers.items()}
        if self.builder_ref:
            out["builder_ref"] = self.builder_ref
        if self.source_line is not None:
            out["source_line"] = self.source_line
        if self.children:
            out["children"] = [c.to_ir() for c in self.children]
        return out


@dataclass
class CangjiePage:
    class_name: str
    file_path: Path
    build_root: Optional[CangjieComponent]
    builders: dict[str, CangjieComponent] = field(default_factory=dict)
    # Map class name → slug used for output dir
    @property
    def slug(self) -> str:
        return _camel_to_snake(self.class_name)


# ---------------------------------------------------------------- parser


_COMPONENT_AT_RE = re.compile(r"@Component\b")
_CLASS_RE = re.compile(r"(?:public\s+)?class\s+(\w+)")
_FUNC_HEADER_RE = re.compile(r"(?:public|private|protected)?\s*(?:override\s+)?func\s+(\w+)\s*\(")
_BUILDER_AT_RE = re.compile(r"@Builder\b")


def parse_file(path: Path) -> list[CangjiePage]:
    text = path.read_text(encoding="utf-8")
    text_clean = _strip_comments(text)
    pages: list[CangjiePage] = []

    # Walk @Component class declarations
    for m in _COMPONENT_AT_RE.finditer(text_clean):
        # Find the next `class X` after @Component
        cls_m = _CLASS_RE.search(text_clean, m.end())
        if not cls_m:
            continue
        class_name = cls_m.group(1)
        # Locate the matching class body braces
        body_start = text_clean.find("{", cls_m.end())
        if body_start == -1:
            continue
        body_end = _matching_brace(text_clean, body_start)
        if body_end == -1:
            continue
        class_body = text_clean[body_start + 1 : body_end]

        # Build a line offset map so we can report source_line
        line_offset_in_file = text_clean[:body_start + 1].count("\n")

        builders: dict[str, CangjieComponent] = {}
        build_root: Optional[CangjieComponent] = None

        # Find all func headers in class_body, then for each: if preceded by
        # @Builder collect it; if it's build(), extract its body as the root.
        for fm in _FUNC_HEADER_RE.finditer(class_body):
            fname = fm.group(1)
            # Locate function body
            fn_open = class_body.find("{", fm.end())
            if fn_open == -1:
                continue
            fn_close = _matching_brace(class_body, fn_open)
            if fn_close == -1:
                continue
            fn_body = class_body[fn_open + 1 : fn_close]
            fn_line_in_file = line_offset_in_file + class_body[: fn_open].count("\n") + 1

            # Check if preceded by @Builder (within ~3 lines)
            preceding = class_body[max(0, fm.start() - 80) : fm.start()]
            is_builder = bool(_BUILDER_AT_RE.search(preceding))

            if fname == "build":
                children = parse_body(fn_body, line_base=fn_line_in_file)
                # Wrap in a synthetic root if multiple top-level components
                if len(children) == 1:
                    build_root = children[0]
                else:
                    build_root = CangjieComponent(
                        name="<build>", kind="root", children=children,
                        source_line=fn_line_in_file,
                    )
            elif is_builder:
                children = parse_body(fn_body, line_base=fn_line_in_file)
                if len(children) == 1:
                    builders[fname] = children[0]
                elif children:
                    builders[fname] = CangjieComponent(
                        name=fname, kind="builder_group", children=children,
                        source_line=fn_line_in_file,
                    )

        # Inline @Builder references inside build_root
        if build_root is not None:
            _inline_builders(build_root, builders)

        pages.append(CangjiePage(
            class_name=class_name,
            file_path=path,
            build_root=build_root,
            builders=builders,
        ))

    return pages


def parse_body(body: str, *, line_base: int = 0) -> list[CangjieComponent]:
    """Parse a sequence of top-level component calls inside a `{ ... }` block."""
    components: list[CangjieComponent] = []
    i, n = 0, len(body)

    while i < n:
        # Skip whitespace / statements that aren't component calls
        ch = body[i]
        if ch.isspace():
            i += 1
            continue

        # Track current line for source mapping
        line_here = line_base + body[:i].count("\n")

        # Detect "if (cond) { ... } else { ... }"
        if body.startswith("if", i) and (i + 2 >= n or not body[i + 2].isalnum() and body[i + 2] != "_"):
            node, j = _parse_if(body, i, line_here)
            if node is not None:
                components.append(node)
            i = j
            continue

        # Detect "this.builderName()"
        if body.startswith("this.", i):
            j = i + 5
            name_match = re.match(r"\w+", body[j:])
            if name_match:
                builder_name = name_match.group(0)
                k = j + len(builder_name)
                # Skip optional "()"
                while k < n and body[k] in " \t":
                    k += 1
                if k < n and body[k] == "(":
                    paren_close = _matching_paren(body, k)
                    if paren_close != -1:
                        k = paren_close + 1
                # Also consume any trailing modifier chain
                k = _skip_modifier_chain(body, k)
                components.append(CangjieComponent(
                    name=builder_name,
                    kind="builder_ref",
                    builder_ref=builder_name,
                    source_line=line_here,
                ))
                i = k
                continue

        # Try to recognize an identifier (component name)
        name_m = re.match(r"([A-Z]\w*)", body[i:])
        if not name_m:
            # Skip to next newline-ish
            next_nl = body.find("\n", i)
            i = next_nl + 1 if next_nl != -1 else n
            continue

        name = name_m.group(1)
        j = i + len(name)

        # Expect "(...)" args
        while j < n and body[j] in " \t":
            j += 1
        if j >= n or body[j] != "(":
            # Not a component call (could be a type ref) — skip
            i = j
            continue
        paren_close = _matching_paren(body, j)
        if paren_close == -1:
            break
        args = body[j + 1 : paren_close]
        j = paren_close + 1

        # Optional child block "{ ... }"
        while j < n and body[j] in " \t":
            j += 1
        children: list[CangjieComponent] = []
        if j < n and body[j] == "{":
            brace_close = _matching_brace(body, j)
            if brace_close != -1:
                inner = body[j + 1 : brace_close]
                inner_line_base = line_base + body[: j + 1].count("\n")
                children = parse_body(inner, line_base=inner_line_base)
                j = brace_close + 1

        # Modifier chain ".foo(...).bar(...)..."
        modifiers: dict[str, str] = {}
        j = _skip_modifier_chain(body, j, modifiers_out=modifiers)

        node = CangjieComponent(
            name=name,
            kind=COMPONENT_KIND.get(name, "view"),
            args=args.strip(),
            modifiers=modifiers,
            children=children,
            source_line=line_here,
        )
        _extract_text(node)
        components.append(node)
        i = j

    return components


def _parse_if(body: str, i: int, line_here: int) -> tuple[Optional[CangjieComponent], int]:
    """Best-effort parse of `if (cond) { then } else { else }` into a branch node."""
    # Find the opening "("
    j = i + 2
    while j < len(body) and body[j] in " \t":
        j += 1
    if j >= len(body) or body[j] != "(":
        return None, i + 2
    paren_close = _matching_paren(body, j)
    if paren_close == -1:
        return None, j + 1
    cond = body[j + 1 : paren_close].strip()
    k = paren_close + 1
    while k < len(body) and body[k] in " \t":
        k += 1
    if k >= len(body) or body[k] != "{":
        return None, k
    brace_close = _matching_brace(body, k)
    if brace_close == -1:
        return None, k + 1
    then_children = parse_body(body[k + 1 : brace_close])
    k = brace_close + 1
    else_children: list[CangjieComponent] = []
    # Optional else
    m = k
    while m < len(body) and body[m] in " \t\n":
        m += 1
    if body[m:m + 4] == "else":
        m += 4
        while m < len(body) and body[m] in " \t\n":
            m += 1
        if m < len(body) and body[m] == "{":
            else_close = _matching_brace(body, m)
            if else_close != -1:
                else_children = parse_body(body[m + 1 : else_close])
                k = else_close + 1
    node = CangjieComponent(
        name="<if>",
        kind="branch",
        args=cond,
        children=[
            CangjieComponent(name="then", kind="branch_arm", children=then_children),
            CangjieComponent(name="else", kind="branch_arm", children=else_children),
        ] if else_children else
        [CangjieComponent(name="then", kind="branch_arm", children=then_children)],
        source_line=line_here,
    )
    return node, k


def _skip_modifier_chain(body: str, j: int, modifiers_out: Optional[dict] = None) -> int:
    """Walk a chain of `.foo(args).bar(args)...` and capture into modifiers_out."""
    n = len(body)
    while j < n:
        # Allow whitespace before next .
        k = j
        while k < n and body[k] in " \t\n":
            k += 1
        if k >= n or body[k] != ".":
            return j
        # Some "this.x" is handled separately — but inside a chain, we look for
        # ".identifier("
        name_m = re.match(r"\.(\w+)\s*\(", body[k:])
        if not name_m:
            return j
        mod_name = name_m.group(1)
        paren_open = k + name_m.end() - 1
        paren_close = _matching_paren(body, paren_open)
        if paren_close == -1:
            return j
        mod_args = body[paren_open + 1 : paren_close].strip()
        if modifiers_out is not None:
            modifiers_out[mod_name] = mod_args
        j = paren_close + 1
    return j


def _extract_text(node: CangjieComponent) -> None:
    """If the component is a Text/Button/etc., try to pull a string literal."""
    if node.name in ("Text", "Span", "Button"):
        # Look for first quoted string in args
        m = re.search(r'"([^"]*)"', node.args)
        if m:
            node.text = m.group(1)


def _inline_builders(node: CangjieComponent, builders: dict[str, CangjieComponent], depth: int = 0) -> None:
    """Replace builder_ref nodes with the actual builder tree (depth-limited)."""
    if depth > 6:
        return
    new_children: list[CangjieComponent] = []
    for child in node.children:
        if child.kind == "builder_ref" and child.builder_ref in builders:
            expanded = _clone(builders[child.builder_ref])
            expanded.builder_ref = child.builder_ref  # keep the trace
            _inline_builders(expanded, builders, depth + 1)
            new_children.append(expanded)
        else:
            _inline_builders(child, builders, depth + 1)
            new_children.append(child)
    node.children = new_children


def _clone(node: CangjieComponent) -> CangjieComponent:
    return CangjieComponent(
        name=node.name,
        kind=node.kind,
        args=node.args,
        text=node.text,
        modifiers=dict(node.modifiers),
        children=[_clone(c) for c in node.children],
        source_line=node.source_line,
        builder_ref=node.builder_ref,
    )


# ---------------------------------------------------------- low-level tokens

def _strip_comments(text: str) -> str:
    """Remove // line comments and /* */ block comments while preserving strings.

    We don't worry about escapes inside strings beyond \\" — Cangjie strings
    use the same convention as Swift/Java.
    """
    out = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            out.append(ch)
            in_str = True
            i += 1
            continue
        if ch == "/" and i + 1 < n:
            if text[i + 1] == "/":
                # line comment — skip to newline
                nl = text.find("\n", i)
                if nl == -1:
                    return "".join(out)
                # Preserve the newline so line numbers don't shift
                out.append("\n")
                i = nl + 1
                continue
            if text[i + 1] == "*":
                # block comment
                end = text.find("*/", i + 2)
                if end == -1:
                    return "".join(out)
                # Keep newlines from inside the block to preserve line numbers
                out.append("\n" * text[i:end + 2].count("\n"))
                i = end + 2
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _matching_brace(text: str, open_pos: int) -> int:
    return _match_delim(text, open_pos, "{", "}")


def _matching_paren(text: str, open_pos: int) -> int:
    return _match_delim(text, open_pos, "(", ")")


def _match_delim(text: str, open_pos: int, opener: str, closer: str) -> int:
    """Return index of matching closing delimiter, or -1. Skips strings/chars."""
    depth = 0
    i, n = open_pos, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            i += 1
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _camel_to_snake(name: str) -> str:
    s = re.sub(r"([a-z\d])([A-Z])", r"\1_\2", name)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
    s = s.lower()
    # Drop "_page" / "_view" suffix when present
    for suf in ("_page", "_view", "_section", "_component"):
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
    return s


def _trim(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


# -------------------------------------------------------------- discovery

def find_page_files(target_root: Path) -> list[Path]:
    """Find all .cj files that contain an @Component and a build() function."""
    found: list[Path] = []
    skip = {".git", "build", "oh_modules", ".hvigor", ".idea"}
    for p in target_root.rglob("*.cj"):
        if any(part in skip for part in p.parts):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "@Component" in text and "build" in text:
            found.append(p)
    return sorted(found)


# ----------------------------------------------------------- entry point

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-root", default=None,
                        help="root directory to scan for .cj page files (defaults to TARGET_PROJECT_ROOT)")
    parser.add_argument("--file", action="append", default=[],
                        help="explicit .cj file(s) to parse instead of scanning")
    parser.add_argument("--out", default=None,
                        help="output dir (defaults to <workflow.root>/ui/target_pages/)")
    args = parser.parse_args()

    if args.file:
        files = [Path(f) for f in args.file]
    else:
        root = Path(args.target_root) if args.target_root else TARGET_PROJECT_ROOT
        files = find_page_files(root)

    ensure_ui_dirs()
    out_dir = Path(args.out) if args.out else (UI_ROOT / "target_pages")
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest: list[dict] = []
    for path in files:
        try:
            pages = parse_file(path)
        except Exception as exc:  # parsing should not abort the whole run
            print(f"[extract_target] ERROR {path}: {exc}")
            continue
        if not pages:
            continue
        for page in pages:
            if page.build_root is None:
                continue
            page_dir = out_dir / page.slug
            page_dir.mkdir(parents=True, exist_ok=True)
            ir = page.build_root.to_ir()
            ir = {"kind": "root", "platform": "cangjie", "children": [ir]}
            (page_dir / "hierarchy.json").write_text(
                json.dumps(ir, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            # Render HTML (no screenshot, no bounds)
            from render_html import render
            (page_dir / "view.html").write_text(
                render(ir, page_id=f"{page.slug} (target)", identity={
                    "top_component": page.class_name,
                    "source_file": str(path),
                }, screenshot_rel=None),
                encoding="utf-8",
            )
            meta = {
                "slug": page.slug,
                "class_name": page.class_name,
                "source_file": str(path),
                "builders_inlined": list(page.builders.keys()),
            }
            (page_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
            manifest.append(meta)
            print(f"[extract_target] {page.class_name:30s} → {page_dir.name}/")

    (out_dir / "target_manifest.json").write_text(
        json.dumps({"pages": manifest, "count": len(manifest)}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"[extract_target] {len(manifest)} page(s) → {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
