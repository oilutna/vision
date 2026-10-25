import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import numpy as np
from poka_yoke_mejorado5 import Store,Auth,READY,PENDING,canonical,atomic_json
from tests.helpers import make_model,add_samples,images,dummy_result,record_validation


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store=Store(self.temp.name)

    def test_independent_models_and_reopen(self):
        a=make_model(self.store,'A')
        b=self.store.create('B')
        c=copy.deepcopy(a['config']);c['brightness']=37;c['zones']['1']['factor']=175
        a=self.store.save(a['id'],c)
        reopened=Store(self.temp.name)
        self.assertEqual(reopened.read(a['id'])['config']['brightness'],37)
        self.assertEqual(reopened.read(a['id'])['config']['zones']['1']['factor'],175)
        self.assertEqual(reopened.read(b['id'])['config']['brightness'],0)
        self.assertEqual(reopened.read(b['id'])['config']['zones']['1']['factor'],100)

    def test_draft_does_not_change_saved_and_previous_restores_assets(self):
        m=make_model(self.store)
        old=m['config']['references'][0]
        c=copy.deepcopy(m['config']);c['tolerance']=8000
        c['references'][0]=self.store.put_image(m['id'],np.full((120,120,3),100,np.uint8))
        self.assertEqual(self.store.read(m['id'])['config']['tolerance'],5000)
        changed=self.store.save(m['id'],c)
        restored=self.store.restore_previous(m['id'])
        self.assertGreater(restored['version'],changed['version'])
        self.assertEqual(restored['config']['tolerance'],5000)
        self.assertEqual(restored['config']['references'][0],old)
        self.assertTrue(np.array_equal(self.store.image(m['id'],old),images()[0]))

    def test_atomic_write_failure_preserves_saved_configuration(self):
        m=make_model(self.store)
        path=self.store.model_dir(m['id'])/'modelo.json'
        old=path.read_bytes()
        c=copy.deepcopy(m['config']);c['brightness']=4
        with patch('poka_yoke_mejorado5.os.replace',side_effect=OSError('disco lleno')):
            with self.assertRaises(OSError):self.store.save(m['id'],c)
        self.assertEqual(path.read_bytes(),old)
        self.assertEqual(list(path.parent.glob('.escribiendo-*')),[])

    def test_duplicate_rename_archive_and_no_overwrite(self):
        a=make_model(self.store)
        b=self.store.duplicate(a['id'],'COPIA')
        self.assertNotEqual(a['id'],b['id'])
        self.assertEqual(b['config'],a['config'])
        self.assertIsNone(b['validation'])
        renamed=self.store.metadata(b['id'],part='OTRA',description='Nueva descripción')
        self.assertEqual(renamed['id'],b['id'])
        self.store.integrity(renamed,True)
        self.assertEqual(self.store.status(self.store.metadata(b['id'],archived=True)),'Archivado')
        self.assertNotEqual(self.store.status(self.store.metadata(b['id'],archived=False)),'Archivado')
        with self.assertRaises(ValueError):self.store.metadata(b['id'],part=a['part'])

    def test_export_import_current_recipe_without_passwords(self):
        a=make_model(self.store)
        auth=Auth(self.temp.name);auth.set_password('contraseña-larga')
        target=Path(self.temp.name)/'modelo.zip'
        self.store.export_model(a['id'],target)
        with zipfile.ZipFile(target) as z:
            self.assertNotIn('administracion.json',z.namelist())
            self.assertFalse(any('historial' in n for n in z.namelist()))
        b=self.store.import_model(target,'IMPORTADO')
        self.assertEqual(a['config'],b['config'])
        self.assertNotEqual(a['id'],b['id'])
        self.assertIsNone(b['validation'])
        self.store.integrity(b,True)

    def test_photo_details_survive_model_export_import(self):
        m=add_samples(self.store,make_model(self.store),n=1)
        c=copy.deepcopy(m['config'])
        c['references'][0].update(piece_id='REFERENCIA-A',note='Variación nominal permitida')
        c['samples'][1].update(defect_type='Conector invertido',note='Lado derecho')
        m=self.store.save(m['id'],c)
        path=Path(self.temp.name)/'con_detalles.zip'
        self.store.export_model(m['id'],path)
        imported=self.store.import_model(path,'COPIA-CON-DETALLES')
        self.assertEqual(imported['config']['samples'][1]['defect_type'],'Conector invertido')
        self.assertEqual(imported['config']['samples'][1]['note'],'Lado derecho')
        self.assertEqual(imported['config']['references'][0]['piece_id'],'REFERENCIA-A')

    def test_import_rejects_path_traversal(self):
        target=Path(self.temp.name)/'malicioso.zip'
        with zipfile.ZipFile(target,'w') as z:
            z.writestr('modelo.json','{}')
            z.writestr('../fuera.txt','ataque')
        with self.assertRaises(ValueError):self.store.import_model(target,'BAD')
        self.assertFalse((Path(self.temp.name)/'fuera.txt').exists())
        self.assertEqual(self.store.list(),[])

    def test_import_rejects_tampered_assets(self):
        a=make_model(self.store)
        target=Path(self.temp.name)/'original.zip';bad=Path(self.temp.name)/'bad.zip'
        self.store.export_model(a['id'],target)
        with zipfile.ZipFile(target) as src,zipfile.ZipFile(bad,'w') as dst:
            for n in src.namelist():dst.writestr(n,b'corrupta' if n.startswith('assets/') else src.read(n))
        with self.assertRaises(ValueError):self.store.import_model(bad,'BAD')
        self.assertEqual(len(self.store.list()),1)

    def test_legacy_migration_keeps_source_and_factors(self):
        folder=Path(self.temp.name)/'legacy';folder.mkdir()
        obj,frame,ng=images()
        import cv2
        cv2.imwrite(str(folder/'VIEJO_full.png'),frame)
        np.save(folder/'VIEJO_roi.npy',[60,40,180,160])
        for i in range(10):cv2.imwrite(str(folder/f'VIEJO_ref_{i}.png'),obj)
        cv2.imwrite(str(folder/'VIEJO_ng_0.png'),ng)
        atomic_json(folder/'VIEJO_config.json',dict(brillo=7,contraste=115,tolerancia=6000,modo_zonas=3,
                    zonas_relativas={'1':[0,0,.3,1],'2':[.3,0,.6,1],'3':[.6,0,1,1]},factores_zona={'1':170}))
        before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()}
        m=self.store.import_legacy(folder,'VIEJO')
        after={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()}
        self.assertEqual(before,after)
        self.assertEqual(m['config']['zones']['1']['factor'],170)
        self.assertEqual(m['config']['tolerance'],6000)
        self.assertEqual(m['config']['camera']['width'],240)
        self.assertTrue(all(e['processed'] for e in m['config']['references']))
        self.assertEqual(m['config']['samples'][0]['truth'],'NG')
        self.assertNotEqual(self.store.status(m),READY)

    def test_authentication_hash_and_change(self):
        auth=Auth(self.temp.name)
        self.assertFalse(auth.configured)
        auth.set_password('clave-inicial')
        self.assertTrue(auth.verify('clave-inicial'))
        self.assertFalse(auth.verify('incorrecta'))
        self.assertNotIn('clave-inicial',auth.path.read_text())
        auth.set_password('clave-nueva')
        self.assertFalse(auth.verify('clave-inicial'))
        self.assertTrue(auth.verify('clave-nueva'))

    def test_incomplete_cannot_be_approved(self):
        m=self.store.create('INCOMPLETO')
        with self.assertRaises(ValueError):self.store.approve(m['id'],'Admin',aligned_references=1)
        self.assertNotEqual(self.store.status(m),READY)

    def test_validation_approval_version_binding_and_invalidation(self):
        m=add_samples(self.store,make_model(self.store))
        m=record_validation(self.store,m)
        self.assertEqual(self.store.status(m),READY)
        c=copy.deepcopy(m['config']);c['zones']['1']['factor']=110
        updated=self.store.save(m['id'],c)
        self.assertIsNone(updated['validation'])
        self.assertEqual(self.store.status(updated),PENDING)
        self.assertEqual(self.store.validation_summary(updated)['ok_pieces'],0)
        with self.assertRaises(ValueError):self.store.approve(m['id'],'Admin',aligned_references=10)

    def test_unevaluated_is_neither_false_accept_nor_reject(self):
        m=add_samples(self.store,make_model(self.store),n=1)
        for s in m['config']['samples']:
            self.store.record(m,None,kind='validation',truth=s['truth'],piece_id=s['piece_id'],sample_id=s['id'])
        summary=self.store.validation_summary(m)
        self.assertEqual(summary['unevaluated'],2)
        self.assertEqual(summary['ng_accepted'],0)
        self.assertEqual(summary['ok_rejected'],0)

    def test_repeated_specimen_does_not_increase_unique_count(self):
        m=add_samples(self.store,make_model(self.store),n=1)
        e=m['config']['samples'][0]
        for _ in range(12):self.store.record(m,dummy_result(),kind='validation',truth='OK',piece_id=e['piece_id'],sample_id=e['id'])
        self.assertEqual(self.store.validation_summary(m)['ok_pieces'],1)

    def test_unidentified_imported_samples_do_not_count_as_distinct_pieces(self):
        m=add_samples(self.store,make_model(self.store),n=1)
        c=copy.deepcopy(m['config'])
        c['samples'][1]['identity_confirmed']=False
        m=self.store.save(m['id'],c)
        e=m['config']['samples'][1]
        self.store.record(m,dummy_result(False),kind='validation',truth='NG',piece_id=e['piece_id'],sample_id=e['id'])
        self.assertEqual(self.store.validation_summary(m)['ng_pieces'],0)

    def test_reimport_repairs_a_corrupted_content_addressed_image(self):
        m=make_model(self.store)
        entry=m['config']['references'][0]
        path=self.store.model_dir(m['id'])/entry['path']
        path.write_bytes(b'archivo roto')
        with self.assertRaises(ValueError):self.store.image(m['id'],entry)
        repaired=self.store.put_image(m['id'],images()[0])
        self.assertEqual(repaired,entry)
        self.assertTrue(np.array_equal(self.store.image(m['id'],entry),images()[0]))

    def test_counts_isolated_by_model_lot_and_event_kind(self):
        a=make_model(self.store);b=make_model(self.store,'OTRA')
        self.store.record(a,dummy_result(),lot='A')
        self.store.record(a,dummy_result(False),lot='A')
        self.store.record(a,dummy_result(),lot='B')
        self.store.record(b,dummy_result(False),lot='A')
        self.store.record(a,dummy_result(False),kind='test',lot='A')
        self.assertEqual(self.store.counts(a['id'],'A'),(1,1))
        self.assertEqual(self.store.counts(a['id'],'B'),(1,0))
        with self.assertRaises(ValueError):self.store.record(a,None)

    def test_log_images_csv_and_filters(self):
        m=make_model(self.store)
        image=images()[1]
        row=self.store.record(m,dummy_result(False),operator='=no_formula',lot='L1',image=image,annotated=image)
        self.assertTrue((self.store.root/row['image']).exists())
        self.assertTrue((self.store.root/row['annotated']).exists())
        self.assertEqual(len(self.store.events(model_id=m['id'],lot='L1',outcome='NG')),1)
        self.assertEqual(self.store.events(lot='OTRO'),[])
        dest=Path(self.temp.name)/'registro.csv'
        self.store.export_csv([row],dest)
        content=dest.read_text(encoding='utf-8-sig')
        self.assertIn("'=no_formula",content)
        self.assertIn('Conector',content)

    def test_image_write_failure_does_not_register_or_count(self):
        m=make_model(self.store)
        with patch('poka_yoke_mejorado5.image_bytes',side_effect=OSError('sin espacio')):
            with self.assertRaises(OSError):self.store.record(m,dummy_result(),lot='A',image=images()[1])
        self.assertEqual(self.store.counts(m['id'],'A'),(0,0))
        self.assertEqual(self.store.events(),[])


if __name__=='__main__':unittest.main()
