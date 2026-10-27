import ast
import copy
import gc
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import numpy as np

from poka_yoke_mejorado5 import Store,VisionEngine,InspectionCycle,canonical,build_photo_comparison
from tests.helpers import make_model,images

class MotorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.s=Store(self.temp.name)
        self.m=make_model(self.s); self.obj,self.good,self.bad=images()
    def tearDown(self):
        gc.collect(); self.temp.cleanup()
    def versions(self):
        a=self.obj.copy(); b=self.obj.copy()
        for x,y,X,Y in [(5,5,18,23),(85,5,98,23)]: a[y:Y,x:X]=100; b[y:Y,x:X]=140
        hybrid=a.copy(); hybrid[5:23,85:98]=b[5:23,85:98]
        c=copy.deepcopy(self.m['config']); c['references']=[self.s.put_image(self.m['id'],q) for q in [a]*5+[b]*5]
        self.m=self.s.save(self.m['id'],c)
        return a,b,hybrid
    def full(self,obj,x=60,y=40):
        f=np.full_like(self.good,25); f[y:y+120,x:x+120]=obj; return f
    def test_complete_versions_accepted_hybrid_rejected(self):
        a,b,hybrid=self.versions(); e=VisionEngine(self.s,self.m)
        for q in [a,b]:
            r,_,n=e.evaluate_sample(self.full(q),dict(kind='full')); self.assertIsNotNone(r,n); self.assertTrue(r['ok'])
        r,_,_=e.evaluate_sample(self.full(hybrid),dict(kind='full')); self.assertFalse(r['ok'])
    def test_representative_is_real_and_unblended(self):
        self.versions(); e=VisionEngine(self.s,self.m)
        self.assertIn(e.representative_index,range(10))
        self.assertEqual(len(e.reference_caches),10)
        # No spread between different approved pieces can inflate pixel tolerance.
        for c in e.reference_caches: self.assertTrue(np.all(c['umbral'][...,0]==30))
        self.assertTrue(any(np.array_equal(e.pattern_cache['reference_bgr'],c['reference_bgr']) for c in e.reference_caches))
    def test_good_bad_and_translation(self):
        e=VisionEngine(self.s,self.m)
        for f,ok in [(self.good,True),(self.bad,False),(self.full(self.obj,10,5),True)]:
            r,_,n=e.evaluate_sample(f,dict(kind='full')); self.assertIsNotNone(r,n); self.assertEqual(r['ok'],ok)
    def test_alignment_failure_blocks_decision(self):
        e=VisionEngine(self.s,self.m)
        with patch.object(e,'_alinear_a',return_value=None):
            r,_,note=e.evaluate_sample(self.good,dict(kind='full'))
        self.assertIsNone(r); self.assertIn('alineación',note)
    def test_last_frame_defect_cannot_be_averaged_into_ok(self):
        e=VisionEngine(self.s,self.m); cy=InspectionCycle(e,require_clear=False)
        events=[]
        for i in range(14):
            out=cy.process(self.bad if i==13 else self.good,i*.1)
            if out['event']: events.append(out['event'])
        self.assertEqual(events,[])
        for i in range(14,26):
            out=cy.process(self.bad,i*.1)
            if out['event']: events.append(out['event'])
        self.assertEqual(len(events),1); self.assertFalse(events[0]['ok'])
    def test_comparison_dialog_shows_the_reference_used(self):
        a,b,_=self.versions(); e=self.s.put_image(self.m['id'],self.full(b)); e['kind']='full'
        packet=build_photo_comparison(self.s,self.m,e)
        self.assertTrue(packet['result']['ok']); self.assertIn('referencia completa',packet['reference_title'])
        self.assertGreaterEqual(packet['result']['reference_index'],5)
    def test_previous_approval_fingerprint_invalidated(self):
        import hashlib,json
        old=hashlib.sha256(json.dumps(self.m['config'],sort_keys=True,allow_nan=False,separators=(',',':')).encode()).hexdigest()
        self.assertNotEqual(old,canonical(self.m['config']))
    def test_identical_references_preserve_a_real_sharp_pattern(self):
        e=VisionEngine(self.s,self.m)
        self.assertTrue(np.array_equal(e.pattern_cache['reference_bgr'],self.obj))
        self.assertEqual(e.aligned_references,10)

if __name__=='__main__': unittest.main(verbosity=2)
