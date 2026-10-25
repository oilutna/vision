import copy
import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import numpy as np
from poka_yoke_mejorado5 import Store,VisionEngine,check_saved_photos,reference_samples
from tests.helpers import make_model,add_samples,images


class PhotoDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store=Store(self.temp.name)
        self.model=add_samples(self.store,make_model(self.store),truths=('NG',))

    def replace_ng(self,image):
        c=copy.deepcopy(self.model['config'])
        c['samples'][0].update(self.store.put_image(self.model['id'],image))
        self.model=self.store.save(self.model['id'],c)
        return c['samples'][0]['id']

    def test_ng_looking_like_ok_is_identified_and_persisted(self):
        image=images()[1].copy();image[0,0]=[1,2,3]
        sid=self.replace_ng(image)
        rows=check_saved_photos(self.store,self.model)
        self.assertEqual(len(rows),20)
        report=self.store.photo_results(self.model)
        self.assertEqual(report[sid]['state'],'fail')
        self.assertEqual(report[sid]['label'],'NG pasa como OK')
        self.assertIsNotNone(report[sid]['match'])
        self.assertIn('defecto no fue detectado',report[sid]['reason'])
        reopened=Store(self.temp.name)
        self.assertEqual(reopened.photo_results(self.model)[sid]['label'],'NG pasa como OK')
        summary=self.store.validation_summary(self.model)
        self.assertEqual(summary['ng_photo_accepted'],1)
        self.assertEqual(summary['ng_photo_rejected'],9)
        with self.assertRaises(ValueError):self.store.approve(self.model['id'],'Admin',aligned_references=10)

    def test_unlocatable_photo_is_not_misreported_as_detected_ng(self):
        sid=self.replace_ng(np.full((200,240,3),27,np.uint8))
        check_saved_photos(self.store,self.model)
        report=self.store.photo_results(self.model)
        self.assertEqual(report[sid]['state'],'unevaluated')
        self.assertEqual(report[sid]['label'],'SIN EVALUAR')
        summary=self.store.validation_summary(self.model)
        self.assertEqual(summary['ng_photo_unevaluated'],1)
        self.assertEqual(summary['ng_photo_accepted'],0)

    def test_damaged_ng_does_not_stop_other_photos_or_reference_engine(self):
        entry=self.model['config']['samples'][0]
        (self.store.model_dir(self.model['id'])/entry['path']).write_bytes(b'danada')
        engine=VisionEngine(self.store,self.model)
        self.assertEqual(engine.aligned_references,10)
        rows=check_saved_photos(self.store,self.model)
        self.assertEqual(len(rows),20)
        report=self.store.photo_results(self.model)
        self.assertEqual(report[entry['id']]['state'],'fail')
        self.assertEqual(report[entry['id']]['label'],'FOTO INVÁLIDA')
        self.assertEqual(self.store.validation_summary(self.model)['reference_ok'],10)
        self.assertEqual(self.store.validation_summary(self.model)['ng_photo_rejected'],9)
        with self.assertRaises((ValueError,OSError)):
            self.store.approve(self.model['id'],'Admin',aligned_references=10)

    def test_alignment_errors_identify_the_exact_reference_slots(self):
        with patch.object(VisionEngine,'_alinear_a',return_value=None):
            rows=check_saved_photos(self.store,self.model)
        report=self.store.photo_results(self.model)
        self.assertEqual(len(rows),20)
        self.assertEqual(report['reference:1']['state'],'fail')
        self.assertIn('no se alinea',report['reference:1']['reason'])
        self.assertEqual(report['reference:9']['state'],'fail')
        self.assertEqual(report['reference:0']['state'],'unevaluated')
        with self.assertRaises(ValueError):self.store.approve(self.model['id'],'Admin',aligned_references=10)

    def test_ng_identical_to_ok_is_marked_as_not_counting(self):
        c=copy.deepcopy(self.model['config'])
        c['samples'][0].update(c['references'][0])
        c['samples'][0]['kind']='crop'
        self.model=self.store.save(self.model['id'],c)
        check_saved_photos(self.store,self.model)
        info=self.store.photo_results(self.model)[c['samples'][0]['id']]
        self.assertEqual(info['label'],'NO CUENTA COMO NG')
        self.assertEqual(info['state'],'fail')
        self.assertIn('idéntica a una OK',info['reason'])
        self.assertEqual(self.store.validation_summary(self.model)['ng_photos'],9)

    def test_saved_setting_change_does_not_reuse_old_photo_results(self):
        check_saved_photos(self.store,self.model)
        c=copy.deepcopy(self.model['config']);c['zones']['1']['factor']=150
        changed=self.store.save(self.model['id'],c)
        self.assertEqual(self.store.photo_results(changed),{})
        self.assertEqual(len(self.store.events(model_id=self.model['id'])),20)

    def test_existing_history_is_preserved_when_diagnostic_column_is_added(self):
        old=Path(self.temp.name)/'old';old.mkdir()
        with sqlite3.connect(old/'historial.sqlite3') as db:
            db.execute('''CREATE TABLE events (id TEXT PRIMARY KEY, timestamp TEXT NOT NULL,
                model_id TEXT NOT NULL, part TEXT NOT NULL, version INTEGER NOT NULL,
                fingerprint TEXT NOT NULL, kind TEXT NOT NULL, operator TEXT, lot TEXT,
                work_order TEXT, outcome TEXT NOT NULL, truth TEXT, piece_id TEXT, sample_id TEXT,
                image TEXT, annotated TEXT, result TEXT NOT NULL, note TEXT)''')
            db.execute("INSERT INTO events (id,timestamp,model_id,part,version,fingerprint,kind,outcome,result) VALUES ('old','2020-01-01','m','P',1,'x','test','OK','null')")
        migrated=Store(old)
        rows=migrated.events()
        self.assertEqual(rows[0]['id'],'old')
        self.assertIsNone(rows[0]['diagnostic'])
        with migrated.connect() as db:
            self.assertIn('diagnostic',{r[1] for r in db.execute('PRAGMA table_info(events)')})


if __name__=='__main__':unittest.main()
