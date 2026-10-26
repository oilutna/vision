import copy
import os
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch
from pathlib import Path
import numpy as np

from poka_yoke_mejorado5 import Application
from tests.helpers import make_model,add_samples,record_validation,images


class FakeCamera:
    def __init__(self):
        self.frame=np.full((200,240,3),25,np.uint8)
        self.generation=0
        self.ready=False
        self.compatible=True
    def configure(self,config):
        self.generation+=1
        self.ready=True
        return self.generation
    def snapshot(self):
        return (self.frame.copy() if self.ready else None),time.monotonic(),dict(
            status='Cámara simulada' if self.ready else 'Cámara sin abrir',ready=self.ready,
            compatible=self.compatible,generation=self.generation,width=240,height=200,properties={})
    def close(self):self.ready=False


@unittest.skipUnless(os.environ.get('DISPLAY') or os.name=='nt','Requiere entorno gráfico')
class GuiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.camera=FakeCamera()
        self.root=tk.Tk()
        self.app=Application(self.root,self.temp.name,camera=self.camera)
        self.errors=[]
        self.root.report_callback_exception=lambda exc,val,tb:self.errors.append(val)
        self.error_patch=patch('poka_yoke_mejorado5.messagebox.showerror',side_effect=lambda *a,**k:self.errors.append(a))
        self.error_patch.start()
        self.pump(.15)
    def tearDown(self):
        self.app.running=self.app.pending_start=False
        self.wait_idle()
        self.app.dirty=False
        self.app.close()
        self.error_patch.stop()
        self.temp.cleanup()
        self.assertEqual(self.errors,[])
    def pump(self,seconds=.15):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            self.root.update()
            time.sleep(.01)
    def wait_idle(self,timeout=5):
        deadline=time.monotonic()+timeout
        while self.app.task is not None and time.monotonic()<deadline:
            self.pump(.04)
        self.assertIsNone(self.app.task)
    def admin(self):
        self.app.auth.set_password('clave-de-pruebas')
        with patch('poka_yoke_mejorado5.simpledialog.askstring',return_value='clave-de-pruebas'):
            self.app.toggle_admin()
        self.assertTrue(self.app.admin_mode)
    def model_in_editor(self):
        m=make_model(self.app.store)
        self.app._refresh_catalog()
        self.app.catalog.selection_set(m['id'])
        self.app.open_model()
        self.pump()
        return m

    def test_startup_opens_preview_without_a_model_or_admin_access(self):
        self.pump(.15)
        self.assertTrue(self.camera.ready)
        self.assertGreater(self.camera.generation,0)
        self.assertIsNotNone(self.app.video.source)
        self.assertIsNone(self.app.active_model)
        self.assertFalse(self.app.admin_mode)
        self.assertFalse(self.app.running)

    def test_visible_reconnect_button_restores_preview_without_admin(self):
        previous=self.camera.generation
        self.camera.ready=False
        self.pump(.1)
        self.assertIsNone(self.app.video.source)
        self.app.camera_button.invoke()
        self.pump(.2)
        self.assertGreater(self.camera.generation,previous)
        self.assertIsNotNone(self.app.video.source)
        self.assertFalse(self.app.admin_mode)
        self.assertFalse(self.app.running)

    def test_loading_editor_opens_the_models_camera_automatically(self):
        self.admin()
        m=make_model(self.app.store)
        c=copy.deepcopy(m['config']);c['camera']['index']=1
        m=self.app.store.save(m['id'],c)
        self.app._load_editor(m)
        self.pump(.2)
        self.assertEqual(self.app.camera_requested['index'],1)
        self.assertIsNotNone(self.app.admin_video.source)

    def test_operator_cannot_edit_and_first_password_is_not_plaintext(self):
        self.assertFalse(self.app.admin_mode)
        with self.assertRaises(PermissionError):self.app.new_model()
        with patch('poka_yoke_mejorado5.simpledialog.askstring',side_effect=['primera-clave','primera-clave']):
            self.app.toggle_admin()
        self.assertTrue(self.app.admin_mode)
        self.assertNotIn('primera-clave',self.app.auth.path.read_text())
        self.app.toggle_admin()
        self.assertFalse(self.app.admin_mode)
        with self.assertRaises(PermissionError):self.app.open_camera()

    def test_save_discard_and_switch_models_without_leakage(self):
        self.admin();a=self.model_in_editor()
        self.app.fields['tolerance'].set('8000')
        self.assertTrue(self.app.dirty)
        self.assertEqual(self.app.store.read(a['id'])['config']['tolerance'],5000)
        with patch('poka_yoke_mejorado5.messagebox.askyesno',return_value=True):self.app.discard_changes()
        self.assertEqual(self.app.fields['tolerance'].get(),'5000')
        self.app.fields['tolerance'].set('7200')
        self.app.zone_fields['1'][0].set('Conector principal')
        self.app.zone_fields['1'][1].set('175')
        self.app.save_model()
        self.assertFalse(self.app.dirty)
        saved=self.app.store.read(a['id'])
        self.assertEqual(saved['config']['tolerance'],7200)
        self.assertEqual(saved['config']['zones']['1']['factor'],175)
        b=self.app.store.create('B')
        self.app._load_editor(b)
        self.assertEqual(self.app.fields['tolerance'].get(),'5000')
        self.app._load_editor(saved)
        self.assertEqual(self.app.zone_fields['1'][0].get(),'Conector principal')

    def test_zone_sliders_show_and_persist_their_own_thresholds(self):
        self.admin();m=self.model_in_editor()
        from tkinter import ttk
        self.assertIsInstance(self.app.zone_scales['1'],ttk.Scale)
        self.app.zone_scales['1'].set(200.7)
        self.app.zone_scales['2'].set(1)
        self.assertEqual(self.app.zone_values['1'].get(),'201 %')
        self.assertEqual(self.app.zone_fields['1'][1].get(),'201')
        self.assertEqual(self.app.zone_values['2'].get(),'1 %')
        self.assertEqual(self.app.store.read(m['id'])['config']['zones']['1']['factor'],100)
        self.app.save_model()
        saved=self.app.store.read(m['id'])
        self.assertEqual(saved['config']['zones']['1']['factor'],201)
        self.assertEqual(saved['config']['zones']['2']['factor'],1)
        self.assertAlmostEqual(self.app.zone_scales['1'].get(),201)
        self.assertFalse(self.app.dirty)
        other=self.app.store.create('OTRO-NUMERO')
        self.app._load_editor(other)
        self.assertAlmostEqual(self.app.zone_scales['1'].get(),100)
        self.app._load_editor(saved)
        self.assertAlmostEqual(self.app.zone_scales['1'].get(),201)
        self.assertAlmostEqual(self.app.zone_scales['2'].get(),1)

    def test_zone_sliders_follow_three_or_five_zone_selection(self):
        self.admin();self.model_in_editor()
        self.assertTrue(self.app.zone_scales['4'].instate(['disabled']))
        self.assertFalse(self.app.zone_scales['3'].instate(['disabled']))
        self.app.fields['zone_count'].set('5')
        self.assertFalse(self.app.zone_scales['4'].instate(['disabled']))
        self.assertFalse(self.app.zone_scales['5'].instate(['disabled']))
        self.app.fields['zone_count'].set('3')
        self.assertTrue(self.app.zone_scales['4'].instate(['disabled']))

    def test_unsaved_cancel_prevents_logout(self):
        self.admin();self.model_in_editor()
        self.app.fields['brightness'].set('12')
        with patch('poka_yoke_mejorado5.messagebox.askyesnocancel',return_value=None):self.app.toggle_admin()
        self.assertTrue(self.app.admin_mode)
        self.assertTrue(self.app.dirty)
        with patch('poka_yoke_mejorado5.messagebox.askyesnocancel',return_value=False):self.app.toggle_admin()
        self.assertFalse(self.app.admin_mode)
        self.assertEqual(self.app.model['config']['brightness'],0)

    def test_gallery_replacement_is_draft_and_can_restore(self):
        self.admin();m=self.model_in_editor()
        old=m['config']['references'][0]
        self.app.gallery_slot=0
        changed=images()[0].copy();changed[10:15,10:15]=255
        with patch('poka_yoke_mejorado5.messagebox.askyesno',return_value=True):self.app._replace_reference(changed)
        self.assertTrue(self.app.dirty)
        self.assertNotEqual(self.app.draft['references'][0],old)
        self.assertEqual(self.app.store.read(m['id'])['config']['references'][0],old)
        with patch('poka_yoke_mejorado5.messagebox.askyesno',return_value=True):self.app.discard_changes()
        self.assertEqual(self.app.draft['references'][0],old)

    def test_camera_remains_visible_above_all_photo_sections(self):
        self.admin();self.model_in_editor()
        for page in [self.app.reference_page]+[g['page'] for g in self.app.sample_galleries.values()]:
            self.app.editor_tabs.select(page)
            self.pump(.12)
            self.assertTrue(self.app.admin_video.winfo_ismapped())
            self.assertIsNotNone(self.app.admin_video.source)

    def test_reference_capture_button_uses_live_camera_without_dialogs(self):
        self.admin();m=self.model_in_editor()
        c=copy.deepcopy(m['config']);c['references']=[None]*10;c['base_image']=None
        self.app._load_editor(self.app.store.save(m['id'],c))
        self.camera.frame=images()[1]
        with patch('poka_yoke_mejorado5.simpledialog.askstring',side_effect=AssertionError('No debe abrir diálogos')):
            self.app.capture_ok_button.invoke()
            self.app.capture_ok_button.invoke()
        self.assertEqual(sum(e is not None for e in self.app.draft['references']),2)
        self.assertEqual(self.app.gallery_slot,1)
        self.assertEqual(self.app.photo_fields['reference']['variables']['piece_id'].get(),'REF-02')
        self.assertTrue(np.array_equal(self.app.store.image(m['id'],self.app.draft['references'][0]),images()[0]))

    def test_ng_capture_thumbnail_inline_details_and_reopen(self):
        self.admin();m=self.model_in_editor()
        self.camera.frame=images()[2]
        self.app.editor_tabs.select(self.app.sample_galleries['NG']['page'])
        with patch('poka_yoke_mejorado5.simpledialog.askstring',side_effect=AssertionError('Sin preguntas emergentes')):
            self.app.sample_galleries['NG']['capture'].invoke()
        e=self.app.draft['samples'][0]
        self.assertEqual(e['truth'],'NG')
        self.assertFalse(e['identity_confirmed'])
        self.assertEqual(len(self.app.sample_galleries['NG']['grid'].winfo_children()),1)
        self.assertTrue(np.array_equal(self.app.store.image(m['id'],e),self.camera.frame))
        variables=self.app.photo_fields['NG']['variables']
        variables['piece_id'].set('ENSAMBLE-03')
        variables['defect_type'].set('Conector faltante')
        variables['note'].set('Falta el conector de la zona 1')
        self.app.apply_photo_details('NG')
        self.assertTrue(e['identity_confirmed'])
        self.app.save_model()
        saved=self.app.store.read(m['id'])
        self.assertEqual(saved['config']['samples'][0]['defect_type'],'Conector faltante')
        self.assertEqual(saved['config']['samples'][0]['note'],'Falta el conector de la zona 1')
        self.app._load_editor(saved)
        self.assertEqual(self.app.photo_fields['NG']['variables']['piece_id'].get(),'ENSAMBLE-03')

    def test_ok_capture_uses_the_same_ten_pattern_references(self):
        self.admin();m=self.model_in_editor()
        c=copy.deepcopy(m['config']);c['references'][9]=None
        self.app._load_editor(self.app.store.save(m['id'],c))
        frame=images()[1].copy();frame[0,0]=[7,8,9]
        self.camera.frame=frame
        with patch('poka_yoke_mejorado5.simpledialog.askstring',side_effect=AssertionError('Sin diálogo de clase')):
            self.app.capture_ok_button.invoke()
        self.assertEqual(self.app.draft['samples'],[])
        self.assertEqual(sum(e is not None for e in self.app.draft['references']),10)
        self.assertNotIn('OK',self.app.sample_galleries)
        self.assertEqual(len(self.app.sample_galleries['NG']['grid'].winfo_children()),0)
        with self.assertRaisesRegex(ValueError,'10 referencias OK'):
            self.app.capture_sample('OK')

    def test_photo_changes_are_kept_when_capturing_or_switching_thumbnails(self):
        self.admin();self.model_in_editor()
        self.camera.frame=images()[2].copy()
        first=self.app.capture_sample('NG')
        self.app.photo_fields['NG']['variables']['defect_type'].set('Faltante')
        self.app.photo_fields['NG']['variables']['note'].set('Detalle de la primera foto')
        self.camera.frame[0,0]=[12,13,14]
        second=self.app.capture_sample('NG')
        self.assertEqual(self.app.draft['samples'][0]['note'],'Detalle de la primera foto')
        self.assertFalse(self.app.draft['samples'][0]['identity_confirmed'])
        self.app._select_sample(first['id'])
        self.assertEqual(self.app.photo_fields['NG']['variables']['defect_type'].get(),'Faltante')
        self.app._select_sample(second['id'])
        self.assertEqual(self.app.photo_fields['NG']['variables']['note'].get(),'')

    def test_applying_reference_details_does_not_discard_ng_edits(self):
        self.admin();self.model_in_editor()
        self.camera.frame=images()[2]
        self.app.capture_sample('NG')
        self.app.photo_fields['NG']['variables']['note'].set('Nota NG pendiente')
        self.app.photo_fields['reference']['variables']['note'].set('Referencia nominal')
        self.app.apply_photo_details('reference')
        self.assertEqual(self.app.draft['samples'][0]['note'],'Nota NG pendiente')
        self.assertEqual(self.app.draft['references'][0]['note'],'Referencia nominal')
        self.assertEqual(self.app.photo_fields['NG']['variables']['note'].get(),'Nota NG pendiente')

    def test_retake_ng_preserves_identity_and_defect_details(self):
        self.admin();self.model_in_editor()
        self.camera.frame=images()[2].copy()
        self.app.capture_sample('NG')
        self.app.photo_fields['NG']['variables']['defect_type'].set('Mal colocado')
        self.app.apply_photo_details('NG')
        original=copy.deepcopy(self.app.draft['samples'][0])
        self.camera.frame[0,0]=[91,92,93]
        with patch('poka_yoke_mejorado5.messagebox.askyesno',return_value=True):self.app.retake_sample('NG')
        entry=self.app.draft['samples'][0]
        self.assertEqual(entry['id'],original['id'])
        self.assertEqual(entry['piece_id'],original['piece_id'])
        self.assertEqual(entry['defect_type'],'Mal colocado')
        self.assertNotEqual(entry['path'],original['path'])
        self.assertEqual(len(self.app.draft['samples']),1)

    def test_capture_does_not_reuse_stale_frame_after_camera_disconnect(self):
        self.admin();self.model_in_editor()
        self.camera.ready=False
        with self.assertRaisesRegex(ValueError,'imagen reciente'):
            self.app.capture_sample('NG')
        self.assertEqual(self.app.draft['samples'],[])

    def test_sample_gallery_paginates_and_selects_each_photo(self):
        self.admin();m=self.model_in_editor()
        m=add_samples(self.app.store,m)
        self.app._load_editor(m)
        self.assertEqual(len(self.app.sample_galleries['NG']['grid'].winfo_children()),8)
        self.app.sample_page('NG',1)
        self.assertEqual(len(self.app.sample_galleries['NG']['grid'].winfo_children()),2)
        last=[s for s in m['config']['samples'] if s['truth']=='NG'][-1]
        self.app.sample_page('NG',-1)
        self.app._select_sample(last['id'])
        self.assertEqual(self.app.sample_galleries['NG']['offset'],8)
        self.assertEqual(self.app.photo_fields['NG']['variables']['piece_id'].get(),last['piece_id'])

    def test_live_capture_layout_keeps_camera_thumbnails_and_notes_visible(self):
        self.admin();self.model_in_editor()
        self.camera.frame=images()[2]
        self.app.capture_sample('NG')
        self.pump(.2)
        thumbnail=self.app.sample_galleries['NG']['grid'].winfo_children()[0]
        note=self.app.photo_fields['NG']['widgets'][2]
        for widget in (self.app.admin_video,thumbnail,note):
            self.assertTrue(widget.winfo_ismapped())
            x=widget.winfo_rootx()+widget.winfo_width()//2
            y=widget.winfo_rooty()+widget.winfo_height()//2
            self.assertEqual(self.root.winfo_containing(x,y),widget)

    def test_validation_and_enable_from_gui_with_actual_engine(self):
        self.admin();m=self.model_in_editor()
        m=add_samples(self.app.store,m)
        self.app._load_editor(m)
        self.app.run_validation();self.wait_idle()
        s=self.app.store.validation_summary(m)
        self.assertEqual((s['reference_ok'],s['ng_photo_rejected']),(10,10))
        self.assertEqual(s['ok_accepted'],0)
        with patch('poka_yoke_mejorado5.messagebox.askyesno',return_value=True):self.app.approve_model()
        self.wait_idle()
        self.assertIn(m['part'],self.app.op_models)
        self.assertIsNotNone(self.app.model['validation'])

    def test_failed_ng_is_red_and_opens_its_exact_result(self):
        self.admin();m=self.model_in_editor()
        m=add_samples(self.app.store,m,truths=('NG',))
        c=copy.deepcopy(m['config'])
        image=images()[1].copy();image[0,0]=[3,4,5]
        c['samples'][0].update(self.app.store.put_image(m['id'],image))
        m=self.app.store.save(m['id'],c)
        sid=c['samples'][0]['id']
        self.app._load_editor(m)
        self.app.run_validation();self.wait_idle()
        self.assertEqual(self.app.photo_report[sid]['label'],'NG pasa como OK')
        self.assertEqual(self.app.selected_sample_ids['NG'],sid)
        self.assertIn('NG pasa como OK',self.app.photo_fields['NG']['diagnosis'].cget('text'))
        self.assertIn('fail',self.app.samples_table.item(sid,'tags'))
        widget=next(w for w in self.app.sample_galleries['NG']['grid'].winfo_children() if w.photo_sid==sid)
        self.assertEqual(widget.cget('style'),'PhotoFail.TButton')
        self.app.view_photo('NG');self.pump(.1)
        windows=[w for w in self.root.winfo_children() if isinstance(w,tk.Toplevel)]
        self.assertEqual(len(windows),1)
        windows[0].destroy()
        self.app._load_editor(self.app.store.read(m['id']))
        self.assertEqual(self.app.photo_report[sid]['state'],'fail')
        self.app.zone_scales['1'].set(175)
        self.pump(.15)
        widget=next(w for w in self.app.sample_galleries['NG']['grid'].winfo_children() if w.photo_sid==sid)
        self.assertEqual(widget.cget('style'),'PhotoWarning.TButton')
        self.assertIn('anterior',widget.cget('text'))

    def test_unlocatable_ng_is_amber_and_can_be_selected(self):
        self.admin();m=self.model_in_editor()
        m=add_samples(self.app.store,m,truths=('NG',))
        c=copy.deepcopy(m['config'])
        c['samples'][0].update(self.app.store.put_image(m['id'],np.full((200,240,3),27,np.uint8)))
        m=self.app.store.save(m['id'],c)
        sid=c['samples'][0]['id']
        self.app._load_editor(m)
        self.app.run_validation();self.wait_idle()
        self.assertEqual(self.app.photo_report[sid]['label'],'SIN EVALUAR')
        widget=next(w for w in self.app.sample_galleries['NG']['grid'].winfo_children() if w.photo_sid==sid)
        self.assertEqual(widget.cget('style'),'PhotoWarning.TButton')
        self.assertIn('Sin coincidencia',self.app.photo_report[sid]['reason'])

    def test_live_test_works_with_bad_ng_without_approving_production(self):
        self.admin();m=self.model_in_editor()
        m=add_samples(self.app.store,m,truths=('NG',))
        entry=m['config']['samples'][0]
        (self.app.store.model_dir(m['id'])/entry['path']).write_bytes(b'foto danada')
        self.app._load_editor(m)
        with self.assertRaises((ValueError,OSError)):
            self.app._prepare_production_engine(m)
        self.camera.frame=images()[1]
        self.app.toggle_live_test();self.wait_idle()
        self.pump(1.8)
        self.assertTrue(self.app.live_running)
        self.assertIsNotNone(self.app.live_output)
        self.assertTrue(self.app.live_output['result']['ok'])
        self.assertIn('PRUEBA',self.app.live_status.cget('text'))
        self.assertNotIn(m['part'],self.app.op_models)
        self.assertEqual(self.app.store.events(model_id=m['id'],kind='production'),[])
        self.assertEqual(self.app.store.counts(m['id'],''),(0,0))
        self.assertIsNone(self.app.store.read(m['id'])['validation'])
        self.app.zone_scales['1'].set(150)
        self.pump(.15)
        self.assertFalse(self.app.live_running)
        self.assertIn('Ajustes cambiados',self.app.live_status.cget('text'))

    def test_live_test_stops_on_camera_loss_and_cannot_count_production(self):
        self.admin();m=self.model_in_editor()
        self.camera.frame=images()[1]
        self.app.toggle_live_test();self.wait_idle();self.pump(.3)
        self.assertTrue(self.app.live_running)
        self.camera.ready=False;self.pump(.2)
        self.assertFalse(self.app.live_running)
        self.assertIn('sin imagen',self.app.live_status.cget('text'))
        self.assertEqual(self.app.store.events(model_id=m['id']),[])
        self.assertFalse(self.app.running)

    def test_exactly_ten_ok_references_and_ten_ng_enable_inspection(self):
        self.admin();m=self.model_in_editor()
        for i in range(10):
            frame=images()[2].copy();frame[0,0]=[30+i,10,20]
            self.camera.frame=frame
            self.app.capture_sample('NG')
        self.assertEqual(len(self.app.draft['samples']),10)
        self.assertTrue(all(s['truth']=='NG' for s in self.app.draft['samples']))
        self.assertEqual(sum(bool(e) for e in self.app.draft['references']),10)
        with self.assertRaisesRegex(ValueError,'10 fotos NG'):
            self.app.capture_sample('NG')
        self.assertEqual(len(self.app.draft['samples']),10)
        self.app.save_model()
        self.app.run_validation();self.wait_idle()
        s=self.app.store.validation_summary(self.app.model)
        self.assertEqual((s['reference_ok'],s['ng_photo_rejected']),(10,10))
        self.assertEqual(s['ok_pieces'],0)
        rows=self.app.store.events(model_id=m['id'])
        self.assertEqual(len(rows),20)
        self.assertEqual(sum(r['kind']=='reference_check' for r in rows),10)
        self.assertEqual(sum(r['kind']=='validation' for r in rows),10)
        with patch('poka_yoke_mejorado5.messagebox.askyesno',return_value=True):
            self.app.approve_model()
        self.wait_idle()
        self.assertIn(m['part'],self.app.op_models)
        self.assertFalse(self.app.model['validation']['independent_ok_validation'])
        self.assertEqual(self.app.model['validation']['mode'],'reference_and_ng_check')

    def test_production_counts_once_and_disconnect_clears_screen(self):
        m=record_validation(self.app.store,add_samples(self.app.store,make_model(self.app.store)))
        self.app._refresh_catalog()
        self.app.op_part.set(m['part']);self.app._select_operation()
        self.app.operator.set('Prueba GUI');self.app.lot.set('LOTE-1')
        self.app.start_or_pause()
        self.pump(.6)
        self.assertTrue(self.app.running)
        self.camera.frame=images()[1]
        self.pump(2)
        self.assertEqual(self.app.store.counts(m['id'],'LOTE-1'),(1,0))
        self.app.pause();self.wait_idle()
        self.app.start_or_pause();self.pump(1)
        self.assertEqual(self.app.store.counts(m['id'],'LOTE-1'),(1,0))
        self.camera.ready=False
        self.pump(.3)
        self.assertFalse(self.app.running)
        self.assertEqual(self.app.result_label.cget('text'),'SIN EVALUACIÓN')

    def test_wrong_camera_settings_block_production(self):
        m=record_validation(self.app.store,add_samples(self.app.store,make_model(self.app.store)))
        self.app._refresh_catalog();self.app.op_part.set(m['part']);self.app._select_operation()
        self.app.operator.set('Operador');self.app.lot.set('L1')
        self.camera.compatible=False
        self.app.start_or_pause();self.pump(.5)
        self.assertFalse(self.app.running)
        self.assertIn('bloqueado',self.app.message.get())
        self.assertEqual(self.app.store.counts(m['id'],'L1'),(0,0))

    def test_resolution_change_resets_only_draft(self):
        self.admin();m=self.model_in_editor()
        self.app.fields['camera.width'].set('1920')
        self.app.fields['camera.height'].set('1080')
        with patch('poka_yoke_mejorado5.messagebox.askyesno',return_value=True):self.app._sync()
        self.assertEqual(self.app.draft['references'],[None]*10)
        self.assertIsNone(self.app.draft['roi'])
        self.assertIsNotNone(self.app.store.read(m['id'])['config']['roi'])

    def test_small_window_keeps_status_and_controls_visible(self):
        self.root.geometry('1000x680')
        self.pump(.3)
        self.assertTrue(self.app.start_button.winfo_ismapped())
        self.assertLess(self.app.start_button.winfo_rooty()+self.app.start_button.winfo_height(),
                        self.root.winfo_rooty()+self.root.winfo_height())
        footer=self.app.body.master.winfo_children()[-1]
        self.assertLessEqual(footer.winfo_rooty()+footer.winfo_height(),
                             self.root.winfo_rooty()+self.root.winfo_height())

    def test_history_can_open_original_and_export_filtered_records(self):
        m=make_model(self.app.store)
        from tests.helpers import dummy_result
        image=images()[1]
        row=self.app.store.record(m,dummy_result(),operator='Operador',lot='L1',image=image,annotated=image)
        self.app._show_event(row)
        self.app.show_history()
        self.pump(.2)
        windows=[w for w in self.root.winfo_children() if isinstance(w,tk.Toplevel)]
        self.assertEqual(len(windows),2)
        for w in windows:w.destroy()

    def test_operator_search_filters_only_available_models(self):
        m=record_validation(self.app.store,add_samples(self.app.store,make_model(self.app.store)))
        self.app.store.create('MODELO-INCOMPLETO')
        self.app.choose_part()
        dialog=next(w for w in self.root.winfo_children() if isinstance(w,tk.Toplevel))
        def descendants(parent):
            for w in parent.winfo_children():
                yield w
                yield from descendants(w)
        from tkinter import ttk
        widgets=list(descendants(dialog))
        tree=next(w for w in widgets if isinstance(w,ttk.Treeview))
        entry=next(w for w in widgets if isinstance(w,ttk.Entry))
        self.assertEqual(tree.get_children(),(m['id'],))
        entry.insert(0,'no-existe')
        self.assertEqual(tree.get_children(),())
        entry.delete(0,'end');entry.insert(0,'PIEZA')
        tree.selection_set(m['id'])
        select=next(w for w in widgets if isinstance(w,ttk.Button))
        select.invoke()
        self.assertEqual(self.app.active_model['id'],m['id'])
        self.assertFalse(self.app.admin_mode)


if __name__=='__main__':unittest.main()
