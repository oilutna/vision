import threading
import time
import unittest
from unittest.mock import patch
import cv2
import numpy as np
from poka_yoke_mejorado5 import Camera


class Capture:
    def __init__(self,width=240,height=200,accept_properties=True):
        self.width,self.height=width,height
        self.accept_properties=accept_properties
        self.released=False
        self.properties={}
    def isOpened(self):return True
    def set(self,key,val):
        if key in (cv2.CAP_PROP_FRAME_WIDTH,cv2.CAP_PROP_FRAME_HEIGHT):return True
        if self.accept_properties:self.properties[key]=val
        return self.accept_properties
    def get(self,key):return self.properties.get(key,0.)
    def read(self):return True,np.zeros((self.height,self.width,3),np.uint8)
    def release(self):self.released=True


def settings():
    return dict(index=0,width=240,height=200,exposure=None,focus=None,white_balance=None)


class CameraTests(unittest.TestCase):
    def wait(self,camera,predicate,timeout=2):
        until=time.monotonic()+timeout
        while time.monotonic()<until:
            value=camera.snapshot()
            if predicate(value):return value
            time.sleep(.01)
        self.fail('La cámara simulada no completó la operación.')
    def test_snapshot_reuses_only_the_consumers_copy_and_clears_on_disconnect(self):
        camera=Camera.__new__(Camera)
        camera.lock=threading.Lock()
        camera.frame=np.full((200,240,3),42,np.uint8)
        camera.timestamp=12.
        camera.info={'ready':True,'generation':1}
        first,stamp,_=camera.snapshot()
        self.assertFalse(np.shares_memory(first,camera.frame))
        reused,_,_=camera.snapshot(stamp,first)
        self.assertIs(reused,first)
        ordinary,_,_=camera.snapshot()
        self.assertIsNot(ordinary,first)
        self.assertFalse(np.shares_memory(ordinary,camera.frame))
        camera.frame=np.full((200,240,3),75,np.uint8);camera.timestamp=13.
        newer,new_stamp,_=camera.snapshot(stamp,first)
        self.assertIsNot(newer,first)
        self.assertEqual(new_stamp,13.)
        self.assertTrue(np.all(newer==75))
        camera.frame=None;camera.info['ready']=False
        missing,_,info=camera.snapshot(new_stamp,newer)
        self.assertIsNone(missing)
        self.assertFalse(info['ready'])

    def test_capture_resolution_and_release_on_close(self):
        cap=Capture()
        with patch('poka_yoke_mejorado5.cv2.VideoCapture',return_value=cap):
            camera=Camera()
            try:
                generation=camera.configure(settings())
                frame,t,info=self.wait(camera,lambda x:x[2].get('ready'))
                self.assertEqual(frame.shape,(200,240,3))
                self.assertEqual(info['generation'],generation)
                self.assertTrue(info['compatible'])
            finally:camera.close()
        self.assertTrue(cap.released)
        self.assertFalse(camera.thread.is_alive())
    def test_unsupported_setting_is_visible_and_incompatible(self):
        cap=Capture(accept_properties=False)
        with patch('poka_yoke_mejorado5.cv2.VideoCapture',return_value=cap):
            camera=Camera()
            try:
                c=settings();c['focus']=10
                camera.configure(c)
                _,_,info=self.wait(camera,lambda x:x[2].get('ready'))
                self.assertFalse(info['compatible'])
                self.assertFalse(info['properties']['focus']['verified'])
                self.assertIn('no comprobados',info['status'])
            finally:camera.close()
    def test_actual_resolution_is_not_assumed(self):
        cap=Capture(width=320,height=240)
        with patch('poka_yoke_mejorado5.cv2.VideoCapture',return_value=cap):
            camera=Camera()
            try:
                camera.configure(settings())
                _,_,info=self.wait(camera,lambda x:x[2].get('ready'))
                self.assertFalse(info['compatible'])
                self.assertEqual((info['width'],info['height']),(320,240))
            finally:camera.close()
    def test_open_failure_and_stale_generation_cannot_publish_frame(self):
        with patch('poka_yoke_mejorado5.cv2.VideoCapture',side_effect=RuntimeError('desconectada')):
            camera=Camera()
            try:
                g=camera.configure(settings())
                self.wait(camera,lambda x:'desconectada' in x[2]['status'])
                camera._publish(g-1,np.zeros((200,240,3),np.uint8),ready=True)
                frame,_,info=camera.snapshot()
                self.assertIsNone(frame)
                self.assertFalse(info['ready'])
            finally:camera.close()

    def test_windows_fallback_if_a_driver_opens_but_never_returns_images(self):
        first=Capture();first.read=lambda:(False,None)
        second=Capture()
        with patch.object(Camera,'backend_candidates',return_value=[(cv2.CAP_ANY,'Automático'),(cv2.CAP_DSHOW,'DirectShow')]), \
             patch('poka_yoke_mejorado5.cv2.VideoCapture',side_effect=[first,second]):
            camera=Camera()
            try:
                camera.configure(settings())
                frame,_,info=self.wait(camera,lambda x:x[2].get('ready'))
                self.assertTrue(first.released)
                self.assertEqual(info['backend'],'DirectShow')
                self.assertIsNotNone(frame)
            finally:camera.close()

    def test_camera_warmup_retries_before_reporting_no_image(self):
        cap=Capture()
        good_read=cap.read
        reads=[0]
        def read():
            reads[0]+=1
            return (False,None) if reads[0] < 3 else good_read()
        cap.read=read
        with patch('poka_yoke_mejorado5.cv2.VideoCapture',return_value=cap):
            camera=Camera()
            try:
                camera.configure(settings())
                self.wait(camera,lambda x:x[2].get('ready'))
                self.assertGreaterEqual(reads[0],3)
                self.assertFalse(cap.released)
            finally:camera.close()

    def test_read_exception_keeps_worker_available_for_reconnect(self):
        first=Capture();second=Capture()
        reads=[0]
        def broken_read():
            reads[0]+=1
            if reads[0] > 1:raise RuntimeError('fallo de lectura simulado')
            return True,np.zeros((200,240,3),np.uint8)
        first.read=broken_read
        with patch('poka_yoke_mejorado5.cv2.VideoCapture',side_effect=[first,second]):
            camera=Camera()
            try:
                camera.configure(settings())
                self.wait(camera,lambda x:'fallo de lectura simulado' in x[2]['status'])
                self.assertTrue(camera.thread.is_alive())
                self.assertTrue(first.released)
                self.assertIsNone(camera.snapshot()[0])
                camera.configure(settings())
                self.wait(camera,lambda x:x[2].get('ready'))
                self.assertIsNotNone(camera.snapshot()[0])
            finally:camera.close()


if __name__=='__main__':unittest.main()
