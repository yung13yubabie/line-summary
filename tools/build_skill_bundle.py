"""Build one inert, instructions-only Skill ZIP without installing or uploading it."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import zipfile

SKILL_SOURCE = "plugin/skills/line-summary-mock/SKILL.md"
SKILL_ARCHIVE_PATH = "line-summary-mock/SKILL.md"
ARCHIVE_MEMBERS = {"directory": SKILL_ARCHIVE_PATH, "root": "SKILL.md"}
SPEC_SOURCE = "plugin/skill-package-spec.json"
NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def validate_frontmatter(data: bytes) -> dict[str, str]:
    """Validate this package's strict two-field plain-scalar YAML subset.

    This is intentionally not a general YAML loader. Reject extended YAML syntax
    so the package can be verified with Python's standard library alone.
    """
    text = data.decode("utf-8")
    lines = text.splitlines()
    if len(lines) < 6 or lines[0] != "---" or lines[3] != "---":
        raise ValueError("Expected exactly two frontmatter fields")
    result = {}
    for expected, line in zip(("name", "description"), lines[1:3]):
        prefix = expected + ": "
        if not line.startswith(prefix):
            raise ValueError("Invalid frontmatter key or order")
        value = line[len(prefix):]
        if (not value or value != value.strip() or "\t" in value
                or ": " in value or " #" in value
                or value[0] in "!&*{}[],#|>@`\"'%?-:+.0123456789"
                or value.lower() in {"true", "false", "null", "~", "yes", "no", "on", "off"}):
            raise ValueError("Use a nonempty single-line plain YAML string")
        result[expected] = value
    if not NAME_RE.fullmatch(result["name"]) or len(result["name"]) > 64:
        raise ValueError("Invalid skill name")
    if result["name"] != "line-summary-mock" or len(result["description"]) > 200:
        raise ValueError("Metadata does not match this Skill package")
    if not "\n".join(lines[4:]).strip():
        raise ValueError("Skill body is empty")
    return result


def _read_source(root: Path, relative: str) -> bytes:
    path = root / relative
    # Reject symlinks anywhere in the source chain, including parent folders.
    candidate = root
    for part in PurePosixPath(relative).parts:
        candidate /= part
        if candidate.is_symlink():
            raise ValueError("Refusing source symlink")
    if not path.resolve().is_relative_to(root):
        raise ValueError("Source escapes package root")
    return path.read_bytes()


def build(source: Path, destination: Path, layout: str = "directory") -> dict:
    if layout not in ARCHIVE_MEMBERS:
        raise ValueError("Unknown Skill ZIP layout")
    archive_path = ARCHIVE_MEMBERS[layout]
    source = source.resolve()
    # Do not resolve the destination final component: exclusive creation rejects
    # existing files and symlinks instead of following or overwriting them.
    destination = destination.absolute()
    spec = json.loads(_read_source(source, SPEC_SOURCE))
    expected_files = [{"source": SKILL_SOURCE, "archive_path": SKILL_ARCHIVE_PATH}]
    if spec.get("files") != expected_files or spec.get("root_directory") != "line-summary-mock":
        raise ValueError("Only the single allowlisted Skill file may be packaged")
    if any(spec.get(key) is not False for key in (
            "contains_executables", "contains_mcp_config", "contains_credentials", "real_data_implemented")):
        raise ValueError("Package must be inert and synthetic-only")
    if spec.get("actual_host_acceptance") != "NOT_RUN":
        raise ValueError("Local packaging cannot claim host acceptance")
    if spec.get("archives", {}).get(layout, {}).get("member") != archive_path:
        raise ValueError("Layout does not match the allowlisted archive member")
    data = _read_source(source, SKILL_SOURCE)
    metadata = validate_frontmatter(data)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as output:
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            entry = zipfile.ZipInfo(archive_path, date_time=(2026, 10, 8, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, data)
    with zipfile.ZipFile(destination) as archive:
        if archive.namelist() != [archive_path] or archive.testzip() is not None:
            raise ValueError("Skill ZIP member validation failed")
        if archive.read(archive_path) != data:
            raise ValueError("Skill ZIP readback differs from source")
    return {
        "archive": destination.name,
        "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        "bytes": destination.stat().st_size,
        "members": [archive_path],
        "layout": layout,
        "frontmatter": metadata,
        "local_validation": "PASS",
        "actual_host_acceptance": "NOT_RUN",
        "real_data_implemented": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--layout", choices=tuple(ARCHIVE_MEMBERS), default="directory")
    args = parser.parse_args()
    report = build(Path(__file__).resolve().parents[1], args.output, args.layout)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
