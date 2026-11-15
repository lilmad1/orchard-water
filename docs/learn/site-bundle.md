# Copying the two exports into one folder for the website

## Overview

Two earlier exports made the data the website shows:

- `site_export` saves one Sentinel-2 day (29 June 2026) as six pictures plus `data.json`.
- `landsat_export` saves every Landsat pass as `landsat-passes.json`.

These sit in two separate folders. The website needs them in ONE place, together with a
small file that tells the page exactly which files belong together and what they should
contain. That is what the new command does:

```
python -m orchardwater.site_copy --sentinel <Sentinel folder> --landsat <Landsat folder> --out <new or empty folder>
```

It writes:

```
<out>/
  manifest.json                   the list: which folders, which files, their checksums
  sentinel-<id>/                  data.json + 6 pictures, copied unchanged
  landsat-<id>/                   landsat-passes.json, copied unchanged
```

## Map

```
site_export.py   -> load_bundle  \
                                   site_copy.py -> <out>/manifest.json + two data folders
landsat_export.py -> load_passes /       |
                                   verify_site_data(<out>)  (anyone can run it again later)
```

## It never deletes or replaces anything

Replacing an earlier output folder and deleting the old one can go wrong (for example a
folder that was really a link to somewhere else). The command must never delete anything
that was there before. So:

- `--out` must be a folder that does not exist yet, or an EMPTY real folder.
- Everything else is refused, and nothing is written: a folder with anything in it (even a
  `manifest.json` from somewhere else), a file, a path with `..` in it, a path that is or
  goes through a link or a Windows junction (a folder that points to another place), the
  same folder as an input or a folder inside or around one, a drive root like `C:\`, the
  user's home folder, the root of a code repository, or a folder whose parent does not exist.
- To update the website's data, run the command into a NEW folder and then swap the
  site's data folder by hand. Git then shows exactly what changed.

## Step by step: what the command does

1. Check where the output goes (the rules above). The link check looks at the path as
   it was typed, before Python's `resolve()` turns it into the "real" path, because
   `resolve()` follows links and would hide them.
2. Check both inputs with the exporters' own checkers. `load_bundle` (Sentinel) and
   `load_passes` (Landsat) run with "the code running now" required. If the files were made
   by older code, or anything does not add up, the command stops and writes nothing.
3. Copy, data folders first, `manifest.json` last. Every new file is opened in a mode
   that refuses to open a file that is already there, so the command can only ever write
   new files. It keeps a list of every file and folder it creates, and for each file the
   SHA-256 and size of the bytes it wrote.
4. Check the finished output with `verify_site_data`.
5. If anything fails after writing started (a full disk, a failed check, even Ctrl+C),
   it tries to remove what is on its list, newest first. This cleanup is best effort, and it
   refuses to touch anything that changed:
   - a file is removed only if it still has exactly the bytes this run wrote (same SHA-256
     and size). A file that changed or went missing is left alone;
   - a folder is removed only if it is empty and is not a link;
   - nothing is removed if the path, or a folder between it and `--out`, is now a link or a
     junction, or if it no longer leads to a place inside the `--out` folder checked at the
     start (so a junction swapped in during the run is never followed);
   - a folder that existed before the run (an empty `--out`) is never removed.

   Everything that was left in place is named in the error message.

## A stricter Sentinel checker (load_bundle)

`load_bundle` must not let wrong files through, for example a square
whose "source" was 0.5 (a photo number must be a whole number), an NDVI of `1e999` (Python
reads that as "infinity"), or an extra entry like a catalogue address hidden in `data.json`.
`load_bundle` now checks every entry against what the export writes:

- exactly the expected entries at every level, nothing missing and nothing extra;
- whole numbers are real whole numbers (not `true`/`false`, not `1.0`), decimals are
  finite, counts are in order and the shares are exactly what the counts give;
- the version stamp is a version number, a 64-character fingerprint and "unknown";
- texts are the fixed texts the export writes, or plain text with no address or file path;
- photo ids use only safe characters, photo times are UTC times on the day;
- per-square values: 0/1 lists hold only 0 and 1, source numbers name a photo or are -1,
  scene classes are 0 to 255;
- the per-square lists agree with each other: a clear square lies inside the outline and
  has usable data, a source photo, a clear scene class and an NDVI from -1 to 1; every other
  square has no NDVI;
- the stored counts are counted again from the lists: the day's field, observed and clear
  squares must be equal. Every photo covers the same outline, so each photo's field count
  must be the outline's count. The lists do not hold each photo's own squares, but every
  square the day's picture took from a photo was observed (and, if clear, clear) in that
  photo too, so the photo's counts can be no smaller;
- the verdict follows from the numbers by the report's rule. It is checked entry by entry
  with type checks, because Python says `True == 1.0` and would let `true` pass as a share;
- a key written twice, and NaN or Infinity, are refused.

The pictures are checked too. A PNG file is a list of "chunks": a header (IHDR), the
pixels (IDAT, often several) and an end marker (IEND). Other chunk types can hold text,
for example a file path or a comment. The writer (rasterio, which uses GDAL) produces only
IHDR, IDAT and IEND; the real 29 June files were checked to confirm this. So `load_bundle` reads
the chunk list by hand and refuses a picture with any other chunk, a chunk before IHDR,
anything after IEND, a broken chunk checksum (CRC), a file that is cut short, or a header
whose size or pixel layout (8 bits, RGB) is not what the export writes.

The JSON reader that refuses repeated keys now lives in `site_export` (`loads_strict`), and
`landsat_export` uses the same one. Because both files changed, both code fingerprints
changed, so both exports had to be made again.

## The manifest

The page in a browser cannot check that the data was made by the current code (that check
needs the Python source). What it CAN do is check that the files it downloads are exactly
the files that passed the checks here. For that it needs a list of checksums it can trust:

- for each dataset: folder name, kind, schema version, source fingerprint, package version,
  and the SHA-256 and size in bytes of the data file;
- for the Sentinel pictures: each file name, SHA-256 and size (`data.json` lists the picture
  checksums too, but the page should check `data.json` before trusting anything inside it);
- the credit texts and notices, copied exactly from the data files;
- `copied_utc`, the time of the copy;
- `publishable`: false when a credit text contains "TEST ONLY". The 29 June export uses the
  placeholder credit "TEST ONLY - credit wording not decided", so today it says false, and
  the site's publish check will refuse it until the real wording is decided.

The two exporters name the fingerprint differently (`source_fingerprint_sha256` in the
Sentinel file, `source_fingerprint` in the Landsat file). The manifest stores both under one
name, `source_fingerprint`, so the page needs only one rule.

A checksum (SHA-256) is a 64-character code worked out from a file's bytes. Change one
byte and the code changes completely. So if the code matches, the file is the same.

## Export ids in folder names

GitHub Pages keeps files in a cache for about 10 minutes. If the page loads a new
`data.json` but an old picture from the cache: the numbers and the picture would not match,
and nobody would notice. To rule that out, each export gets its own folder name,
made from its content: `sentinel-` plus the first 12 characters of the SHA-256 of its data
file. Different content gives a different name, so an old manifest can never point at new
files. The Sentinel `data.json` holds the checksum of every picture, so its own checksum
pins down the whole bundle.

## Byte-for-byte copying

The files are copied exactly as they are. The JSON is never read and written out again,
because that could change spaces, the order of keys or the line ends, and then the
checksums would no longer match the files that were checked. The manifest itself is written
as bytes with plain `\n` line ends. Windows normally writes `\r\n` when Python writes text,
and git (`autocrlf`) can do the same when it saves files; `verify_site_data`
refuses a manifest whose text is not exactly the form the command writes, so such a change
is caught.

## verify_site_data: the check anyone can run again

`verify_site_data(folder)` repeats the whole chain on an existing output folder:

- the manifest is in its one exact text form (sorted keys, two-space indent, `\n` line ends)
  and has exactly the allowed entries; version numbers and sizes are real whole numbers,
  checksums are 64 hex characters, `publishable` is true or false;
- every name is a bare file or folder name the exporters write. A path like `../x` or `C:/x`
  can never get in, because only the exporters' own names are allowed;
- every listed file is present with the listed size and SHA-256, nothing else is there,
  and no data folder or file is a link;
- each folder is named after its own data file (no folder from another export);
- both data folders pass `load_bundle` and `load_passes`;
- the manifest repeats the data files' facts exactly (credit, notices, fingerprint,
  package version, schema version, kind, picture list);
- `publishable` follows from the credit texts.

The page build step should call it before anything is published.

## New Python ideas

- `open(path, "xb")`: "x" means "create a new file, and fail if it already exists";
  "b" means bytes. This is how the command makes sure it never overwrites anything.
- Copying by reading and writing bytes: `source.read_bytes()` gives the file's exact
  bytes; writing them unchanged into the new file is a byte-for-byte copy. Holding the bytes
  also lets the command remember their SHA-256 for the cleanup.
- `@dataclass(frozen=True) class Created`: a small record (path, SHA-256, size) for each
  thing the run made. "frozen" means it cannot be changed after it is made.
- `path.rmdir()`: removes a folder only if it is empty. Used when cleaning up after a
  failure, so a folder that holds someone else's file is left alone.
- `except BaseException as error:` then `raise`: catches every kind of stop, including
  Ctrl+C, cleans up, and then lets the error carry on so it is still seen. If the cleanup
  had to leave something in place, the message names it (for Ctrl+C as a "note" on it).
- `struct.unpack(">I", four_bytes)`: reads 4 bytes as one whole number, biggest byte
  first, the way PNG writes its chunk lengths. `zlib.crc32(data)`: works out the
  checksum PNG stores after each chunk.
- `Path.absolute()` versus `Path.resolve()`: both give a full path; `resolve()` also
  follows links, `absolute()` does not. The link check needs the path before links are
  followed.
- `Path.is_junction()`: tells a Windows junction (a folder that points elsewhere)
  from a real folder.
- `json.dumps(..., sort_keys=True, indent=2, allow_nan=False)`: one fixed way to turn
  the manifest into text, so the same manifest always gives the same bytes, and a "not a
  number" value is refused instead of written.
