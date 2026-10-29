import copy
import gc
import os
import queue
import threading
import unittest
from unittest.mock import patch

import cv2
from poka_yoke_mejorado5 import Camera
from tests import test_camera as camera_tests

Capture = camera_tests.Capture
settings = camera_tests.settings


class CameraDiscoveryTests(unittest.TestCase):
    wait = camera_tests.CameraTests.wait

    def test_scan_releases_preview_and_probes_then_restores_requested_camera(self):
        captures = []
        active = []
        def open_capture(index, *args):
            self.assertFalse(active, 'Two devices must not be opened simultaneously')
            cap = Capture()
            if index not in (0, 2):
                cap.isOpened = lambda: False
            active.append(cap)
            def release():
                cap.released = True
                active.remove(cap)
            cap.release = release
            captures.append((index, cap))
            return cap
        with patch.object(Camera, 'backend_candidates', return_value=[(cv2.CAP_ANY, 'Automático')]), \
             patch('poka_yoke_mejorado5.cv2.VideoCapture', side_effect=open_capture):
            camera = Camera()
            try:
                config = settings()
                camera.configure(config)
                self.wait(camera, lambda x: x[2].get('ready'))
                original = captures[0][1]
                generation = camera.discover(config)
                result_generation, devices = camera.discovery_results.get(timeout=3)
                self.assertEqual(result_generation, generation)
                self.assertEqual([d['index'] for d in devices], [0, 2])
                self.wait(camera, lambda x: x[2].get('ready') and x[2]['generation'] == generation)
                self.assertTrue(original.released)
                self.assertEqual(captures[-1][0], config['index'])
                self.assertTrue(all(cap.released for _, cap in captures[:-1]))
                self.assertNotIn('_scan_indices', config)
            finally:
                camera.close()
            self.assertTrue(all(cap.released for _, cap in captures))

    def test_no_devices_and_cancelled_scan_return_without_fabricating_candidates(self):
        camera = Camera.__new__(Camera)
        camera.stop_event = threading.Event()
        camera.generation = 3
        camera.discovery_results = queue.Queue()
        camera._first_frame = None
        with patch.object(camera, '_publish'), patch.object(camera, '_open', side_effect=RuntimeError('busy')) as opening:
            camera._discover(settings(), [0, 1], 3)
            self.assertEqual(camera.discovery_results.get_nowait(), (3, []))
            self.assertEqual(opening.call_count, 2)
            camera._discover(settings(), [0, 1], 2)
            self.assertEqual(opening.call_count, 2)
            self.assertEqual(camera.discovery_results.get_nowait(), (2, []))


@unittest.skipUnless(os.name == 'nt' or os.environ.get('DISPLAY'), 'Requires graphical session')
class CameraSelectorGuiTests(unittest.TestCase):
    # Reuse the existing application's simulated camera fixture, without real USB access.
    from tests.test_gui import GuiTests as _Fixture
    setUp = _Fixture.setUp
    pump = _Fixture.pump
    wait_idle = _Fixture.wait_idle
    admin = _Fixture.admin
    model_in_editor = _Fixture.model_in_editor

    def tearDown(self):
        gc.collect()
        self._Fixture.tearDown(self)

    def test_selector_connects_without_a_model_and_validates_manual_index(self):
        self.admin()
        self.root.geometry('1000x680')
        self.pump()
        self.assertTrue(self.app.camera_choices.winfo_ismapped())
        self.assertTrue(self.app.scan_camera_button.winfo_ismapped())
        right = self.app.scan_camera_button.winfo_rootx() + self.app.scan_camera_button.winfo_width()
        self.assertLessEqual(right, self.root.winfo_rootx() + self.root.winfo_width())
        self.app.camera_choice.set('2 · 1280 × 720 · imagen disponible')
        self.app.connect_selected_camera()
        self.assertEqual(self.app.camera_requested['index'], 2)
        self.assertFalse(self.app.running)
        self.assertIsNone(self.app.model)
        for invalid in ('USB', '-1', '21'):
            self.app.camera_choice.set(invalid)
            with self.assertRaises(ValueError):
                self.app.connect_selected_camera()

    def test_selected_camera_updates_draft_and_saved_model_preserving_references(self):
        self.admin()
        model = self.model_in_editor()
        refs = copy.deepcopy(model['config']['references'])
        self.app.camera_choice.set('2')
        self.app.connect_selected_camera()
        self.assertTrue(self.app.dirty)
        self.assertEqual(self.app.draft['camera']['index'], 2)
        self.assertEqual(self.app.draft['references'], refs)
        self.app.save_model()
        self.wait_idle()
        self.assertEqual(self.app.store.read(model['id'])['config']['camera']['index'], 2)
        self.assertEqual(self.app.model['config']['references'], refs)

    def test_discovery_is_asynchronous_updates_list_and_does_not_change_selection(self):
        self.admin()
        self.camera.discovery_results = queue.Queue()
        self.camera.discover = self.camera.configure
        self.app.camera_choice.set('0')
        self.app.find_cameras()
        generation = self.app.camera_scan_generation
        self.camera.discovery_results.put((generation, [dict(index=2, width=640, height=480)]))
        self.app._consume_camera_discovery()
        self.assertIsNone(self.app.camera_scan_generation)
        self.assertTrue(self.app.camera_choices['values'][0].startswith('2 · '))
        self.assertEqual(self.app.camera_choice.get(), '0')
        self.app.find_cameras()
        cancelled = self.app.camera_scan_generation
        self.app.camera_choice.set('2')
        self.app.connect_selected_camera()
        self.camera.discovery_results.put((cancelled, []))
        self.app._consume_camera_discovery()
        self.assertIsNone(self.app.camera_scan_generation)
        self.assertIn('2 · ', self.app.camera_choices['values'][0])


if __name__ == '__main__':
    unittest.main()
