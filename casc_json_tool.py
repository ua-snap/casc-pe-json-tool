#!/usr/bin/env python
"""Helper for hand-editing the CASC *_full_migrated.json files.

The migrated files are generated output (casc_sb_cache.py -> *_full.json ->
download_to_s3.py -> *_full_migrated.json), so every change made here is also
journaled to manual_edits/overrides.json and can be replayed with
`apply-overrides` after the generators run again.

Run `python casc_json_tool.py --help` for the command list, or
`python casc_json_tool.py fields` for a description of every field.
"""

import argparse
import copy
import datetime
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_JSON_DIR = os.path.join(BASE_DIR, "json")
EDITS_DIR = os.path.join(BASE_DIR, "manual_edits")
OVERRIDES_PATH = os.path.join(EDITS_DIR, "overrides.json")
BACKUP_DIR = os.path.join(EDITS_DIR, "backups")
# Migrated files are several megabytes each, so keep only a short history.
BACKUPS_TO_KEEP = 10

CSCS = {
    "5050cb0ee4b0be20bb30eac0": "National CASC",
    "4f831626e4b0e84f6086809b": "Alaska CASC",
    "5e2f3f59e4b0a79317d422af": "Midwest CASC",
    "4f83509de4b0e84f60868124": "North Central CASC",
    "4f8c648de4b0546c0c397b43": "Northeast CASC",
    "4f8c64d2e4b0546c0c397b46": "Northwest CASC",
    "4f8c650ae4b0546c0c397b48": "Pacific Islands CASC",
    "4f8c652fe4b0546c0c397b4a": "South Central CASC",
    "4f8c6557e4b0546c0c397b4c": "Southeast CASC",
    "4f8c6580e4b0546c0c397b4e": "Southwest CASC",
}

CONTACT_GROUPS = [
    "principal_investigators",
    "co_investigators",
    "authors",
    "cooperator_partner",
]

DATE_KEYS = ["start_date", "end_date", "publication_date", "created", "updated", "other"]

# Order used when writing a record, matching SbCache.newParseJson output.
RECORD_KEY_ORDER = [
    "contacts",
    "organizations",
    "cscs",
    "csc",
    "dates",
    "fiscal_year",
    "keywords",
    "topics",
    "subtopics",
    "hasMaps",
    "relationships",
    "main_project",
    "map_url",
    "images",
    "files",
    "id",
    "hasFolders",
    "approved_data",
    "approved_products",
    "other_approved",
    "other_children",
    "parent_id",
    "status",
    "body",
    "title",
    "types",
    "summary",
    "spatial",
    "identifiers",
    "doi",
    "preview_image",
]

SEARCH_KEY_ORDER = [
    "contacts",
    "contactSearchString",
    "organizations",
    "csc",
    "dates",
    "relationships",
    "fiscal_year",
    "keywords",
    "topics",
    "subtopics",
    "hasMaps",
    "previewImage",
    "id",
    "hasFolders",
    "parent_id",
    "status",
    "summary",
    "body",
    "title",
    "types",
]

STUB_KEY_ORDER = ["csc", "id", "body", "title", "types"]

FIELD_DOCS = [
    ("title", "text (required)", "Project or item title shown everywhere in the UI."),
    ("body", "html text", "Full description. Simple HTML such as <br> and <p> is allowed."),
    ("summary", "text", "Short blurb. Leave blank and the tool derives it from body."),
    ("types", "list of text (required)", "Must contain 'Project' for the item to appear in project lists."),
    ("status", "text", "'Active', 'Completed', 'In Progress', 'Proposed', or 'N/A'."),
    ("fiscal_year", "text", "Four-digit funding year, e.g. '2024'."),
    ("dates", "object", "start_date / end_date / publication_date as YYYY-MM-DD (YYYY or YYYY-MM also accepted); created and updated are timestamps."),
    ("contacts", "object", "Four lists: principal_investigators, co_investigators, authors, cooperator_partner. Each entry is {name, organization}."),
    ("organizations", "list of text", "Partner or funding organization names (CASC names excluded)."),
    ("csc", "object (required)", "{name, id} of the CASC that owns this file. Set automatically."),
    ("cscs", "list of objects", "Additional CASCs that funded the item, each {name, id}."),
    ("keywords", "list of text", "Free-text keywords used by search."),
    ("topics", "list of text", "Science theme labels, e.g. 'Wildlife and Plants'."),
    ("subtopics", "list of text", "Sub-theme labels, e.g. 'Fish'."),
    ("parent_id", "text", "ScienceBase id of the parent folder or collection."),
    ("id", "text (required)", "24-character identifier. Must match the key in the file."),
    ("hasMaps", "true/false", "Set true only when map_url points at a live WMS service."),
    ("map_url", "url", "WMS GetCapabilities URL. Required when hasMaps is true."),
    ("hasFolders", "true/false", "True when the item has child folders in ScienceBase."),
    ("images", "list of objects", "Each {title, url, useForPreview}. Exactly one should have useForPreview true."),
    ("preview_image", "object", "{title, url, thumbnail} used for the detail-page hero image."),
    ("files", "list of objects", "Downloadable, non-image files. Each needs at least name and url."),
    ("approved_products", "list of objects", "Publications and products: {id, title, webLinks, files, children, doi}."),
    ("approved_data", "list of objects", "Approved datasets, same shape as approved_products."),
    ("other_approved", "list of objects", "Other public items, same shape as approved_products."),
    ("other_children", "list of objects", "Remaining child items, each {id, title}."),
    ("identifiers", "list of objects", "Each {type, scheme, key}. DOI keys are stored as 'doi:10.xxxx/yyyy'."),
    ("doi", "text", "Convenience copy of the DOI identifier, including the 'doi:' prefix."),
    ("spatial", "object", "{boundingBox: {minX,maxX,minY,maxY}, representationalPoint: [lon, lat]}."),
    ("relationships", "list of objects or null", "Sub-project links. Leave null unless you know the related ids."),
]

KNOWN_RECORD_KEYS = set(RECORD_KEY_ORDER)
VALID_STATUSES = {"Active", "Completed", "In Progress", "Proposed", "Funded", "N/A"}
# ScienceBase stores partial dates, so YYYY and YYYY-MM are valid alongside YYYY-MM-DD.
DATE_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")
ID_RE = re.compile(r"^[0-9a-f]{24}$", re.IGNORECASE)
TAG_RE = re.compile(r"<[^<]+?>")


class ToolError(Exception):
    pass


# --------------------------------------------------------------------------
# Paths and loading
# --------------------------------------------------------------------------


def slug_for(csc_name):
    return csc_name.lower().replace(" ", "_")


def resolve_casc(token):
    """Accept a CASC id, full name, slug, or unambiguous prefix."""
    if token in CSCS:
        return token, CSCS[token]

    needle = token.strip().lower().replace("-", "_").replace(" ", "_")
    matches = []
    for csc_id, name in CSCS.items():
        slug = slug_for(name)
        if needle in (slug, slug.replace("_casc", ""), name.lower()):
            return csc_id, name
        if slug.startswith(needle) or slug.replace("_casc", "").startswith(needle):
            matches.append((csc_id, name))

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ToolError(
            "'%s' matches several CASCs: %s"
            % (token, ", ".join(sorted(name for _, name in matches)))
        )
    raise ToolError(
        "Unknown CASC '%s'. Choose one of: %s"
        % (token, ", ".join(sorted(slug_for(n) for n in CSCS.values())))
    )


def file_paths(json_dir, csc_name):
    slug = slug_for(csc_name)
    return {
        "migrated": os.path.join(json_dir, slug + "_full_migrated.json"),
        "search": os.path.join(json_dir, slug + "_search.json"),
        "stub": os.path.join(json_dir, slug + "_stub.json"),
    }


def load_json(path):
    if not os.path.exists(path):
        raise ToolError("File not found: %s" % path)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except ValueError as exc:
        raise ToolError("%s is not valid JSON: %s" % (os.path.basename(path), exc))


def backup_file(path):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    name = os.path.basename(path)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target = os.path.join(BACKUP_DIR, "%s.%s.bak" % (name, stamp))
    shutil.copy2(path, target)

    existing = sorted(
        entry
        for entry in os.listdir(BACKUP_DIR)
        if entry.startswith(name + ".") and entry.endswith(".bak")
    )
    for stale in existing[:-BACKUPS_TO_KEEP]:
        os.unlink(os.path.join(BACKUP_DIR, stale))
    return target


def save_json(path, data, make_backup=True):
    """Write atomically so an interrupted run cannot truncate a 6 MB file."""
    backup = backup_file(path) if make_backup and os.path.exists(path) else None
    directory = os.path.dirname(path) or "."
    handle, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(data, out, indent=2)
            out.write("\n")
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    return backup


def ordered(record, key_order):
    out = {key: record[key] for key in key_order if key in record}
    for key in record:
        if key not in out:
            out[key] = record[key]
    return out


# --------------------------------------------------------------------------
# Overrides journal
# --------------------------------------------------------------------------


def load_overrides():
    if not os.path.exists(OVERRIDES_PATH):
        return {"version": 1, "cascs": {}}
    data = load_json(OVERRIDES_PATH)
    data.setdefault("cascs", {})
    return data


def save_overrides(data):
    os.makedirs(EDITS_DIR, exist_ok=True)
    save_json(OVERRIDES_PATH, data, make_backup=os.path.exists(OVERRIDES_PATH))


def journal(csc_name, item_id, action, fields=None, removed=None, note=None):
    data = load_overrides()
    bucket = data["cascs"].setdefault(slug_for(csc_name), {})
    entry = {
        "action": action,
        "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "updated_by": getpass.getuser(),
    }
    if fields is not None:
        entry["fields"] = fields
    if removed:
        entry["removed_fields"] = sorted(removed)
    if note:
        entry["note"] = note

    previous = bucket.get(item_id)
    # A create stays a create: later edits fold into the stored full record.
    if action == "patch" and previous and previous.get("action") == "create":
        merged = copy.deepcopy(previous.get("fields", {}))
        merged.update(fields or {})
        for key in removed or []:
            merged.pop(key, None)
        entry["action"] = "create"
        entry["fields"] = merged
        entry.pop("removed_fields", None)

    bucket[item_id] = entry
    save_overrides(data)


def diff_fields(before, after):
    changed = {}
    for key, value in after.items():
        if key not in before or before[key] != value:
            changed[key] = value
    removed = [key for key in before if key not in after]
    return changed, removed


# --------------------------------------------------------------------------
# Record construction
# --------------------------------------------------------------------------


def new_id():
    return uuid.uuid4().hex[:24]


def blank_record(csc_id, title=""):
    return {
        "contacts": {group: [] for group in CONTACT_GROUPS},
        "organizations": [],
        "cscs": [],
        "csc": {"name": CSCS[csc_id], "id": csc_id},
        "dates": {key: None for key in DATE_KEYS},
        "fiscal_year": None,
        "keywords": [],
        "topics": [],
        "subtopics": [],
        "hasMaps": False,
        "relationships": None,
        "map_url": None,
        "images": None,
        "files": None,
        "id": new_id(),
        "hasFolders": False,
        "parent_id": csc_id,
        "status": None,
        "body": None,
        "title": title,
        "types": ["Project"],
        "summary": None,
        "spatial": None,
        "identifiers": None,
        "doi": None,
        "preview_image": None,
    }


def strip_tags(text):
    return TAG_RE.sub("", text or "")


def derive_summary(body):
    if not body:
        return None
    return strip_tags(body[0:255]) + " ..."


def now_stamp():
    return datetime.datetime.now().astimezone().isoformat(timespec="milliseconds")


def search_entry(record):
    body = record.get("body")
    contacts = record.get("contacts") or {}

    names = []
    for group in ("principal_investigators", "co_investigators", "authors"):
        for person in contacts.get(group) or []:
            if person.get("name"):
                names.append(person["name"])

    preview = None
    for image in record.get("images") or []:
        if image.get("useForPreview") and image.get("url"):
            preview = {"title": image.get("title") or "No Title", "url": image["url"]}
            break

    entry = {
        "contacts": record.get("contacts"),
        "contactSearchString": " ".join(names).strip(),
        "organizations": record.get("organizations"),
        "csc": record.get("csc"),
        "dates": record.get("dates"),
        "relationships": record.get("relationships"),
        "fiscal_year": record.get("fiscal_year"),
        "keywords": record.get("keywords"),
        "topics": record.get("topics"),
        "subtopics": record.get("subtopics"),
        "hasMaps": record.get("hasMaps", False),
        "previewImage": preview,
        "id": record.get("id"),
        "hasFolders": record.get("hasFolders", False),
        "parent_id": record.get("parent_id"),
        "status": record.get("status"),
        "summary": derive_summary(body),
        "body": strip_tags(body) if body else None,
        "title": record.get("title"),
        "types": record.get("types"),
    }
    return ordered(entry, SEARCH_KEY_ORDER)


def stub_entry(record):
    body = record.get("body")
    if not body or not record.get("types"):
        return None
    entry = {
        "csc": (record.get("csc") or {}).get("name"),
        "id": record.get("id"),
        "body": strip_tags(body),
        "title": record.get("title"),
        "types": record.get("types"),
    }
    return ordered(entry, STUB_KEY_ORDER)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _is_person_list(value):
    return isinstance(value, list) and all(
        isinstance(item, dict) and "name" in item for item in value
    )


def validate_record(record, key, csc_id):
    """Return a list of (level, message); level is 'error' or 'warning'."""
    problems = []

    def error(message):
        problems.append(("error", message))

    def warn(message):
        problems.append(("warning", message))

    if not isinstance(record, dict):
        return [("error", "record is not an object")]

    item_id = record.get("id")
    if not item_id:
        error("'id' is missing")
    elif item_id != key:
        error("'id' (%s) does not match its key (%s)" % (item_id, key))
    elif not ID_RE.match(item_id):
        warn("'id' is not 24 hex characters; the front end may not link to it")

    title = record.get("title")
    if not title or not str(title).strip():
        error("'title' is required")

    types = record.get("types")
    if types is None:
        warn("'types' is null, so this item is hidden from project lists")
    elif not isinstance(types, list) or not all(isinstance(t, str) for t in types):
        error("'types' must be a list of strings")
    elif "Project" not in types:
        warn("'types' has no 'Project' entry, so it will not appear in project lists")

    csc = record.get("csc")
    if not isinstance(csc, dict) or "id" not in csc or "name" not in csc:
        error("'csc' must be an object with 'name' and 'id'")
    elif csc["id"] != csc_id:
        error(
            "'csc.id' is %s but this file belongs to %s (%s)"
            % (csc["id"], CSCS[csc_id], csc_id)
        )
    elif csc["name"] != CSCS[csc_id]:
        error("'csc.name' should be '%s'" % CSCS[csc_id])

    contacts = record.get("contacts")
    if contacts is not None:
        if not isinstance(contacts, dict):
            error("'contacts' must be an object or null")
        else:
            for group in CONTACT_GROUPS:
                if group not in contacts:
                    warn("'contacts' is missing the '%s' list" % group)
                elif not _is_person_list(contacts[group]):
                    error("'contacts.%s' must be a list of {name, organization}" % group)
            for extra in set(contacts) - set(CONTACT_GROUPS):
                warn("'contacts.%s' is not a recognized contact group" % extra)

    dates = record.get("dates")
    if dates is not None:
        if not isinstance(dates, dict):
            error("'dates' must be an object or null")
        else:
            for field in ("start_date", "end_date", "publication_date"):
                value = dates.get(field)
                if value and not DATE_RE.match(str(value)):
                    error(
                        "'dates.%s' should be YYYY-MM-DD (or YYYY / YYYY-MM), got '%s'"
                        % (field, value)
                    )
            start, end = dates.get("start_date"), dates.get("end_date")
            if start and end and DATE_RE.match(str(start)) and DATE_RE.match(str(end)):
                if start > end:
                    warn("'dates.start_date' is after 'dates.end_date'")
            for extra in set(dates) - set(DATE_KEYS):
                warn("'dates.%s' is not a recognized date field" % extra)

    year = record.get("fiscal_year")
    if year is not None and not re.match(r"^\d{4}$", str(year)):
        warn("'fiscal_year' should be a four-digit year, got '%s'" % year)

    status = record.get("status")
    if status is not None and status not in VALID_STATUSES:
        warn(
            "'status' is '%s'; the usual values are %s"
            % (status, ", ".join(sorted(VALID_STATUSES)))
        )

    for field in ("keywords", "topics", "subtopics", "organizations"):
        value = record.get(field)
        if value is None:
            continue
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            error("'%s' must be a list of strings or null" % field)
        elif len(set(value)) != len(value):
            warn("'%s' contains duplicate entries" % field)

    for field in ("body", "summary", "map_url", "doi", "parent_id"):
        value = record.get(field)
        if value is not None and not isinstance(value, str):
            error("'%s' must be text or null" % field)

    if record.get("hasMaps") and not record.get("map_url"):
        warn("'hasMaps' is true but 'map_url' is empty, so no map will render")
    if record.get("map_url") and not record.get("hasMaps"):
        warn("'map_url' is set but 'hasMaps' is false, so no map tab will render")

    images = record.get("images")
    if images is not None:
        if not isinstance(images, list):
            error("'images' must be a list or null")
        else:
            previews = 0
            for index, image in enumerate(images):
                if not isinstance(image, dict) or not image.get("url"):
                    error("'images[%d]' needs a 'url'" % index)
                    continue
                if image.get("useForPreview"):
                    previews += 1
            if previews > 1:
                warn("%d images have useForPreview true; only the first is used" % previews)

    files = record.get("files")
    if files is not None:
        if not isinstance(files, list):
            error("'files' must be a list or null")
        else:
            for index, entry in enumerate(files):
                if not isinstance(entry, dict):
                    error("'files[%d]' must be an object" % index)
                elif not entry.get("url"):
                    error("'files[%d]' needs a 'url'" % index)
                elif not entry.get("name"):
                    warn("'files[%d]' has no 'name'" % index)
                if isinstance(entry, dict) and (entry.get("contentType") or "").startswith("image"):
                    warn("'files[%d]' is an image; images belong in 'images'" % index)

    preview = record.get("preview_image")
    if preview is not None:
        if not isinstance(preview, dict) or not preview.get("url"):
            error("'preview_image' must be an object with a 'url', or null")

    for section in ("approved_products", "approved_data", "other_approved", "other_children"):
        value = record.get(section)
        if value is None:
            continue
        if not isinstance(value, list):
            error("'%s' must be a list" % section)
            continue
        for index, child in enumerate(value):
            if not isinstance(child, dict):
                error("'%s[%d]' must be an object" % (section, index))
            elif not child.get("title"):
                warn("'%s[%d]' has no 'title'" % (section, index))

    identifiers = record.get("identifiers")
    if identifiers is not None:
        if not isinstance(identifiers, list):
            error("'identifiers' must be a list or null")
        else:
            for index, ident in enumerate(identifiers):
                if not isinstance(ident, dict) or "key" not in ident:
                    error("'identifiers[%d]' needs at least a 'key'" % index)
                elif (ident.get("type") or "").lower() == "doi":
                    if not str(ident["key"]).lower().startswith("doi:"):
                        warn("'identifiers[%d]' DOI key should start with 'doi:'" % index)

    doi = record.get("doi")
    if doi and not str(doi).lower().startswith("doi:"):
        warn("'doi' should start with 'doi:'")

    spatial = record.get("spatial")
    if spatial is not None:
        if not isinstance(spatial, dict):
            error("'spatial' must be an object or null")
        else:
            box = spatial.get("boundingBox")
            if box is not None:
                missing = [k for k in ("minX", "maxX", "minY", "maxY") if k not in box]
                if missing:
                    error("'spatial.boundingBox' is missing %s" % ", ".join(missing))
                elif box["minX"] > box["maxX"] or box["minY"] > box["maxY"]:
                    error("'spatial.boundingBox' min values exceed max values")

    for extra in sorted(set(record) - KNOWN_RECORD_KEYS):
        warn("'%s' is not a field the API reads; it will be ignored" % extra)

    return problems


def report(problems, label):
    errors = [m for level, m in problems if level == "error"]
    warnings = [m for level, m in problems if level == "warning"]
    for message in errors:
        print("  ERROR   %s: %s" % (label, message))
    for message in warnings:
        print("  WARNING %s: %s" % (label, message))
    return len(errors), len(warnings)


def blocking_errors(problems, baseline_problems):
    """Errors the current edit introduced.

    Some upstream ScienceBase records already fail validation, and nobody should
    be stopped from fixing a typo because of a pre-existing problem elsewhere in
    the same record.
    """
    pre_existing = {m for level, m in baseline_problems or [] if level == "error"}
    return [m for level, m in problems if level == "error" and m not in pre_existing]


# --------------------------------------------------------------------------
# Editor round-trip
# --------------------------------------------------------------------------


def open_in_editor(path):
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if editor:
        command = editor.split() + [path]
    elif shutil.which("code"):
        command = ["code", "--wait", path]
    elif shutil.which("nano"):
        command = ["nano", path]
    else:
        command = ["vi", path]
    subprocess.call(command)


def edit_in_editor(record, csc_id, item_key):
    """Open a record in the user's editor, re-prompting until it validates."""
    baseline_problems = validate_record(record, item_key, csc_id)
    handle, tmp_path = tempfile.mkstemp(prefix="casc-%s-" % item_key, suffix=".json")
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        json.dump(ordered(record, RECORD_KEY_ORDER), out, indent=2)
        out.write("\n")

    try:
        while True:
            open_in_editor(tmp_path)
            try:
                with open(tmp_path, "r", encoding="utf-8") as src:
                    edited = json.load(src)
            except ValueError as exc:
                print("\nThat file is not valid JSON: %s" % exc)
                if not confirm("Re-open the editor and fix it?", default=True):
                    return None
                continue

            problems = validate_record(edited, item_key, csc_id)
            errors = blocking_errors(problems, baseline_problems)
            if errors:
                for message in errors:
                    print("  ERROR   %s: %s" % (item_key, message))
                print("\n%d problem(s) must be fixed before saving." % len(errors))
                if not confirm("Re-open the editor?", default=True):
                    return None
                continue
            return edited
    finally:
        os.unlink(tmp_path)


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------


def ask(prompt, default=None, required=False):
    suffix = " [%s]" % default if default else ""
    while True:
        answer = input("%s%s: " % (prompt, suffix)).strip()
        if not answer and default is not None:
            return default
        if answer:
            return answer
        if not required:
            return None
        print("  This field is required.")


def ask_list(prompt):
    answer = input("%s (comma separated, blank for none): " % prompt).strip()
    if not answer:
        return []
    return [part.strip() for part in answer.split(",") if part.strip()]


def confirm(prompt, default=False):
    suffix = "[Y/n]" if default else "[y/N]"
    answer = input("%s %s " % (prompt, suffix)).strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def ask_people(group_label):
    people = []
    print("  %s (blank name to finish)" % group_label)
    while True:
        name = input("    name: ").strip()
        if not name:
            return people
        organization = input("    organization: ").strip() or None
        people.append({"name": name, "organization": organization})


# --------------------------------------------------------------------------
# Dot-path access for `set`
# --------------------------------------------------------------------------


def parse_path(path):
    parts = []
    for chunk in path.split("."):
        if not chunk:
            raise ToolError("Field path '%s' has an empty segment" % path)
        parts.append(int(chunk) if chunk.isdigit() else chunk)
    return parts


def set_path(record, path, value):
    parts = parse_path(path)
    cursor = record
    for index, part in enumerate(parts[:-1]):
        trail = ".".join(str(p) for p in parts[: index + 1])
        if isinstance(part, int):
            if not isinstance(cursor, list) or part >= len(cursor):
                raise ToolError("'%s' does not exist" % trail)
            cursor = cursor[part]
        else:
            if not isinstance(cursor, dict):
                raise ToolError("'%s' is not an object" % trail)
            if part not in cursor or cursor[part] is None:
                cursor[part] = {}
            cursor = cursor[part]

    last = parts[-1]
    if isinstance(last, int):
        if not isinstance(cursor, list) or last >= len(cursor):
            raise ToolError("'%s' does not exist" % path)
        cursor[last] = value
    else:
        if not isinstance(cursor, dict):
            raise ToolError("'%s' is not an object" % path)
        cursor[last] = value


def get_path(record, path):
    cursor = record
    for part in parse_path(path):
        if isinstance(part, int):
            if not isinstance(cursor, list) or part >= len(cursor):
                raise ToolError("'%s' does not exist" % path)
            cursor = cursor[part]
        else:
            if not isinstance(cursor, dict) or part not in cursor:
                raise ToolError("'%s' does not exist" % path)
            cursor = cursor[part]
    return cursor


def parse_value(raw, force_json):
    if force_json:
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise ToolError("--value is not valid JSON: %s" % exc)
    lowered = raw.strip().lower()
    if lowered == "null":
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return raw


# --------------------------------------------------------------------------
# Shared write path
# --------------------------------------------------------------------------


def write_record(args, csc_id, csc_name, item_id, record, action, baseline=None, note=None):
    """Validate, write the migrated file, sync companions, and journal."""
    baseline_problems = (
        validate_record(baseline, item_id, csc_id) if baseline is not None else []
    )
    problems = validate_record(record, item_id, csc_id)
    errors = blocking_errors(problems, baseline_problems)
    warnings = [m for level, m in problems if level == "warning"]

    for message in errors:
        print("  ERROR   %s: %s" % (item_id, message))
    for message in warnings:
        print("  WARNING %s: %s" % (item_id, message))
    if errors:
        raise ToolError("%d error(s) found; nothing was written." % len(errors))
    if warnings:
        print("  (%d warning(s) above are advisory only.)" % len(warnings))

    paths = file_paths(args.json_dir, csc_name)
    migrated = load_json(paths["migrated"])

    record = ordered(record, RECORD_KEY_ORDER)
    dates = record.get("dates")
    if isinstance(dates, dict):
        dates["updated"] = now_stamp()
        if action == "create" and not dates.get("created"):
            dates["created"] = dates["updated"]

    if args.dry_run:
        print("\nDry run; no files changed. Resulting record:\n")
        print(json.dumps(record, indent=2)[:4000])
        return

    migrated[item_id] = record
    save_json(paths["migrated"], migrated)
    print("Updated %s" % os.path.relpath(paths["migrated"], BASE_DIR))

    sync_companions(args.json_dir, csc_name, {item_id: record})

    fields, removed = (record, []) if baseline is None else diff_fields(baseline, record)
    journal(csc_name, item_id, action, fields=fields, removed=removed, note=note)
    print("Journaled to %s" % os.path.relpath(OVERRIDES_PATH, BASE_DIR))


def sync_companions(json_dir, csc_name, records):
    paths = file_paths(json_dir, csc_name)
    search = load_json(paths["search"])
    stub = load_json(paths["stub"])

    for item_id, record in records.items():
        search[item_id] = search_entry(record)
        entry = stub_entry(record)
        if entry is None:
            stub.pop(item_id, None)
        else:
            stub[item_id] = entry

    save_json(paths["search"], search)
    save_json(paths["stub"], stub)
    print(
        "Synced %s and %s"
        % (
            os.path.relpath(paths["search"], BASE_DIR),
            os.path.relpath(paths["stub"], BASE_DIR),
        )
    )


def find_record(migrated, item_id):
    if item_id in migrated:
        return item_id
    matches = [key for key in migrated if key.startswith(item_id)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ToolError("'%s' matches %d ids; use the full id." % (item_id, len(matches)))
    raise ToolError("No item with id '%s' in this CASC." % item_id)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_fields(args):
    print("Fields in a *_full_migrated.json record\n")
    width = max(len(name) for name, _, _ in FIELD_DOCS)
    for name, kind, description in FIELD_DOCS:
        print("%-*s  %s" % (width, name, kind))
        print("%s  %s\n" % (" " * width, description))


def cmd_list(args):
    csc_id, csc_name = resolve_casc(args.casc)
    migrated = load_json(file_paths(args.json_dir, csc_name)["migrated"])

    rows = []
    for item_id, record in migrated.items():
        types = record.get("types") or []
        if args.projects_only and "Project" not in types:
            continue
        title = record.get("title") or "(no title)"
        if args.contains and args.contains.lower() not in title.lower():
            continue
        rows.append((item_id, title, ", ".join(types) or "-"))

    rows.sort(key=lambda row: row[1].lower())
    print("%s: %d item(s)\n" % (csc_name, len(rows)))
    for item_id, title, types in rows[: args.limit]:
        print("%s  %-70s  %s" % (item_id, title[:70], types))
    if len(rows) > args.limit:
        print("\n... %d more; raise --limit to see them." % (len(rows) - args.limit))


def cmd_find(args):
    needle = args.text.lower()
    total = 0
    for csc_id, csc_name in sorted(CSCS.items(), key=lambda pair: pair[1]):
        path = file_paths(args.json_dir, csc_name)["migrated"]
        if not os.path.exists(path):
            continue
        migrated = load_json(path)
        hits = []
        for item_id, record in migrated.items():
            haystack = " ".join(
                str(record.get(field) or "")
                for field in ("title", "body", "summary", "doi")
            ).lower()
            keywords = " ".join(record.get("keywords") or []).lower()
            if needle in haystack or needle in keywords:
                hits.append((item_id, record.get("title") or "(no title)"))
        if hits:
            print("\n%s (%s)" % (csc_name, slug_for(csc_name)))
            for item_id, title in sorted(hits, key=lambda row: row[1].lower()):
                print("  %s  %s" % (item_id, title[:80]))
            total += len(hits)
    print("\n%d match(es)." % total)


def cmd_show(args):
    csc_id, csc_name = resolve_casc(args.casc)
    migrated = load_json(file_paths(args.json_dir, csc_name)["migrated"])
    item_id = find_record(migrated, args.id)
    record = migrated[item_id]
    value = get_path(record, args.field) if args.field else record
    print(json.dumps(value, indent=2, ensure_ascii=False))


def cmd_new(args):
    csc_id, csc_name = resolve_casc(args.casc)
    migrated = load_json(file_paths(args.json_dir, csc_name)["migrated"])

    if args.from_file:
        record = load_json(args.from_file)
        item_id = record.get("id") or new_id()
        record["id"] = item_id
        record.setdefault("csc", {"name": csc_name, "id": csc_id})
        if item_id in migrated:
            raise ToolError("Item %s already exists; use 'edit' instead." % item_id)
        write_record(args, csc_id, csc_name, item_id, record, "create", note=args.note)
        return

    print("New item for %s. Press Enter to skip optional fields.\n" % csc_name)
    record = blank_record(csc_id)
    record["title"] = ask("Title", required=True)
    record["body"] = ask("Description (HTML allowed)")
    record["status"] = ask("Status (Active / Completed / In Progress / Proposed)")
    record["fiscal_year"] = ask("Fiscal year (YYYY)")
    record["dates"]["start_date"] = ask("Start date (YYYY-MM-DD)")
    record["dates"]["end_date"] = ask("End date (YYYY-MM-DD)")
    record["dates"]["publication_date"] = ask("Publication date (YYYY-MM-DD)")
    record["contacts"]["principal_investigators"] = ask_people("Principal investigators")
    record["contacts"]["co_investigators"] = ask_people("Co-investigators")
    record["organizations"] = ask_list("Partner organizations")
    record["keywords"] = ask_list("Keywords")
    record["topics"] = ask_list("Topics")
    record["subtopics"] = ask_list("Subtopics")
    types = ask_list("Types (leave blank for 'Project')")
    record["types"] = types or ["Project"]
    parent = ask("Parent id", default=csc_id)
    record["parent_id"] = parent
    record["summary"] = derive_summary(record["body"])

    print("\nGenerated id: %s" % record["id"])
    if confirm("Open the full record in an editor before saving?", default=False):
        edited = edit_in_editor(record, csc_id, record["id"])
        if edited is None:
            print("Cancelled; nothing was written.")
            return
        record = edited

    if record["id"] in migrated:
        raise ToolError("Item %s already exists." % record["id"])

    write_record(args, csc_id, csc_name, record["id"], record, "create", note=args.note)


def cmd_edit(args):
    csc_id, csc_name = resolve_casc(args.casc)
    migrated = load_json(file_paths(args.json_dir, csc_name)["migrated"])
    item_id = find_record(migrated, args.id)
    baseline = copy.deepcopy(migrated[item_id])

    print("Editing: %s" % (baseline.get("title") or item_id))
    edited = edit_in_editor(baseline, csc_id, item_id)
    if edited is None:
        print("Cancelled; nothing was written.")
        return
    if edited == baseline:
        print("No changes made.")
        return

    changed, removed = diff_fields(baseline, edited)
    print("\nChanged fields: %s" % (", ".join(sorted(changed)) or "none"))
    if removed:
        print("Removed fields: %s" % ", ".join(removed))

    write_record(args, csc_id, csc_name, item_id, edited, "patch", baseline, args.note)


def cmd_set(args):
    csc_id, csc_name = resolve_casc(args.casc)
    migrated = load_json(file_paths(args.json_dir, csc_name)["migrated"])
    item_id = find_record(migrated, args.id)
    baseline = copy.deepcopy(migrated[item_id])

    record = copy.deepcopy(baseline)
    set_path(record, args.field, parse_value(args.value, args.json))

    if record == baseline:
        print("No changes made.")
        return
    print("Setting %s on %s" % (args.field, baseline.get("title") or item_id))
    write_record(args, csc_id, csc_name, item_id, record, "patch", baseline, args.note)


def cmd_delete(args):
    csc_id, csc_name = resolve_casc(args.casc)
    paths = file_paths(args.json_dir, csc_name)
    migrated = load_json(paths["migrated"])
    item_id = find_record(migrated, args.id)
    title = migrated[item_id].get("title") or item_id

    print("About to remove: %s\n  %s" % (item_id, title))
    if not args.yes and not confirm("Remove it from all three files?", default=False):
        print("Cancelled.")
        return
    if args.dry_run:
        print("Dry run; nothing was removed.")
        return

    del migrated[item_id]
    save_json(paths["migrated"], migrated)

    for kind in ("search", "stub"):
        data = load_json(paths[kind])
        if data.pop(item_id, None) is not None:
            save_json(paths[kind], data)

    journal(csc_name, item_id, "delete", note=args.note)
    print("Removed %s and journaled the deletion." % item_id)


def cmd_validate(args):
    targets = (
        sorted(CSCS.items(), key=lambda pair: pair[1])
        if args.casc in (None, "all")
        else [resolve_casc(args.casc)]
    )

    total_errors = 0
    total_warnings = 0
    for csc_id, csc_name in targets:
        paths = file_paths(args.json_dir, csc_name)
        if not os.path.exists(paths["migrated"]):
            print("%s: no migrated file, skipping" % csc_name)
            continue

        migrated = load_json(paths["migrated"])
        search = load_json(paths["search"]) if os.path.exists(paths["search"]) else {}
        print("\n%s (%d items)" % (csc_name, len(migrated)))

        errors = warnings = 0
        for item_id, record in migrated.items():
            problems = validate_record(record, item_id, csc_id)
            if not args.show_warnings:
                problems = [p for p in problems if p[0] == "error"]
            if problems:
                item_errors, item_warnings = report(problems, item_id)
                errors += item_errors
                warnings += item_warnings

        missing = [key for key in migrated if key not in search]
        if missing:
            warnings += len(missing)
            print(
                "  WARNING %d item(s) are missing from %s; run 'sync'."
                % (len(missing), os.path.basename(paths["search"]))
            )
            for item_id in missing[:10]:
                print("          %s  %s" % (item_id, migrated[item_id].get("title")))

        print("  -> %d error(s), %d warning(s)" % (errors, warnings))
        total_errors += errors
        total_warnings += warnings

    print("\nTotal: %d error(s), %d warning(s)" % (total_errors, total_warnings))
    return 1 if total_errors else 0


def cmd_sync(args):
    csc_id, csc_name = resolve_casc(args.casc)
    paths = file_paths(args.json_dir, csc_name)
    migrated = load_json(paths["migrated"])

    if args.id:
        item_id = find_record(migrated, args.id)
        records = {item_id: migrated[item_id]}
    else:
        records = migrated

    if args.dry_run:
        print("Dry run; would sync %d item(s)." % len(records))
        return
    sync_companions(args.json_dir, csc_name, records)
    print("Synced %d item(s)." % len(records))


def cmd_apply_overrides(args):
    data = load_overrides()
    buckets = data.get("cascs", {})
    if not buckets:
        print("No overrides recorded in %s" % os.path.relpath(OVERRIDES_PATH, BASE_DIR))
        return

    for slug, entries in sorted(buckets.items()):
        csc_id, csc_name = resolve_casc(slug)
        paths = file_paths(args.json_dir, csc_name)
        if not os.path.exists(paths["migrated"]):
            print("%s: no migrated file, skipping" % csc_name)
            continue

        migrated = load_json(paths["migrated"])
        touched = {}
        removed_ids = []
        skipped = []

        for item_id, entry in entries.items():
            action = entry.get("action")
            if action == "delete":
                if migrated.pop(item_id, None) is not None:
                    removed_ids.append(item_id)
                continue

            fields = entry.get("fields") or {}
            if action == "create":
                record = copy.deepcopy(fields)
            else:
                existing = migrated.get(item_id)
                if existing is None:
                    skipped.append(item_id)
                    continue
                record = copy.deepcopy(existing)
                record.update(copy.deepcopy(fields))
                for key in entry.get("removed_fields") or []:
                    record.pop(key, None)

            record["id"] = item_id
            problems = validate_record(record, item_id, csc_id)
            if any(level == "error" for level, _ in problems):
                print("\n%s / %s could not be applied:" % (csc_name, item_id))
                report([p for p in problems if p[0] == "error"], item_id)
                skipped.append(item_id)
                continue
            touched[item_id] = ordered(record, RECORD_KEY_ORDER)

        print(
            "\n%s: %d upsert(s), %d deletion(s), %d skipped"
            % (csc_name, len(touched), len(removed_ids), len(skipped))
        )
        if args.dry_run or (not touched and not removed_ids):
            continue

        migrated.update(touched)
        save_json(paths["migrated"], migrated)

        for kind in ("search", "stub"):
            companion = load_json(paths[kind])
            for item_id in removed_ids:
                companion.pop(item_id, None)
            save_json(paths[kind], companion)
        if touched:
            sync_companions(args.json_dir, csc_name, touched)

    if args.dry_run:
        print("\nDry run; no files changed.")


def cmd_overrides(args):
    data = load_overrides()
    buckets = data.get("cascs", {})
    if not buckets:
        print("No overrides recorded.")
        return
    for slug, entries in sorted(buckets.items()):
        print("\n%s (%d)" % (slug, len(entries)))
        for item_id, entry in sorted(entries.items()):
            fields = entry.get("fields") or {}
            summary = (
                "full record"
                if entry.get("action") == "create"
                else ", ".join(sorted(fields)) or "-"
            )
            print(
                "  %s  %-7s  %s  by %s"
                % (
                    item_id,
                    entry.get("action"),
                    entry.get("updated_at", "")[:19],
                    entry.get("updated_by", "?"),
                )
            )
            print("    %s" % summary[:100])


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="casc_json_tool.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--json-dir",
        default=DEFAULT_JSON_DIR,
        help="Directory holding the CASC JSON files (default: json/)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_write_flags(sub):
        sub.add_argument("--dry-run", action="store_true", help="Show the result without writing")
        sub.add_argument("--note", help="Short reason for the change, saved in the journal")

    sub = subparsers.add_parser("fields", help="Describe every field in a record")
    sub.set_defaults(func=cmd_fields)

    sub = subparsers.add_parser("list", help="List items in one CASC")
    sub.add_argument("casc", help="CASC name, slug, or id (e.g. alaska)")
    sub.add_argument("--contains", help="Only titles containing this text")
    sub.add_argument("--projects-only", action="store_true", help="Only items typed 'Project'")
    sub.add_argument("--limit", type=int, default=50)
    sub.set_defaults(func=cmd_list)

    sub = subparsers.add_parser("find", help="Search every CASC for text")
    sub.add_argument("text")
    sub.set_defaults(func=cmd_find)

    sub = subparsers.add_parser("show", help="Print one item, or one field of it")
    sub.add_argument("casc")
    sub.add_argument("id", help="Full id or unique prefix")
    sub.add_argument("--field", help="Dot path, e.g. dates.start_date or images.0.url")
    sub.set_defaults(func=cmd_show)

    sub = subparsers.add_parser("new", help="Create a new item")
    sub.add_argument("casc")
    sub.add_argument("--from-file", help="Load the record from a JSON file instead of prompting")
    add_write_flags(sub)
    sub.set_defaults(func=cmd_new)

    sub = subparsers.add_parser("edit", help="Open one item in your editor")
    sub.add_argument("casc")
    sub.add_argument("id")
    add_write_flags(sub)
    sub.set_defaults(func=cmd_edit)

    sub = subparsers.add_parser("set", help="Change a single field without an editor")
    sub.add_argument("casc")
    sub.add_argument("id")
    sub.add_argument("--field", required=True, help="Dot path, e.g. status or dates.end_date")
    sub.add_argument("--value", required=True)
    sub.add_argument("--json", action="store_true", help="Parse --value as JSON")
    add_write_flags(sub)
    sub.set_defaults(func=cmd_set)

    sub = subparsers.add_parser("delete", help="Remove an item from all three files")
    sub.add_argument("casc")
    sub.add_argument("id")
    sub.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    add_write_flags(sub)
    sub.set_defaults(func=cmd_delete)

    sub = subparsers.add_parser("validate", help="Check records against the expected shape")
    sub.add_argument("casc", nargs="?", default="all")
    sub.add_argument("--show-warnings", action="store_true")
    sub.set_defaults(func=cmd_validate)

    sub = subparsers.add_parser(
        "sync", help="Rebuild *_search.json and *_stub.json from the migrated file"
    )
    sub.add_argument("casc")
    sub.add_argument("--id", help="Only sync this item")
    sub.add_argument("--dry-run", action="store_true")
    sub.set_defaults(func=cmd_sync)

    sub = subparsers.add_parser("overrides", help="List the journaled manual edits")
    sub.set_defaults(func=cmd_overrides)

    sub = subparsers.add_parser(
        "apply-overrides",
        help="Replay journaled edits onto freshly generated files",
    )
    sub.add_argument("--dry-run", action="store_true")
    sub.set_defaults(func=cmd_apply_overrides)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except ToolError as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
