import copy
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from poka_yoke_mejorado5 import Store, canonical
from poka_yoke_mejorado5 import VisionEngine,InspectionCycle,FRAMES_ESTABLES,FRAMES_PROMEDIO
from tests.helpers import make_model,add_samples,images,dummy_result


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store=Store(self.temp.name)
        self.model=make_model(self.store)
        self.engine=VisionEngine(self.store,self.model)

    def test_actual_engine_accepts_reference_and_rejects_missing_component(self):
        _,good,bad=images()
        yes,_,_=self.engine.evaluate_sample(good,dict(kind='full'))
        no,_,_=self.engine.evaluate_sample(bad,dict(kind='full'))
        self.assertTrue(yes['ok'])
        self.assertFalse(no['ok'])
        self.assertTrue(any(not z['ok'] for z in no['zonas']))
        self.assertEqual(yes['zonas'][0]['name'],'Componente 1')
        self.assertEqual(self.engine.aligned_references,10)

    def test_wrong_resolution_is_blocked(self):
        with self.assertRaisesRegex(ValueError,'Resolución incompatible'):
            self.engine.evaluate_sample(np.zeros((300,400,3),np.uint8),dict(kind='full'))

    def test_missing_or_unalignable_reference_blocks_start(self):
        m=copy.deepcopy(self.model)
        m['config']['references'][9]=None
        with self.assertRaises(ValueError):VisionEngine(self.store,m)
        with patch.object(VisionEngine,'_alinear_a',return_value=None):
            with self.assertRaisesRegex(ValueError,'No se pudieron alinear'):VisionEngine(self.store,self.model)

    def test_ng_never_enters_reference_stack(self):
        m=add_samples(self.store,self.model)
        other=VisionEngine(self.store,m)
        self.assertTrue(np.array_equal(self.engine.cache['media_f'],other.cache['media_f']))
        self.assertTrue(np.array_equal(self.engine.cache['umbral'],other.cache['umbral']))

    def test_color_normalization_can_be_disabled_explicitly(self):
        lab=np.full((120,120,3),[110,150,150],np.float32)
        self.engine.config['normalize_color']=False
        output=self.engine._normalizar_iluminacion(lab,self.engine.cache)
        self.assertTrue(np.array_equal(output[...,1:],lab[...,1:]))

    def test_full_validation_batch_with_actual_images(self):
        m=add_samples(self.store,self.model)
        e=VisionEngine(self.store,m)
        for sample in m['config']['samples']:
            image=self.store.image(m['id'],sample)
            result,annotated,note=e.evaluate_sample(image,sample)
            self.assertIsNotNone(result,note)
            self.assertEqual('OK' if result['ok'] else 'NG',sample['truth'])
            self.store.record(m,result,kind='validation',truth=sample['truth'],piece_id=sample['piece_id'],
                              sample_id=sample['id'],image=image,annotated=annotated)
        summary=self.store.validation_summary(m)
        self.assertEqual(summary['ok_accepted'],10)
        self.assertEqual(summary['ng_rejected'],10)
        self.assertEqual(summary['unevaluated'],0)
        approved=self.store.approve(m['id'],'Prueba software',aligned_references=e.aligned_references)
        self.assertEqual(approved['validation']['version'],m['version'])


class DeterministicEngine:
    def __init__(self):
        self.config={'absence_seconds':.5}
        self.cache={}
        self.score=.99
        self.position=(10,10)
    def check_frame(self,frame):pass
    def adjust(self,frame):return frame
    def locate(self,frame):return self.score,*self.position
    def _roi_alineada(self,*args):return np.ones((12,12,3),np.float32)
    def evaluate_roi(self,*args):return dummy_result()
    def annotate(self,frame,*args):return frame.copy()


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.engine=DeterministicEngine()
        self.cycle=InspectionCycle(self.engine,require_clear=False)
        self.frame=np.zeros((200,240,3),np.uint8)
        self.t=0.
    def tick(self,n=1,step=.1):
        outputs=[]
        for _ in range(n):
            self.t+=step
            outputs.append(self.cycle.process(self.frame,self.t))
        return outputs
    def events(self,outputs):return sum(o['event'] is not None for o in outputs)
    def test_motion_does_not_duplicate_latched_piece(self):
        self.assertEqual(self.events(self.tick(30)),1)
        self.engine.position=(80,80)
        self.assertEqual(self.events(self.tick(30)),0)
        self.assertTrue(self.cycle.latched)
    def test_pause_resume_preserves_latch_and_requires_new_retirement(self):
        self.assertEqual(self.events(self.tick(30)),1)
        self.cycle.interrupt()
        self.assertEqual(self.events(self.tick(30)),0)
        self.engine.score=.1
        self.tick(8)
        self.engine.score=.99
        self.assertEqual(self.events(self.tick(30)),1)
    def test_short_absence_clears_display_but_does_not_rearm(self):
        self.tick(30)
        self.engine.score=.1
        short=self.tick(2)
        self.assertTrue(all(o['result'] is None for o in short))
        self.engine.score=.99
        self.assertEqual(self.events(self.tick(30)),0)
    def test_initial_clear_and_camera_disconnect(self):
        self.cycle=InspectionCycle(self.engine,require_clear=True)
        self.assertEqual(self.events(self.tick(30)),0)
        self.engine.score=.1;self.tick(8)
        self.engine.score=.99
        self.assertEqual(self.events(self.tick(30)),1)
        self.cycle.disconnected()
        self.assertEqual(self.events(self.tick(30)),0)
    def test_low_match_never_fabricates_a_decision(self):
        self.engine.score=.1
        outputs=self.tick(100)
        self.assertEqual(self.events(outputs),0)
        self.assertTrue(all(o['result'] is None for o in outputs))
    def test_absence_interrupts_incomplete_average(self):
        self.tick(FRAMES_ESTABLES+3)
        self.assertGreater(self.cycle.count,0)
        self.engine.score=.1;self.tick(1)
        self.assertEqual(self.cycle.count,0)
        self.assertIsNone(self.cycle.accum)


if __name__=='__main__':unittest.main()
