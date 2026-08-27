"""Saved jobs: a nest you can open again and cut again.

Production is repetition. "Make six more of last week's gearbox plates" was a
from-scratch rebuild every time - re-upload every DXF, re-enter the material and
thickness, re-nest the sheet - and the result was never quite the same nest twice.

A saved job is the whole setup: material, thickness, tool, Z datum, the sheet, and
every part with its placement, rotation and operations. The DXFs are saved WITH it,
because a job that depends on files still being in someone's Downloads folder is not
saved at all.

Where it lives: beside the team config, in a `penguincam-jobs/` directory - the same
place, the same lifetime, the same backups as the config itself. Not in the config
file: a job carries geometry, and a YAML config that a person edits by hand should not
grow a base64 blob in the middle of it.
"""
import base64
import datetime
import math
import json
import os
import re
import shutil
import tempfile

#: Directory name, created next to the team config the first time a job is saved.
JOBS_DIRNAME = 'penguincam-jobs'

#: Anything larger is not a plate job. A DXF this big is usually a whole assembly
#: exported by mistake, and 25 of them would fill a shop laptop.
MAX_DXF_BYTES = 4 * 1024 * 1024
MAX_PARTS = 60

#: Bumped when the on-disk shape changes in a way older readers cannot handle.
FORMAT_VERSION = 2


class JobLibraryError(ValueError):
    """A job that cannot be saved or loaded as asked."""


def _finite(value, what):
    """A coordinate that is not a real number. NaN and infinity survive `float()` and
    `json.dump` writes them as bare `NaN`/`Infinity`, which `json.load` accepts and the
    browser then places a part at - so a job saved with one is openable but unusable.
    Refused at the point it enters the library instead."""
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError):
        raise JobLibraryError(f'A part\'s {what} is not a number, so the job was not saved.')
    if not math.isfinite(number):
        raise JobLibraryError(f'A part\'s {what} is not a number, so the job was not saved.')
    return number


def _slug(name: str) -> str:
    """A filesystem-safe id derived from the job name.

    Deliberately strict: this becomes a directory name under a path the app writes to,
    so anything that could climb out of it (dots, slashes, backslashes, colons) is
    replaced rather than escaped.
    """
    slug = re.sub(r'[^a-z0-9]+', '-', str(name).strip().lower()).strip('-')
    return (slug or 'job')[:60]


def jobs_dir(config_path: str, create: bool = False) -> str:
    """Where saved jobs live for this install, or '' if there is nowhere to put them."""
    if not config_path:
        return ''
    path = os.path.join(os.path.dirname(os.path.abspath(config_path)), JOBS_DIRNAME)
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def _job_path(root: str, job_id: str) -> str:
    """The directory for one job, guaranteed to be inside `root`.

    The id comes off the wire. `_slug` already strips everything dangerous, and this
    checks the result rather than trusting it - a path that escapes the jobs directory
    would let a POST write anywhere the app can write.
    """
    candidate = os.path.abspath(os.path.join(root, _slug(job_id)))
    if os.path.commonpath([candidate, os.path.abspath(root)]) != os.path.abspath(root):
        raise JobLibraryError('That job name cannot be used as a folder name.')
    return candidate


def list_jobs(config_path: str):
    """Every saved job, newest first, without loading any geometry."""
    root = jobs_dir(config_path)
    if not root or not os.path.isdir(root):
        return []
    jobs = []
    for entry in sorted(os.listdir(root)):
        # A save in flight, or a backup a crashed save left behind, is not a job. It can
        # hold a complete job.json, so it would otherwise be listed and openable.
        if entry.endswith('.previous') or '.saving' in entry:
            continue
        meta_path = os.path.join(root, entry, 'job.json')
        if not os.path.isfile(meta_path):
            continue
        try:
            with open(meta_path, 'r', encoding='utf-8') as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue        # a damaged job should not hide the others
        jobs.append({
            'id': entry,
            'name': data.get('name') or entry,
            'saved_at': data.get('saved_at') or '',
            'part_count': len(data.get('parts') or []),
            'material': data.get('material') or '',
            'thickness_text': data.get('thickness_text') or '',
            'stock_name': (data.get('stock') or {}).get('name') or '',
        })
    jobs.sort(key=lambda j: j['saved_at'], reverse=True)
    return jobs


def save_job(config_path: str, name: str, setup: dict, parts: list):
    """Write one job: its setup, and a DXF per part.

    `parts` is a list of {name, dxf_bytes, place_x, place_y, rotation, mirror, ops}.
    Returns (job_id, path).
    """
    if not str(name or '').strip():
        raise JobLibraryError('Give the job a name you will recognise next season.')
    if not parts:
        raise JobLibraryError('A job with no parts is not worth saving.')
    if len(parts) > MAX_PARTS:
        raise JobLibraryError(f'A job can hold at most {MAX_PARTS} parts.')

    root = jobs_dir(config_path, create=True)
    if not root:
        raise JobLibraryError('There is nowhere to save jobs: no local config file.')

    job_id = _slug(name)
    path = _job_path(root, job_id)
    # A staging directory of its own per save. A shared `path + '.saving'` meant two
    # saves of the same name racing each other wrote into one directory and the loser's
    # rmtree took the winner's files with it.
    staging = tempfile.mkdtemp(prefix=os.path.basename(path) + '.saving-', dir=root)

    try:
        meta_parts = []
        for index, part in enumerate(parts):
            blob = part.get('dxf_bytes') or b''
            if not blob:
                raise JobLibraryError(f'{part.get("name") or "a part"} has no DXF to save.')
            if len(blob) > MAX_DXF_BYTES:
                raise JobLibraryError(
                    f'{part.get("name") or "a part"} is {len(blob) // 1024} KB, larger '
                    f'than the {MAX_DXF_BYTES // 1024} KB a saved part may be.')
            dxf_name = f'part_{index:02d}.dxf'
            with open(os.path.join(staging, dxf_name), 'wb') as fh:
                fh.write(blob)
            meta_parts.append({
                'name': part.get('name') or f'part {index + 1}',
                'number': str(part.get('number') or '')[:20],
                'dxf': dxf_name,
                # Both anchors. `place_x/y` is the footprint's lower-left corner, which
                # is what every other wire format in this app means by "place"; the
                # centre is what the browser actually stores per part. Saving only the
                # corner and reading it back as a centre moved every part by half its
                # own footprint, compounding on each save/open - a different nest, with
                # nothing on screen to say so.
                'place_x': _finite(part.get('place_x'), 'placement'),
                'place_y': _finite(part.get('place_y'), 'placement'),
                'center_x': _finite(part.get('center_x'), 'placement'),
                'center_y': _finite(part.get('center_y'), 'placement'),
                'label_x': (_finite(part.get('label_x'), 'label placement')
                            if part.get('label_x') is not None else None),
                'label_y': (_finite(part.get('label_y'), 'label placement')
                            if part.get('label_y') is not None else None),
                'rotation': _finite(part.get('rotation'), 'rotation'),
                'mirror': bool(part.get('mirror')),
                'ops': part.get('ops') or None,
            })

        meta = dict(setup or {})
        meta.update({
            'format': FORMAT_VERSION,
            'name': str(name).strip(),
            'saved_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'parts': meta_parts,
        })
        with open(os.path.join(staging, 'job.json'), 'w', encoding='utf-8') as fh:
            json.dump(meta, fh, indent=2, sort_keys=True)
            fh.write('\n')

        # Swap the finished directory in, so a crash mid-save cannot leave a job that
        # loads with half its parts missing.
        backup = path + '.previous'
        shutil.rmtree(backup, ignore_errors=True)
        had_previous = os.path.isdir(path)
        if had_previous:
            os.rename(path, backup)
        try:
            os.rename(staging, path)
        except OSError:
            # The new job did not land. Put the old one back rather than leaving the
            # name with nothing under it and the only copy sitting in `.previous`.
            if had_previous and not os.path.isdir(path):
                os.rename(backup, path)
            raise
        shutil.rmtree(backup, ignore_errors=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return job_id, path


def load_job(config_path: str, job_id: str) -> dict:
    """One saved job, with each part's DXF inlined as base64 so the browser can rebuild
    the parts list exactly as it would from a fresh upload."""
    root = jobs_dir(config_path)
    if not root:
        raise JobLibraryError('There are no saved jobs on this machine.')
    path = _job_path(root, job_id)
    meta_path = os.path.join(path, 'job.json')
    if not os.path.isfile(meta_path):
        raise JobLibraryError('That job is not saved on this machine.')
    with open(meta_path, 'r', encoding='utf-8') as fh:
        meta = json.load(fh)

    if int(meta.get('format') or 1) > FORMAT_VERSION:
        raise JobLibraryError('That job was saved by a newer PenguinCAM than this one.')

    parts = []
    for part in meta.get('parts') or []:
        dxf_path = os.path.join(path, os.path.basename(part.get('dxf') or ''))
        if not os.path.isfile(dxf_path):
            raise JobLibraryError(
                f'{part.get("name") or "a part"} is missing its DXF; the saved job is '
                f'incomplete and cannot be opened.')
        with open(dxf_path, 'rb') as fh:
            blob = fh.read()
        parts.append(dict(part, dxf_base64=base64.b64encode(blob).decode('ascii')))
    meta['parts'] = parts
    meta['id'] = os.path.basename(path)
    return meta


def delete_job(config_path: str, job_id: str) -> bool:
    """Remove a saved job. True if it was there."""
    root = jobs_dir(config_path)
    if not root:
        return False
    path = _job_path(root, job_id)
    if not os.path.isdir(path):
        return False
    shutil.rmtree(path)
    return True
