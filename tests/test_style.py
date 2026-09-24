import ast
import tokenize
from pathlib import Path

SRC_DIR = Path(__file__).parents[1] / "src" / "fixie"
REPO_ROOT = Path(__file__).parents[1]


def _source_files():
    return sorted(SRC_DIR.glob("*.py"))


def test_docstrings_short_and_public_only():
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                doc = ast.get_docstring(node)
                is_private_function = (
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name.startswith("_")
                    and not (node.name.startswith("__") and node.name.endswith("__"))
                )
                if is_private_function:
                    assert doc is None, f"{path}:{node.name} is private, must have no docstring"
                    continue
                if doc is not None:
                    line_count = len(doc.splitlines())
                    assert line_count <= 5, f"{path}:{getattr(node, 'name', '<module>')} docstring too long"


def test_only_ponytail_comments_in_source():
    for path in _source_files():
        with path.open("rb") as f:
            for tok in tokenize.tokenize(f.readline):
                if tok.type == tokenize.COMMENT:
                    assert tok.string.startswith("# ponytail:"), f"{path}:{tok.start[0]} disallowed comment"


def test_toml_and_env_example_have_no_comments():
    for name in ("config.toml", "pyproject.toml", ".env.example"):
        path = REPO_ROOT / name
        for line in path.read_text(encoding="utf-8").splitlines():
            assert not line.strip().startswith("#"), f"{name} has a comment: {line}"
