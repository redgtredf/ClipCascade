import os


class DocumentNameError(ValueError):
    """Raised when a received document name fails path-safety validation."""


def sanitize_received_filenames(files: dict) -> dict:
    """
    Validates received document names so each is a safe plain basename.

    Rejects absolute paths, directory components, traversal, empty/dot names,
    and duplicate output names. Returns the mapping unchanged otherwise;
    nothing is normalised into something the sender did not send.
    """
    validated = {}
    seen_keys = set()
    for raw_name, obj in (files or {}).items():
        if not isinstance(raw_name, str):
            raise DocumentNameError(
                f"Invalid file name type: {type(raw_name).__name__}"
            )
        name = raw_name.strip()
        if not name or name in (".", ".."):
            raise DocumentNameError(f"Invalid file name: {raw_name!r}")
        if os.path.isabs(name):
            raise DocumentNameError(f"Absolute paths are not allowed: {raw_name!r}")
        if os.sep in name or (os.altsep and os.altsep in name):
            raise DocumentNameError(
                f"Directory components are not allowed: {raw_name!r}"
            )
        if name != raw_name:
            raise DocumentNameError(f"File name is not a safe basename: {raw_name!r}")
        key = name.casefold()
        if key in seen_keys:
            raise DocumentNameError(f"Duplicate file name: {name}")
        seen_keys.add(key)
        validated[name] = obj
    return validated


def save_received_files(files: dict, target_directory: str) -> list:
    """
    Saves validated received documents into target_directory.

    Never overwrites an existing file silently: collisions get an explicit
    non-colliding name. Returns the list of written paths. Validation runs for
    the whole batch before anything is written.
    """
    validated = sanitize_received_filenames(files)
    written = []
    for name, obj in validated.items():
        destination = os.path.join(target_directory, name)
        unique = destination
        stem, ext = os.path.splitext(name)
        counter = 1
        while os.path.exists(unique):
            unique = os.path.join(target_directory, f"{stem} ({counter}){ext}")
            counter += 1
        with open(unique, "wb") as f:
            f.write(obj.getvalue())
        written.append(unique)
    return written
