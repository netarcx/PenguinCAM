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


def _part(name, x=0.0, y=0.0, rotation=0.0, blob=DXF, mirror=False, ops=None,
          cx=None, cy=None):
    # mirror and ops are parameters, not constants. Hardcoding them False/None made the
    # round-trip test unable to fail for either field: dropping `mirror` entirely from
    # the writer left every test green, and a re-cut nest came back un-mirrored.
    return {'name': name, 'dxf_bytes': blob, 'place_x': x, 'place_y': y,
            'center_x': x if cx is None else cx, 'center_y': y if cy is None else cy,
            'rotation': rotation, 'mirror': mirror, 'ops': ops}


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


class SavedJobFidelityTest(unittest.TestCase):
    """What a saved job has to bring back, beyond "it loaded"."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='jobfid_')
        self.config = os.path.join(self.dir, 'PenguinCAM-config.yaml')
        io.open(self.config, 'w', encoding='utf-8').write('version: 2\n')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_mirrored_part_comes_back_mirrored(self):
        """Re-cutting a saved nest with a part silently un-mirrored produces a left
        bracket where a right one was wanted, and it looks correct on screen."""
        parts = [_part('GEARBOX-R', 1.0, 2.0, mirror=True)]
        job_id, _ = job_library.save_job(self.config, 'Mirrored', {}, parts)
        loaded = job_library.load_job(self.config, job_id)
        self.assertTrue(loaded['parts'][0]['mirror'])

    def test_per_part_operations_come_back(self):
        ops = [{'op_type': 'profile', 'tool_slot': 1, 'depth': None, 'scope': {}},
               {'op_type': 'drill', 'tool_slot': 2, 'depth': 0.3, 'scope': {}}]
        job_id, _ = job_library.save_job(self.config, 'Multi', {}, [_part('P', ops=ops)])
        loaded = job_library.load_job(self.config, job_id)
        self.assertEqual(loaded['parts'][0]['ops'], ops)

    def test_both_placement_anchors_survive(self):
        """The corner AND the centre. Writing only the corner and reading it back as a
        centre moved every part by half its own footprint, compounding on each cycle."""
        parts = [_part('P', x=2.0, y=3.0, cx=4.0, cy=5.5)]
        job_id, _ = job_library.save_job(self.config, 'Anchors', {}, parts)
        back = job_library.load_job(self.config, job_id)['parts'][0]
        self.assertEqual((back['place_x'], back['place_y']), (2.0, 3.0))
        self.assertEqual((back['center_x'], back['center_y']), (4.0, 5.5))

    def test_a_placement_that_is_not_a_number_is_refused_at_the_door(self):
        """NaN survives float() and json.dump writes it as bare NaN, which json.load
        accepts - so the job saves, opens, and places a part nowhere."""
        for bad in (float('nan'), float('inf')):
            with self.assertRaises(job_library.JobLibraryError):
                job_library.save_job(self.config, 'Bad', {},
                                     [_part('P', x=bad)])


class JobRouteTest(unittest.TestCase):
    """The routes, over HTTP. save -> list -> open -> delete, the way the browser does
    it. None of this had a test: the base64 decode, the part assembly and the status
    mapping were all only exercised by hand."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='jobroute_')
        self.path = os.path.join(self.dir, 'PenguinCAM-config-2129.yaml')
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        shutil.copy(os.path.join(repo, 'PenguinCAM-config-2129.yaml'), self.path)
        self._env = {k: os.environ.get(k) for k in (local_mode.CONFIG_ENV_VAR,
                                                    local_mode.LOCAL_ENV_VAR)}
        os.environ[local_mode.CONFIG_ENV_VAR] = self.path
        os.environ[local_mode.LOCAL_ENV_VAR] = '1'
        import frc_cam_gui_app as gui
        self.gui = gui
        self._local = gui.LOCAL_MODE
        gui.LOCAL_MODE = True
        self._limiter = gui.limiter.enabled
        gui.limiter.enabled = False
        self.client = gui.app.test_client()

    def tearDown(self):
        self.gui.LOCAL_MODE = self._local
        self.gui.limiter.enabled = self._limiter
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.dir, ignore_errors=True)

    def _spec(self, name='Gearbox nest'):
        return {'name': name,
                'setup': {'material': 'aluminum', 'thickness': 0.25},
                'parts': [{'name': 'GEARBOX-L', 'dxf_base64': base64.b64encode(DXF).decode(),
                           'place_x': 0.25, 'place_y': 0.25,
                           'center_x': 2.25, 'center_y': 1.75,
                           'rotation': 90, 'mirror': True, 'ops': None}]}

    def test_save_list_open_delete(self):
        response = self.client.post('/jobs/save', json=self._spec())
        self.assertEqual(response.status_code, 200, msg=response.get_data(as_text=True))
        job_id = response.get_json()['saved_id']

        listing = self.client.get('/jobs').get_json()
        self.assertIn('Gearbox nest', [j['name'] for j in listing['jobs']])

        opened = self.client.post('/jobs/open', json={'id': job_id}).get_json()
        part = opened['job']['parts'][0]
        self.assertEqual(part['name'], 'GEARBOX-L')
        self.assertEqual(base64.b64decode(part['dxf_base64']), DXF)
        self.assertEqual((part['center_x'], part['center_y']), (2.25, 1.75))
        self.assertTrue(part['mirror'])
        self.assertEqual(part['rotation'], 90)

        self.assertEqual(self.client.post('/jobs/delete', json={'id': job_id}).status_code, 200)
        self.assertEqual([j['name'] for j in self.client.get('/jobs').get_json()['jobs']], [])

    def test_a_part_that_did_not_arrive_intact_is_refused(self):
        spec = self._spec()
        spec['parts'][0]['dxf_base64'] = 'not base64 at all!!'
        response = self.client.post('/jobs/save', json=spec)
        self.assertEqual(response.status_code, 400)

    def test_opening_a_job_that_is_not_there(self):
        self.assertEqual(self.client.post('/jobs/open', json={'id': 'nope'}).status_code, 404)

    def test_a_job_needs_a_name_and_a_part(self):
        self.assertEqual(self.client.post('/jobs/save', json={'name': '', 'parts': []}).status_code, 400)


class JobStagingTest(unittest.TestCase):
    """The half-written states a save passes through must never look like a job."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='jobstage_')
        self.config = os.path.join(self.dir, 'PenguinCAM-config.yaml')
        io.open(self.config, 'w', encoding='utf-8').write('version: 2\n')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_save_in_flight_is_not_listed_or_openable(self):
        job_id, path = job_library.save_job(self.config, 'Nest', {}, [_part('P')])
        root = os.path.dirname(path)
        # Whatever a crashed save leaves behind holds a complete job.json, so it would
        # otherwise be listed as a job and opened as one.
        for leftover in (path + '.saving-abc123', path + '.previous'):
            shutil.copytree(path, leftover)
        names = [j['id'] for j in job_library.list_jobs(self.config)]
        self.assertEqual(names, [job_id])

    def test_two_saves_of_the_same_name_do_not_share_a_staging_directory(self):
        """A shared `<job>.saving` meant the loser's cleanup deleted the winner's files
        out from under it."""
        first = job_library.save_job(self.config, 'Nest', {}, [_part('A')])[1]
        second = job_library.save_job(self.config, 'Nest', {}, [_part('B')])[1]
        self.assertEqual(first, second)      # same name, same slug, same directory
        loaded = job_library.load_job(self.config, os.path.basename(second))
        self.assertEqual([p['name'] for p in loaded['parts']], ['B'])
        # And nothing was left lying around.
        root = os.path.dirname(second)
        self.assertEqual([e for e in os.listdir(root) if '.saving' in e or
                          e.endswith('.previous')], [])

    def test_a_failed_swap_leaves_the_previous_job_in_place(self):
        """Not an empty name with the only copy stranded in `.previous`."""
        job_library.save_job(self.config, 'Nest', {}, [_part('GOOD')])
        real_rename = os.rename
        calls = {'n': 0}

        def flaky(src, dst):
            calls['n'] += 1
            if calls['n'] == 2:              # the staging -> path swap
                raise OSError('disk full')
            return real_rename(src, dst)

        os.rename = flaky
        try:
            with self.assertRaises(OSError):
                job_library.save_job(self.config, 'Nest', {}, [_part('NEW')])
        finally:
            os.rename = real_rename
        loaded = job_library.load_job(self.config, 'nest')
        self.assertEqual([p['name'] for p in loaded['parts']], ['GOOD'])
