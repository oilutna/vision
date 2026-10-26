import copy
import tempfile
import unittest
import numpy as np
from poka_yoke_mejorado5 import (Store, PhotoUndoHistory, build_photo_comparison, VisionEngine,
                                zone_feedback, production_phase)
from tests.helpers import make_model, add_samples, images, dummy_result


class UXDataTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store=Store(self.temp.name)
        self.model=make_model(self.store)

    def test_undo_capture_preserves_later_settings_and_unrelated_photo_details(self):
        before=copy.deepcopy(self.model['config'])
        after=copy.deepcopy(before)
        after['samples']=[dict(id='a'*32,path='new.png',piece_id='NG-01')]
        undo=PhotoUndoHistory();undo.remember(before,after,'captura')
        current=copy.deepcopy(after)
        current['zones']['1']['factor']=17
        current['references'][2]['note']='Detalle posterior que debe conservarse'
        restored,label=undo.undo(current)
        self.assertEqual(restored['samples'],[])
        self.assertEqual(restored['zones']['1']['factor'],17)
        self.assertEqual(restored['references'][2]['note'],current['references'][2]['note'])
        self.assertEqual(label,'captura')

    def test_undo_replacement_and_deletion_restore_assets_order_and_details(self):
        model=add_samples(self.store,self.model,n=3,truths=('NG',))
        before=copy.deepcopy(model['config'])
        after=copy.deepcopy(before)
        after['samples'].pop(1)
        undo=PhotoUndoHistory();undo.remember(before,after,'eliminación')
        current=copy.deepcopy(after);current['samples'][0]['note']='Otra foto editada después'
        restored,_=undo.undo(current)
        self.assertEqual([e['id'] for e in restored['samples']],[e['id'] for e in before['samples']])
        self.assertEqual(restored['samples'][1],before['samples'][1])
        self.assertEqual(restored['samples'][0]['note'],'Otra foto editada después')
        newer=copy.deepcopy(restored)
        newer['references'][0]={'path':'otro.png'};newer['base_image']={'path':'otra-base.png'}
        undo.remember(restored,newer,'repetir OK')
        recovered,_=undo.undo(newer)
        self.assertEqual(recovered,restored)
        with self.assertRaises(ValueError):undo.undo(recovered)

    def test_comparison_uses_the_same_zone_decision_and_records_nothing(self):
        model=add_samples(self.store,self.model,truths=('NG',))
        entry=model['config']['samples'][0]
        packet=build_photo_comparison(self.store,model,entry)
        expected,_,_=VisionEngine(self.store,model).evaluate_sample(self.store.image(model['id'],entry),entry)
        self.assertEqual(packet['result'],expected)
        self.assertFalse(packet['result']['ok'])
        self.assertTrue(any(not z['ok'] for z in packet['zones']))
        self.assertEqual(packet['reference'].shape,packet['sample'].shape)
        self.assertEqual(packet['sample'].shape[:2],(120,120))
        self.assertEqual(self.store.events(model_id=model['id']),[])
        self.assertIsNone(self.store.read(model['id'])['validation'])

    def test_comparison_exposes_ng_accepted_as_ok(self):
        model=add_samples(self.store,self.model,truths=('NG',))
        entry=copy.deepcopy(model['config']['samples'][0])
        entry.update(self.store.put_image(model['id'],images()[1]))
        packet=build_photo_comparison(self.store,model,entry)
        self.assertTrue(packet['result']['ok'])
        self.assertEqual(packet['entry']['truth'],'NG')
        self.assertTrue(all(z['ok'] for z in packet['zones']))

    def test_unlocatable_comparison_stays_unevaluated_and_keeps_full_photo(self):
        entry=self.store.put_image(self.model['id'],np.full((200,240,3),25,np.uint8))
        entry.update(truth='NG',kind='full',piece_id='NG-01')
        packet=build_photo_comparison(self.store,self.model,entry)
        self.assertIsNone(packet['result'])
        self.assertEqual(packet['zones'],[])
        self.assertEqual(packet['sample'].shape[:2],(200,240))
        self.assertIn('sin evaluación',packet['note'].lower())

    def test_incomplete_reference_comparison_does_not_invent_a_result(self):
        model=copy.deepcopy(self.model)
        model['config']['references'][9]=None
        different=images()[0].copy();different[0,0]=[201,202,203]
        broken=self.store.put_image(model['id'],different)
        (self.store.model_dir(model['id'])/broken['path']).write_bytes(b'foto danada')
        model['config']['references'][0]=broken
        entry=dict(self.model['config']['base_image'],kind='full',truth='NG')
        packet=build_photo_comparison(self.store,model,entry)
        self.assertIsNone(packet['result'])
        self.assertIsNotNone(packet['reference'])
        self.assertTrue(np.array_equal(packet['reference'],images()[0]))
        self.assertIn('10 fotos OK',packet['note'])

    def test_zone_feedback_and_production_phases_explain_actual_values(self):
        row=zone_feedback(dummy_result(False))[0]
        self.assertEqual(row['text'],'Diferencia 100 / límite 6 px')
        self.assertIn('94 px',row['message'])
        self.assertEqual(row['fraction'],1.)
        self.assertFalse(row['ok'])
        for state,expected in [('Esperando pieza · sin evaluación','ESPERANDO PIEZA'),
                               ('Evaluando 3/8','EVALUANDO'),
                               ('Resultado emitido · esperando retiro','RETIRE LA PIEZA'),
                               ('Retira la pieza para armar el ciclo','RETIRE LA PIEZA')]:
            self.assertEqual(production_phase(state)[0],expected)


if __name__=='__main__':unittest.main()
