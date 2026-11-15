"""Copy a checked Sentinel-2 export and a checked Landsat export into one folder for the website.

Run it like this:
    python -m orchardwater.site_copy --sentinel site-data/2026-06-29 --landsat site-data/landsat \
        --out <a new or empty folder>

What it writes into the output folder:
  sentinel-<id>/        the Sentinel-2 bundle (data.json and its pictures), copied unchanged
  landsat-<id>/         landsat-passes.json, copied unchanged
  manifest.json         written last: names both folders and gives the SHA-256 and size of
                        every file, plus the credit texts, notices and version stamps

<id> is the EXPORT ID: the first 12 characters of the SHA-256 of the data file. A changed
export gets a new folder name, so a page that still holds an old manifest can never mix old
numbers with new pictures. The manifest sits outside both data folders because
site_export.load_bundle refuses any extra file inside a bundle folder.

This command never deletes or replaces anything that was there before it ran. --out must not
exist yet, or be an empty real folder. To update the website's data, run it into a new
folder and replace the site's data folder by hand (git then shows the change).

Before copying, both inputs must pass the exporters' own loaders with the code running now
(load_bundle and load_passes). After copying, verify_site_data() checks the whole output
again. A page build step should call verify_site_data() too, so nothing is published that
has not passed the same chain.
"""

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

from orchardwater import landsat_export, site_export

# Bump this when the layout of manifest.json changes, so an old page refuses a new file.
MANIFEST_SCHEMA_VERSION = 1
MANIFEST_FILE = "manifest.json"

# 12 hex characters = 48 bits: far more than enough to tell a handful of exports apart.
EXPORT_ID_LENGTH = 12
SENTINEL_FOLDER = re.compile(r"sentinel-[0-9a-f]{12}", re.ASCII)
LANDSAT_FOLDER = re.compile(r"landsat-[0-9a-f]{12}", re.ASCII)

# The Sentinel data file has no "kind" entry of its own, so the manifest names it here.
SENTINEL_KIND = "sentinel-2-day"

# A credit holding this text is a placeholder. The manifest then says publishable: false
# and the page tooling refuses to publish it. Compared without regard to upper/lower case.
TEST_ONLY_MARK = "TEST ONLY"

COPIED_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
COPIED_TIME_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", re.ASCII)
SHA256_PATTERN = site_export.FINGERPRINT_FORMAT  # 64 lowercase hex characters

# The root of this code repository (src/orchardwater/site_copy.py is two folders below it).
REPO_ROOT = Path(__file__).resolve().parents[2]

TOP_KEYS = frozenset(
    {"manifest_schema_version", "copied_utc", "publishable", "sentinel", "landsat"}
)
DATASET_KEYS = frozenset(
    {
        "folder",
        "kind",
        "schema_version",
        "source_fingerprint",
        "package_version",
        "data_file",
        "credit",
        "notices",
    }
)
SENTINEL_KEYS = DATASET_KEYS | {"pictures"}
FILE_KEYS = frozenset({"file", "sha256", "bytes"})


def _fail(message: str) -> NoReturn:
    raise ValueError(f"The site data is not usable: {message}")


# ---------------------------------------------------------------- small shared helpers


def file_sha256(path: Path) -> str:
    """SHA-256 of a file's exact bytes, as 64 hex characters."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def export_id(data_sha256: str) -> str:
    """The export id: the first 12 hex characters of the data file's SHA-256."""
    return data_sha256[:EXPORT_ID_LENGTH]


def file_entry(path: Path) -> dict[str, Any]:
    """The manifest entry of one file: its own name (never a folder), SHA-256 and size in bytes."""
    return {"file": path.name, "sha256": file_sha256(path), "bytes": path.stat().st_size}


def is_publishable(credits: list[str]) -> bool:
    """False if any credit is a placeholder (contains "TEST ONLY", any case), else True."""
    return not any(TEST_ONLY_MARK.lower() in credit.lower() for credit in credits)


def read_strict_json(path: Path) -> Any:
    """Read a JSON file with site_export.loads_strict (no repeated key, no NaN or Infinity)."""
    try:
        return site_export.loads_strict(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        _fail(f"{path.name} is missing, is not valid JSON, or repeats a key.")


def manifest_text(manifest: dict[str, Any]) -> str:
    """The one exact text form of a manifest: sorted keys, indent 2, "\\n" line ends."""
    text = json.dumps(manifest, sort_keys=True, indent=2, allow_nan=False, ensure_ascii=False)
    return text + "\n"


def _is_link(path: Path) -> bool:
    """True for a symbolic link or a Windows junction (a folder that points somewhere else)."""
    return path.is_symlink() or path.is_junction()


def _is_sentinel_file(name: str) -> bool:
    """True for a file name the Sentinel export writes (data.json or one of its pictures)."""
    return name == site_export.DATA_FILE or site_export.GENERATED_PICTURE.match(name) is not None


def _is_landsat_file(name: str) -> bool:
    """True for the one file name the Landsat export writes."""
    return name == landsat_export.DATA_FILE


# ---------------------------------------------------------------- reading the inputs


def load_sentinel(folder: Path, require_current_code: bool = True) -> dict[str, Any]:
    """Check a Sentinel-2 bundle folder with the exporter's own loader. Returns the data."""
    return site_export.load_bundle(folder, require_current_code)


def load_landsat(folder: Path, require_current_code: bool = True) -> dict[str, Any]:
    """Check a Landsat folder with the exporter's own loader. Returns the data."""
    return landsat_export.load_passes(folder, require_current_code)


# ---------------------------------------------------------------- building the manifest


def sentinel_entry(folder: Path, data: dict[str, Any]) -> dict[str, Any]:
    """The manifest entry of a checked Sentinel bundle in folder."""
    pictures = [file_entry(folder / entry["file"]) for entry in data["images"].values()]
    pictures.sort(key=lambda entry: entry["file"])
    data_file = file_entry(folder / site_export.DATA_FILE)
    return {
        "folder": "sentinel-" + export_id(data_file["sha256"]),
        "kind": SENTINEL_KIND,
        "schema_version": data["schema_version"],
        # The Sentinel file calls its fingerprint source_fingerprint_sha256 and the Landsat
        # file calls it source_fingerprint. Both are a SHA-256 of the code that made the
        # file; the manifest stores both under the same neutral name, source_fingerprint.
        "source_fingerprint": data["version"]["source_fingerprint_sha256"],
        "package_version": data["version"]["package_version"],
        "data_file": data_file,
        "pictures": pictures,
        "credit": data["credit"],
        "notices": data["notices"],
    }


def landsat_entry(folder: Path, data: dict[str, Any]) -> dict[str, Any]:
    """The manifest entry of a checked Landsat file in folder."""
    data_file = file_entry(folder / landsat_export.DATA_FILE)
    return {
        "folder": "landsat-" + export_id(data_file["sha256"]),
        "kind": data["kind"],
        "schema_version": data["schema_version"],
        "source_fingerprint": data["version"]["source_fingerprint"],  # see sentinel_entry
        "package_version": data["version"]["package_version"],
        "data_file": data_file,
        "credit": data["credit"],
        "notices": data["notices"],
    }


def build_manifest(
    sentinel: dict[str, Any], landsat: dict[str, Any], copied: datetime
) -> dict[str, Any]:
    """The whole manifest from the two dataset entries and the time of the copy (UTC)."""
    return {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "copied_utc": copied.astimezone(UTC).strftime(COPIED_TIME_FORMAT),
        "publishable": is_publishable([sentinel["credit"], landsat["credit"]]),
        "sentinel": sentinel,
        "landsat": landsat,
    }


# ---------------------------------------------------------------- where the output may go


def check_output_path(out_dir: Path, inputs: list[Path]) -> Path:
    """Stop unless out_dir is a safe place to write: not there yet, or an empty real folder.

    Refused: ".." in the path; a link or junction at out_dir or at any folder above it
    (checked on the path as given, before resolve(), because resolve() follows links and
    would hide them); a drive root, the home folder or a code repository root; a folder that
    is, contains or sits inside an input folder; a file; a folder that is not empty; a
    missing parent folder. Nothing is written by this check. Returns the resolved --out,
    which the cleanup after a failure uses to stay inside it.
    """
    if ".." in out_dir.parts:
        _fail("--out must not contain '..'; give the folder directly.")
    _refuse_links_on_path(out_dir)
    target = out_dir.resolve()
    _refuse_special_places(target)
    for source in inputs:
        folder = source.resolve()
        if target == folder or folder in target.parents or target in folder.parents:
            _fail("--out must not be, contain or sit inside an input folder.")
    if not target.parent.is_dir():
        _fail("the folder that should hold --out does not exist; create it first.")
    if target.exists() and not target.is_dir():
        _fail("--out is a file, not a folder.")
    if target.exists() and any(target.iterdir()):
        _fail("--out is not empty. Give a new or empty folder; this command never replaces files.")
    return target


def _refuse_links_on_path(path: Path) -> None:
    """Stop if the path, or any folder above it, is a symbolic link or a junction."""
    # absolute() puts the current folder in front of a relative path but, unlike
    # resolve(), follows no link, so every step of the path can still be looked at.
    absolute = path.absolute()
    for step in [absolute, *absolute.parents]:
        if _is_link(step):
            _fail(f"--out goes through a link or junction ({step}); give the real folder.")


def _refuse_special_places(target: Path) -> None:
    """Stop if target is a drive root, the home folder or the root of a code repository."""
    if target.parent == target:
        _fail("--out is the root of a drive.")
    if target == Path.home().resolve():
        _fail("--out is the home folder.")
    if target == REPO_ROOT or (target / ".git").exists():
        _fail("--out is the root of a code repository.")


# ---------------------------------------------------------------- copying


@dataclass(frozen=True)
class Created:
    """One thing this run made: a folder (sha256 and size None) or a file with the SHA-256
    and size of the bytes this run wrote into it."""

    path: Path
    sha256: str | None = None
    size: int | None = None


def _new_folder(path: Path, created: list[Created]) -> None:
    """Make a folder that must not exist yet, and remember that this run made it."""
    path.mkdir()  # raises FileExistsError if it is already there
    created.append(Created(path))


def _new_file_with(target: Path, content: bytes, created: list[Created]) -> None:
    """Write bytes into a file that must not exist yet, and remember what was written.

    Mode "xb" creates the file and fails if it is already there, so only files this run
    made are remembered. The SHA-256 and size let the cleanup see if the file changed.
    Writing bytes also means Windows does not change "\\n" into "\\r\\n".
    """
    with open(target, "xb") as writer:
        created.append(Created(target, hashlib.sha256(content).hexdigest(), len(content)))
        writer.write(content)


def _write_output(
    sentinel_dir: Path,
    landsat_dir: Path,
    sentinel_data: dict[str, Any],
    manifest: dict[str, Any],
    out_dir: Path,
    created: list[Created],
) -> None:
    """Copy the files byte for byte into out_dir (data folders first, manifest.json last)."""
    sentinel_target = out_dir / manifest["sentinel"]["folder"]
    _new_folder(sentinel_target, created)
    names = sorted(entry["file"] for entry in sentinel_data["images"].values())
    for name in [*names, site_export.DATA_FILE]:
        _new_file_with(sentinel_target / name, (sentinel_dir / name).read_bytes(), created)
    landsat_target = out_dir / manifest["landsat"]["folder"]
    _new_folder(landsat_target, created)
    landsat_name = landsat_export.DATA_FILE
    content = (landsat_dir / landsat_name).read_bytes()
    _new_file_with(landsat_target / landsat_name, content, created)
    _new_file_with(out_dir / MANIFEST_FILE, manifest_text(manifest).encode("utf-8"), created)


def remove_created(created: list[Created], out_dir: Path, out_resolved: Path) -> list[str]:
    """After a failure, remove what this run created and still is unchanged, newest first.

    Best effort, and it refuses to touch anything that changed: a file is removed only if
    it still has the bytes this run wrote (same SHA-256 and size); a folder only if it is
    empty and not a link; nothing is removed through a link or junction, or outside the
    --out folder checked at the start. Returns the names of what was left in place.
    """
    left: list[str] = []
    for item in reversed(created):
        if not _remove_if_unchanged(item, out_dir, out_resolved):
            if item.path == out_dir:
                left.append(out_dir.name)
            else:
                left.append(item.path.relative_to(out_dir).as_posix())
    return left


def _remove_if_unchanged(item: Created, out_dir: Path, out_resolved: Path) -> bool:
    """Remove one thing this run made if it is still exactly that. True if it was removed."""
    try:
        if not _still_inside(item.path, out_dir, out_resolved):
            return False
        if item.sha256 is None:
            if not item.path.is_dir() or any(item.path.iterdir()):
                return False
            item.path.rmdir()
            return True
        if not item.path.is_file():
            return False  # missing, or something else is there now
        raw = item.path.read_bytes()
        if len(raw) != item.size or hashlib.sha256(raw).hexdigest() != item.sha256:
            return False
        item.path.unlink()
        return True
    except OSError:
        return False


def _still_inside(path: Path, out_dir: Path, out_resolved: Path) -> bool:
    """True if no step from path up to out_dir is a link or junction, and path still leads
    to a place inside the --out folder that was checked at the start."""
    for step in [path, *path.parents]:
        if _is_link(step):
            return False
        if step == out_dir:
            break
    resolved = path.resolve()  # safe: no link on the way, checked just above
    return resolved == out_resolved or out_resolved in resolved.parents


def copy_site_data(
    sentinel_dir: Path, landsat_dir: Path, out_dir: Path, copied: datetime | None = None
) -> dict[str, Any]:
    """Check both exports, copy them into out_dir with a manifest, check the result. Returns it.

    Nothing is written if the output place or an input fails its checks. The data folders
    are written first and manifest.json last, so a folder without a manifest is clearly
    unfinished. If anything fails after writing started (or the run is stopped with Ctrl+C),
    remove_created removes what this run made and is still unchanged; anything that changed,
    went missing or sits behind a link is left in place and named in the error message.
    Anything that was there before the run is never touched.
    """
    out_resolved = check_output_path(out_dir, [sentinel_dir, landsat_dir])
    sentinel_data = load_sentinel(sentinel_dir)
    landsat_data = load_landsat(landsat_dir)
    # The entries hash the SOURCE files; verify_site_data compares the copies with them.
    sentinel = sentinel_entry(sentinel_dir, sentinel_data)
    landsat = landsat_entry(landsat_dir, landsat_data)
    manifest = build_manifest(sentinel, landsat, copied or datetime.now(UTC))
    created: list[Created] = []
    try:
        if not out_dir.exists():
            _new_folder(out_dir, created)
        _write_output(sentinel_dir, landsat_dir, sentinel_data, manifest, out_dir, created)
        return verify_site_data(out_dir)
    except BaseException as error:  # also Ctrl+C: never leave half a copy behind
        left = remove_created(created, out_dir, out_resolved)
        if not left:
            raise
        note = "Cleanup left in place (changed, missing, not empty or behind a link): "
        note += ", ".join(left)
        if isinstance(error, Exception):
            raise ValueError(f"{error} {note}") from error
        error.add_note(note)
        raise


# ---------------------------------------------------------------- checking an output folder


def verify_site_data(folder: Path, require_current_code: bool = True) -> dict[str, Any]:
    """Check a folder written by copy_site_data from scratch. Returns the manifest.

    Checks that manifest.json is in its one exact text form with exactly the allowed entries
    and only relative names; that every listed file exists with the listed SHA-256 and size
    and nothing else is in the folder; that each folder name is the export id of its data
    file; that both data folders pass the exporters' loaders; that the manifest repeats the
    data files' facts exactly; and that publishable follows the credit rule. Raises
    ValueError with a plain message at the first problem.

    require_current_code (default True) also refuses data made by other code than the code
    running now. Pass False only to look at old data, never to publish it.
    """
    manifest = read_manifest(folder)
    check_manifest_shape(manifest)
    check_folder_contents(folder, manifest)
    sentinel_dir = folder / manifest["sentinel"]["folder"]
    landsat_dir = folder / manifest["landsat"]["folder"]
    sentinel_data = load_sentinel(sentinel_dir, require_current_code)
    landsat_data = load_landsat(landsat_dir, require_current_code)
    if manifest["sentinel"] != sentinel_entry(sentinel_dir, sentinel_data):
        _fail("the manifest does not repeat the Sentinel files and facts exactly.")
    if manifest["landsat"] != landsat_entry(landsat_dir, landsat_data):
        _fail("the manifest does not repeat the Landsat file and facts exactly.")
    credits = [manifest["sentinel"]["credit"], manifest["landsat"]["credit"]]
    if manifest["publishable"] != is_publishable(credits):
        _fail("publishable does not follow from the credit texts.")
    return manifest


def read_manifest(folder: Path) -> dict[str, Any]:
    """Read manifest.json strictly and insist on its one exact text form."""
    path = folder / MANIFEST_FILE
    manifest = read_strict_json(path)
    if not isinstance(manifest, dict):
        _fail(f"{MANIFEST_FILE} is not a JSON object.")
    try:
        same_text = manifest_text(manifest).encode("utf-8") == path.read_bytes()
    except ValueError:
        same_text = False
    if not same_text:
        # Catches hand edits and line-end changes (git's autocrlf would turn "\n" into "\r\n").
        _fail(f"{MANIFEST_FILE} is not in the exact form this command writes.")
    return manifest


def check_manifest_shape(manifest: dict[str, Any]) -> None:
    """Exactly the allowed entries, each of the right kind; names are bare and relative."""
    if set(manifest) != TOP_KEYS:
        _fail(f"{MANIFEST_FILE} has missing or extra entries.")
    version = manifest["manifest_schema_version"]
    if type(version) is not int or version != MANIFEST_SCHEMA_VERSION:
        _fail(f"manifest schema version is {version!r}, expected {MANIFEST_SCHEMA_VERSION}.")
    copied = manifest["copied_utc"]
    if not isinstance(copied, str) or COPIED_TIME_PATTERN.fullmatch(copied) is None:
        _fail("copied_utc is not a UTC time written like 2026-10-09T12:00:00Z.")
    try:
        datetime.strptime(copied, COPIED_TIME_FORMAT)
    except ValueError:
        _fail("copied_utc is not a real date and time.")
    if type(manifest["publishable"]) is not bool:
        _fail("publishable is not true or false.")
    _check_dataset_shape(manifest["sentinel"], SENTINEL_KEYS, SENTINEL_FOLDER, "sentinel")
    _check_dataset_shape(manifest["landsat"], DATASET_KEYS, LANDSAT_FOLDER, "landsat")


def _check_dataset_shape(
    entry: Any, keys: frozenset[str], folder: re.Pattern[str], what: str
) -> None:
    """One dataset entry: exact keys, a folder named like an export id, files listed correctly."""
    if not isinstance(entry, dict) or set(entry) != keys:
        _fail(f"the {what} entry has missing or extra entries.")
    if not isinstance(entry["folder"], str) or folder.fullmatch(entry["folder"]) is None:
        _fail(f"the {what} folder name is not '{what}-' plus a 12-character export id.")
    fingerprint = entry["source_fingerprint"]
    if not isinstance(fingerprint, str) or SHA256_PATTERN.fullmatch(fingerprint) is None:
        _fail(f"the {what} source fingerprint is not a SHA-256.")
    if type(entry["schema_version"]) is not int:
        _fail(f"the {what} schema version is not a whole number.")
    if what == "sentinel" and not isinstance(entry["pictures"], list):
        _fail("the sentinel pictures are not a list.")
    files = [entry["data_file"], *entry.get("pictures", [])]
    for listed in files:
        _check_file_shape(listed, what)


def _check_file_shape(listed: Any, what: str) -> None:
    """A file entry: a bare file name we write, a SHA-256 and a whole number of bytes."""
    if not isinstance(listed, dict) or set(listed) != FILE_KEYS:
        _fail(f"a {what} file entry has missing or extra entries.")
    name = listed["file"]
    if what == "sentinel":
        allowed = _is_sentinel_file
    else:
        allowed = _is_landsat_file
    # Only names our exporters write are allowed, so "..", "/", "\\" and drive letters
    # (any path at all) can never get in.
    if not isinstance(name, str) or not allowed(name):
        _fail(f"a {what} file name is not one this command writes.")
    if not isinstance(listed["sha256"], str) or SHA256_PATTERN.fullmatch(listed["sha256"]) is None:
        _fail(f"{what} file {name}: the checksum is not a SHA-256.")
    if type(listed["bytes"]) is not int or listed["bytes"] < 0:
        _fail(f"{what} file {name}: the size is not a whole number of bytes.")


def check_folder_contents(folder: Path, manifest: dict[str, Any]) -> None:
    """Exactly the listed files are present, with the listed SHA-256 and size; nothing else."""
    sentinel, landsat = manifest["sentinel"], manifest["landsat"]
    _same_names(folder, {MANIFEST_FILE, sentinel["folder"], landsat["folder"]}, "output folder")
    for entry in (sentinel, landsat):
        listed = [entry["data_file"], *entry.get("pictures", [])]
        data_dir = folder / entry["folder"]
        if _is_link(data_dir) or not data_dir.is_dir():
            _fail(f"{entry['folder']} is not a plain folder.")
        _same_names(data_dir, {item["file"] for item in listed}, entry["folder"])
        for item in listed:
            _check_file(data_dir / item["file"], item, entry["folder"])
        if entry["folder"].split("-")[1] != export_id(entry["data_file"]["sha256"]):
            _fail(f"{entry['folder']} is not named after its data file (another export id).")


def _same_names(folder: Path, wanted: set[str], what: str) -> None:
    """The folder holds exactly the wanted names: nothing missing, nothing extra."""
    present = {entry.name for entry in folder.iterdir()}
    if present - wanted:
        _fail(f"the {what} holds files that are not listed: " + ", ".join(sorted(present - wanted)))
    if wanted - present:
        _fail(f"the {what} is missing: " + ", ".join(sorted(wanted - present)))


def _check_file(path: Path, item: dict[str, Any], what: str) -> None:
    """One file: a plain file (no link) with the listed size and SHA-256."""
    if _is_link(path) or not path.is_file():
        _fail(f"{what}/{item['file']} is not a plain file.")
    if path.stat().st_size != item["bytes"] or file_sha256(path) != item["sha256"]:
        _fail(f"{what}/{item['file']} has changed since it was copied (size or checksum differs).")


# ---------------------------------------------------------------- command line


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Copy checked exports into one site data folder.")
    parser.add_argument("--sentinel", type=Path, required=True, help="Sentinel-2 export folder")
    parser.add_argument("--landsat", type=Path, required=True, help="Landsat export folder")
    parser.add_argument("--out", type=Path, required=True, help="the site's data folder")
    args = parser.parse_args(argv)
    try:
        manifest = copy_site_data(args.sentinel, args.landsat, args.out)
    except (ValueError, OSError) as error:
        raise SystemExit(f"Stopped. {error}") from None
    print(f"Copied {manifest['sentinel']['folder']} and {manifest['landsat']['folder']}")
    print(f"Wrote {MANIFEST_FILE} last, in {args.out}; the output passed verify_site_data.")
    if manifest["publishable"]:
        print("Publishable: yes")
    else:
        print(f"Publishable: no (a credit text says {TEST_ONLY_MARK})")


if __name__ == "__main__":
    main()
