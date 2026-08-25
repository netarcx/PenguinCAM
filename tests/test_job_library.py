"""Saved jobs: a nest you can open again and cut again.

The thing being stored is not just settings - it is geometry plus placement, and it has
to come back EXACTLY, because "make six more" means six more of the same part in the
same places. So the round-trip is the central test.

The rest is about a store that writes files to disk on behalf of a web request: the name
comes off the wire and becomes a directory, sizes come off the wire and become file
writes, and a crash mid-save must not leave a job that opens with half its parts.
"""
import base64
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import job_library
import local_mode

DXF = b'0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n'


def _part(name, x=0.0, y=0.0, rotation=0.0, blob=DXF):
    return {'name': name, 'dxf_bytes': blob, 'place_x': x, 'place_y': y,
            'rotation': rotation, 'mirror': False, 'ops': None}


class JobLibraryTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='jobs_')
        self.config = os.path.join(self.dir, 'PenguinCAM-config.yaml')
        io.open(self.config, 'w', encoding='utf-8').write('version: 2\n')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_job_round_trips_exactly(self):
        """Placements included: "make six more" means six more in the same places."""
        setup = {'material': 'aluminum', 'thickness': 0.25, 'thickness_text': '0.25"',
                 'z_datum': 'stock_top'}
        parts = [_part('GEARBOX-L', 0.25, 0.25), _part('GEARBOX-R', 5.5, 0.25, 90.0)]
        job_id, _ = job_library.save_job(self.config, 'Gearbox plates', setup, parts)

        loaded = job_library.load_job(self.config, job_id)
        self.assertEqual(loaded['name'], 'Gearbox plates')
        self.assertEqual(loaded['material'], 'aluminum')
        self.assertEqual(loaded['z_datum'], 'stock_top')
        self.assertEqual([p['name'] for p in loaded['parts']], ['GEARBOX-L', 'GEARBOX-R'])
        self.assertAlmostEqual(loaded['parts'][1]['place_x'], 5.5)
        self.assertAlmostEqual(loaded['parts'][1]['rotation'], 90.0)
        for part in loaded['parts']:
            self.assertEqual(base64.b64decode(part['dxf_base64']), DXF,
                             'the DXF did not survive the round trip')

    def test_the_dxfs_are_stored_with_the_job(self):
        """A job that depended on files still being in someone's Downloads folder would
        not be saved at all."""
        job_id, path = job_library.save_job(self.config, 'Job', {}, [_part('A')])
        written = sorted(os.listdir(path))
        self.assertIn('job.json', written)
        self.assertTrue([f for f in written if f.endswith('.dxf')])

    def test_saving_the_same_name_replaces_it(self):
        job_library.save_job(self.config, 'Plates', {}, [_part('A'), _part('B')])
        job_library.save_job(self.config, 'Plates', {}, [_part('C')])
        jobs = job_library.list_jobs(self.config)
        self.assertEqual(len(jobs), 1, 'a re-save must replace, not accumulate')
        self.assertEqual(jobs[0]['part_count'], 1)
        loaded = job_library.load_job(self.config, jobs[0]['id'])
        self.assertEqual([p['name'] for p in loaded['parts']], ['C'],
                         'the old parts survived a replace')

    def test_listing_does_not_need_the_geometry(self):
        job_library.save_job(self.config, 'Plates', {'material': 'plywood'},
                             [_part('A'), _part('B')])
        entry = job_library.list_jobs(self.config)[0]
        self.assertEqual(entry['part_count'], 2)
        self.assertEqual(entry['material'], 'plywood')
        self.assertNotIn('parts', entry)

    def test_a_hostile_name_cannot_escape_the_jobs_directory(self):
        """The name comes off the wire and becomes a directory under a path the app
        writes to."""
        root = os.path.abspath(job_library.jobs_dir(self.config, create=True))
        for name in ('../../escape', '..\\..\\win', '/etc/passwd', 'a/../../b', '....//x'):
            _, path = job_library.save_job(self.config, name, {}, [_part('A')])
            self.assertTrue(os.path.abspath(path).startswith(root + os.sep),
                            f'{name!r} wrote to {path}')
        self.assertEqual(sorted(os.listdir(self.dir)),
                         ['PenguinCAM-config.yaml', job_library.JOBS_DIRNAME])

    def test_an_oversized_part_is_refused(self):
        big = b'0' * (job_library.MAX_DXF_BYTES + 1)
        with self.assertRaises(job_library.JobLibraryError):
            job_library.save_job(self.config, 'Huge', {}, [_part('A', blob=big)])

    def test_an_empty_or_unnamed_job_is_refused(self):
        with self.assertRaises(job_library.JobLibraryError):
            job_library.save_job(self.config, 'Nothing', {}, [])
        with self.assertRaises(job_library.JobLibraryError):
            job_library.save_job(self.config, '   ', {}, [_part('A')])

    def test_a_job_missing_a_dxf_refuses_to_open(self):
        """Better than opening with a part silently absent from the nest."""
        job_id, path = job_library.save_job(self.config, 'Plates', {},
                                            [_part('A'), _part('B')])
        os.remove(os.path.join(path, 'part_01.dxf'))
        with self.assertRaises(job_library.JobLibraryError) as caught:
            job_library.load_job(self.config, job_id)
        self.assertIn('incomplete', str(caught.exception))

    def test_a_damaged_job_does_not_hide_the_others(self):
        job_library.save_job(self.config, 'Good one', {}, [_part('A')])
        _, broken = job_library.save_job(self.config, 'Broken one', {}, [_part('A')])
        io.open(os.path.join(broken, 'job.json'), 'w', encoding='utf-8').write('{not json')
        names = [j['name'] for j in job_library.list_jobs(self.config)]
        self.assertIn('Good one', names)

    def test_a_job_from_a_newer_version_is_refused_not_misread(self):
        job_id, path = job_library.save_job(self.config, 'Future', {}, [_part('A')])
        meta_path = os.path.join(path, 'job.json')
        meta = json.load(io.open(meta_path, encoding='utf-8'))
        meta['format'] = job_library.FORMAT_VERSION + 1
        json.dump(meta, io.open(meta_path, 'w', encoding='utf-8'))
        with self.assertRaises(job_library.JobLibraryError):
            job_library.load_job(self.config, job_id)

    def test_delete_removes_the_job_and_its_dxfs(self):
        job_id, path = job_library.save_job(self.config, 'Plates', {}, [_part('A')])
        self.assertTrue(job_library.delete_job(self.config, job_id))
        self.assertFalse(os.path.exists(path))
        self.assertFalse(job_library.delete_job(self.config, job_id))

    def test_no_config_means_nowhere_to_save(self):
        os.remove(self.config)
        saved = os.environ.get(local_mode.CONFIG_ENV_VAR)
        os.environ[local_mode.CONFIG_ENV_VAR] = self.config
        try:
            self.assertEqual(job_library.list_jobs(''), [])
            with self.assertRaises(job_library.JobLibraryError):
                job_library.save_job('', 'Plates', {}, [_part('A')])
        finally:
            if saved is None:
                os.environ.pop(local_mode.CONFIG_ENV_VAR, None)
            else:
                os.environ[local_mode.CONFIG_ENV_VAR] = saved


if __name__ == '__main__':
    unittest.main()
