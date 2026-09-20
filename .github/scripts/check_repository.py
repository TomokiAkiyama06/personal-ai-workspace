"""Offline checks for the repository's documentation and CI support files."""

from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
import yaml
from yaml.constructor import ConstructorError


class UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loading with duplicate keys rejected before merge expansion."""

    def __init__(self, stream):
        super().__init__(stream)
        self.checked_mapping_nodes = set()

    def flatten_mapping(self, node):
        # flatten_mapping also visits merge aliases and mutates their nodes.
        # Check original keys once, before any inherited keys are inserted.
        if node in self.checked_mapping_nodes:
            return
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
CONFLICT_MARKER = re.compile(r"^(?:<{7}|>{7}|\|{7})(?:\s|$)")
MARKDOWN = MarkdownIt("commonmark").enable("table")


def check_markdown(root, path, content):
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
            target = ((root / link_path.lstrip("/")) if link_path.startswith("/")
                      else (root / path).parent / link_path).resolve()
            line = block.map[0] + 1 if block.map else 1
            if not target.is_relative_to(root):
                errors.append(f"{path}:{line}: local link leaves repository: {destination}")
            elif not target.exists():
                errors.append(f"{path}:{line}: local link target missing: {destination}")
    return errors


def validate(root, paths):
    root = root.resolve()
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
        for number, line in enumerate(content.splitlines(), 1):
            if CONFLICT_MARKER.match(line):
                errors.append(f"{path}:{number}: merge conflict marker")
            stripped = line.rstrip(" \t")
            trailing = line[len(stripped):]
            hard_break = (markdown and stripped and len(trailing) >= 2
                          and set(trailing) == {" "})
            if trailing and not hard_break:
                errors.append(f"{path}:{number}: trailing whitespace")
        if markdown:
            errors.extend(check_markdown(root, path, content))
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
