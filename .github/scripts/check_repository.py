"""Offline checks for the repository's documentation and CI support files."""

from pathlib import Path
import os
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
import yaml
from yaml.constructor import ConstructorError


class UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loading with duplicate keys rejected before merge expansion."""

    MAX_MERGE_ENTRIES = 10_000
    MAX_TOTAL_MERGE_ENTRIES = 100_000

    def __init__(self, stream):
        super().__init__(stream)
        self.checked_mapping_nodes = set()
        self.merge_sizes = {}
        self.measuring_merge_nodes = set()
        # A loader handles one YAML file, including all of its documents.
        self.total_merge_entries = 0

    def construct_document(self, node):
        self.checked_mapping_nodes.clear()
        self.merge_sizes.clear()
        self.measuring_merge_nodes.clear()
        return super().construct_document(node)

    def merge_size(self, node):
        """Count merge entries without allocating expanded alias lists."""
        if node in self.merge_sizes:
            return self.merge_sizes[node]
        if node in self.measuring_merge_nodes:
            raise ConstructorError(None, None, "recursive YAML merge", node.start_mark)
        self.measuring_merge_nodes.add(node)
        size = 0
        for key, value in node.value:
            if key.tag == "tag:yaml.org,2002:merge":
                sources = value.value if isinstance(value, yaml.SequenceNode) else [value]
                for source in sources:
                    # Invalid merge targets are reported by PyYAML itself.
                    if isinstance(source, yaml.MappingNode):
                        size += self.merge_size(source)
                        self.check_merge_size(size, node)
            else:
                size += 1
                self.check_merge_size(size, node)
        self.measuring_merge_nodes.remove(node)
        self.merge_sizes[node] = size
        return size

    def check_merge_size(self, size, node):
        if size > self.MAX_MERGE_ENTRIES:
            raise ConstructorError(
                None, None, f"merge expansion exceeds {self.MAX_MERGE_ENTRIES} entries",
                node.start_mark,
            )

    def flatten_mapping(self, node):
        # flatten_mapping also visits merge aliases and mutates their nodes.
        # Check original keys once, before any inherited keys are inserted.
        if node in self.checked_mapping_nodes:
            return
        if any(key.tag == "tag:yaml.org,2002:merge" for key, _ in node.value):
            self.total_merge_entries += self.merge_size(node)
            if self.total_merge_entries > self.MAX_TOTAL_MERGE_ENTRIES:
                raise ConstructorError(
                    None, None,
                    f"total merge expansion exceeds {self.MAX_TOTAL_MERGE_ENTRIES} entries",
                    node.start_mark,
                )
        self.checked_mapping_nodes.add(node)
        seen = set()
        for key_node, _ in node.value:
            key = ("<<" if key_node.tag == "tag:yaml.org,2002:merge"
                   else self.construct_object(key_node))
            try:
                duplicate = key in seen
                seen.add(key)
            except TypeError as error:
                raise ConstructorError(
                    None, None, "unhashable mapping key", key_node.start_mark
                ) from error
            if duplicate:
                raise ConstructorError(
                    None, None, "duplicate mapping key", key_node.start_mark
                )
        super().flatten_mapping(node)


# GitHub uses YAML 1.2 boolean spellings. Keep `on`/`off`/`yes`/`no` as strings
# without changing PyYAML's global SafeLoader behavior.
UniqueKeyLoader.yaml_implicit_resolvers = {
    character: [(tag, pattern) for tag, pattern in resolvers
                if tag != "tag:yaml.org,2002:bool"]
    for character, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
UniqueKeyLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)

TEXT_SUFFIXES = {".md", ".yaml", ".yml", ".py", ".txt"}
TEXT_NAMES = {".gitignore", ".gitattributes", ".editorconfig", "LICENSE", "CODEOWNERS"}
CONFLICT_MARKER = re.compile(r"^(?:<{7}|={7}|>{7}|\|{7})(?:\s|$)")
MARKDOWN = MarkdownIt("commonmark").enable("table")


def check_markdown(root, path, content, tracked_targets):
    """Check local link paths, excluding fragments, HTML, and external URLs."""
    errors = []
    for block in MARKDOWN.parse(content):
        for token in block.children or []:
            attribute = {"link_open": "href", "image": "src"}.get(token.type)
            if attribute is None:
                continue
            destination = token.attrGet(attribute)
            url = urlsplit(destination)
            if url.scheme or url.netloc or not url.path:
                continue
            link_path = unquote(url.path)
            # GitHub renders leading-slash links relative to the repository root.
            destination_path = ((root / link_path.lstrip("/")) if link_path.startswith("/")
                                else (root / path).parent / link_path)
            logical_target = Path(os.path.abspath(destination_path))
            target = destination_path.resolve()
            line = block.map[0] + 1 if block.map else 1
            if not target.is_relative_to(root):
                errors.append(f"{path}:{line}: local link leaves repository: {destination}")
            elif not target.exists():
                errors.append(f"{path}:{line}: local link target missing: {destination}")
            elif logical_target not in tracked_targets or target not in tracked_targets:
                errors.append(f"{path}:{line}: local link target is not tracked: {destination}")
    return errors


def validate(root, paths):
    root = root.resolve()
    paths = list(paths)
    tracked_files = {root / path for path in paths}
    tracked_targets = tracked_files | {
        parent for path in tracked_files for parent in path.parents
        if parent.is_relative_to(root)
    }
    errors = []
    checked = 0
    for path in paths:
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in TEXT_NAMES:
            continue
        checked += 1
        source = (root / path).resolve()
        if not source.is_relative_to(root):
            errors.append(f"{path}: file leaves repository")
            continue
        try:
            content = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            errors.append(f"{path}: cannot read UTF-8 text ({type(error).__name__})")
            continue
        markdown = path.suffix.lower() == ".md"
        # Seven equals signs are also a valid Markdown Setext heading underline.
        setext_lines = {
            token.map[1] for token in MARKDOWN.parse(content)
            if token.type == "heading_open" and token.markup == "=" and token.map
        } if markdown else set()
        for number, line in enumerate(content.split("\n"), 1):
            setext_underline = line.startswith("=======") and number in setext_lines
            if CONFLICT_MARKER.match(line) and not setext_underline:
                errors.append(f"{path}:{number}: merge conflict marker")
            stripped = line.rstrip(" \t")
            trailing = line[len(stripped):]
            hard_break = (markdown and stripped and len(trailing) >= 2
                          and set(trailing) == {" "})
            if trailing and not hard_break:
                errors.append(f"{path}:{number}: trailing whitespace")
        if markdown:
            errors.extend(check_markdown(root, path, content, tracked_targets))
        if path.suffix.lower() in {".yaml", ".yml"}:
            try:
                list(yaml.load_all(content, Loader=UniqueKeyLoader))
            except yaml.YAMLError as error:
                mark = getattr(error, "problem_mark", None)
                line = mark.line + 1 if mark is not None else 1
                errors.append(f"{path}:{line}: YAML {getattr(error, 'problem', str(error))}")
    return checked, errors


def main():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True,
    )
    paths = [Path(name.decode("utf-8")) for name in result.stdout.split(b"\0") if name]
    checked, errors = validate(root, paths)
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        print(f"Repository checks failed: {len(errors)} error(s).", file=sys.stderr)
        return 1
    print(f"Repository checks passed: {checked} tracked text files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
