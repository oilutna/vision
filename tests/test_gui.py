import copy
import os
import tempfile
import time
import tkinter as tk
import customtkinter as ctk
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
        self.root=ctk.CTk()
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
    def wait_idle(self,timeout=10):
        # Incluye lotes de 20 fotos y escritura a disco; no mide la latencia del ciclo.
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
        import customtkinter as ctk
        self.assertIsInstance(self.app.zone_scales['1'],ctk.CTkSlider)
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

    def assert_control_visible(self,widget):
        self.assertTrue(widget.winfo_ismapped(),str(widget))
        x,y=widget.winfo_rootx(),widget.winfo_rooty()
        right,bottom=x+widget.winfo_width(),y+widget.winfo_height()
        parent=widget.master
        while parent is not None:
            self.assertGreaterEqual(x,parent.winfo_rootx()-1,str(widget))
            self.assertGreaterEqual(y,parent.winfo_rooty()-1,str(widget))
            self.assertLessEqual(right,parent.winfo_rootx()+parent.winfo_width()+1,str(widget))
            self.assertLessEqual(bottom,parent.winfo_rooty()+parent.winfo_height()+1,str(widget))
            parent=parent.master
        hit=self.root.winfo_containing(x+widget.winfo_width()//2,y+widget.winfo_height()//2)
        while hit is not None and hit != widget:
            hit=hit.master
        self.assertIs(hit,widget,'Control tapado: '+str(widget))

    def test_admin_buttons_fit_all_photo_sections_at_laptop_sizes(self):
        self.admin();self.model_in_editor()
        self.camera.frame=images()[2]
        self.app.capture_sample('NG')
        for size in ('1000x680','1280x720','1440x900'):
            self.root.geometry(size)
            for page,capture,key in ((self.app.reference_page,self.app.capture_ok_button,'reference'),
                                    (self.app.sample_galleries['NG']['page'],self.app.sample_galleries['NG']['capture'],'NG')):
                self.app.editor_tabs.select(page);self.pump(.12)
                for widget in (self.app.save_button,self.app.camera_button,self.app.access_button,
                               self.app.roi_button,self.app.live_button,capture,*capture.master.actions,
                               *self.app.photo_fields[key]['actions'].actions,
                               self.app.photo_fields[key]['widgets'][2]):
                    with self.subTest(size=size,page=key,widget=str(widget)):
                        self.assert_control_visible(widget)
            self.app.editor_tabs.select('Comprobación');self.pump(.1)
            validation=self.app.editor_tabs.pages[2][0].inner
            from poka_yoke_mejorado5 import ActionBar
            for bar in (w for w in validation.winfo_children() if isinstance(w,ActionBar)):
                for widget in bar.actions:self.assert_control_visible(widget)
            self.app.control_tabs.select('Cámara');self.pump(.1)
            camera=next(w for w,title in self.app.control_tabs.pages if title=='Cámara').inner
            for bar in (w for w in camera.winfo_children() if isinstance(w,ActionBar)):
                for widget in bar.actions:self.assert_control_visible(widget)
            self.app.control_tabs.select('Zonas')

    def test_wrapping_toolbar_keeps_actions_visible_and_clickable(self):
        from poka_yoke_mejorado5 import ActionBar,button
        dialog=ctk.CTkToplevel(self.root);dialog.geometry('310x200')
        bar=ActionBar(dialog);bar.pack(fill='x',padx=5)
        clicked=[]
        for text in ('Guardar configuración','Restaurar guardada','Comprobar ajustes'):
            button(bar,text,lambda value=text:clicked.append(value))
        self.pump(.2)
        self.assertGreater(bar.winfo_height(),34)
        for widget in bar.actions:
            self.assert_control_visible(widget);widget.invoke()
        self.assertEqual(len(clicked),3)
        dialog.destroy()

    def test_background_task_disables_custom_controls_and_restores_zone_states(self):
        self.admin();self.model_in_editor()
        self.app._set_admin_editable(False)
        for widget in (self.app.save_button,self.app.capture_ok_button,self.app.zone_scales['1']):
            self.assertTrue(widget.instate(['disabled']))
        self.app._set_admin_editable(True)
        self.assertFalse(self.app.save_button.instate(['disabled']))
        self.assertFalse(self.app.zone_scales['1'].instate(['disabled']))
        self.assertTrue(self.app.zone_scales['4'].instate(['disabled']))

    def test_catalog_actions_and_scaled_controls_remain_accessible(self):
        from poka_yoke_mejorado5 import ActionBar
        self.admin();self.model_in_editor()
        self.root.geometry('1000x680')
        self.app.admin_tabs.select(0);self.pump(.15)
        catalog=self.app.admin_tabs.pages[0][0]
        for bar in (w for w in catalog.winfo_children() if isinstance(w,ActionBar)):
            for widget in bar.actions:self.assert_control_visible(widget)
        self.app.admin_tabs.select(self.app.editor)
        try:
            ctk.set_widget_scaling(1.25);self.pump(.25)
            for widget in (self.app.save_button,self.app.roi_button,self.app.live_button,self.app.capture_ok_button):
                self.assert_control_visible(widget)
        finally:
            ctk.set_widget_scaling(1.0);self.pump(.1)

    def test_preparation_guide_tracks_photos_validation_and_unsaved_changes(self):
        self.admin();m=self.model_in_editor()
        self.app._update_preparation()
        self.assertTrue(all(done for done,_ in self.app.guide_steps[:4]))
        self.assertFalse(self.app.guide_steps[4][0])
        self.app.guide_to(4)
        self.assertEqual(self.app.editor_tabs.select(),str(self.app.sample_galleries['NG']['page']))
        self.app.guide_to(2)
        self.assertEqual(self.app.control_tabs.select(),str(next(w for w,title in self.app.control_tabs.pages if title=='Zonas')))
        m=add_samples(self.app.store,m,truths=('NG',))
        self.app._load_editor(m)
        self.app.run_validation();self.wait_idle();self.pump(.15)
        self.assertTrue(all(done for done,_ in self.app.guide_steps))
        self.assertIsNone(self.app.store.read(m['id'])['validation'])
        self.app.zone_scales['1'].set(23);self.pump(.1)
        self.assertFalse(self.app.guide_steps[5][0])
        self.assertIn('Guarda',self.app.guide_rows[5][1].cget('text'))

    def test_numeric_zone_controls_sync_step_validate_and_persist(self):
        self.admin();m=self.model_in_editor()
        factor=self.app.zone_fields['1'][1]
        factor.set('1');self.app._step_zone('1',1)
        self.assertEqual(factor.get(),'2')
        self.app._step_zone('1',-1);self.app._step_zone('1',-1)
        self.assertEqual(factor.get(),'1')
        factor.set('301')
        self.assertTrue(self.app.zone_inputs['1'].instate(['invalid']))
        with self.assertRaises(ValueError):self.app._sync()
        self.app.zone_scales['1'].set(17)
        self.assertEqual(factor.get(),'17')
        self.assertFalse(self.app.zone_inputs['1'].instate(['invalid']))
        self.app.save_model()
        self.assertEqual(self.app.store.read(m['id'])['config']['zones']['1']['factor'],17)
        self.assertTrue(self.app.zone_inputs['4'].instate(['disabled']))
        self.assertTrue(self.app.zone_steps['4'][0].instate(['disabled']))

    def test_undo_capture_keeps_subsequent_settings_and_other_photo_notes(self):
        self.admin();m=self.model_in_editor()
        self.camera.frame=images()[2]
        self.app.capture_sample('NG')
        self.app.zone_fields['1'][1].set('17')
        self.app.photo_fields['reference']['variables']['note'].set('Nota posterior de otra foto')
        self.app.undo_photo()
        self.assertEqual(self.app.draft['samples'],[])
        self.assertEqual(self.app.draft['zones']['1']['factor'],17)
        self.assertEqual(self.app.draft['references'][0]['note'],'Nota posterior de otra foto')
        self.assertTrue(self.app.dirty)
        self.assertTrue(self.app.undo_buttons['NG'].instate(['disabled']))
        self.assertEqual(self.app.store.read(m['id'])['config']['samples'],[])

    def test_undo_deleted_ng_and_replaced_ok_then_clear_on_save(self):
        self.admin();m=self.model_in_editor()
        self.camera.frame=images()[2]
        entry=self.app.capture_sample('NG');self.app.save_model()
        self.assertEqual(self.app.photo_undo.actions,[])
        with patch('poka_yoke_mejorado5.messagebox.askyesno',return_value=True):self.app.remove_sample('NG')
        self.assertEqual(self.app.draft['samples'],[])
        self.app.undo_photo()
        self.assertEqual(self.app.draft['samples'][0]['id'],entry['id'])
        previous=copy.deepcopy(self.app.draft['references'][0])
        base=copy.deepcopy(self.app.draft['base_image'])
        self.app.gallery_slot=0
        with patch('poka_yoke_mejorado5.messagebox.askyesno',return_value=True):self.app.retake_reference()
        self.assertNotEqual(self.app.draft['references'][0]['path'],previous['path'])
        self.app.undo_photo()
        self.assertEqual(self.app.draft['references'][0],previous)
        self.assertEqual(self.app.draft['base_image'],base)
        self.app.save_model()
        self.assertEqual(self.app.photo_undo.actions,[])
        self.app._load_editor(self.app.store.create('OTRA-PIEZA'))
        self.assertEqual(self.app.photo_undo.actions,[])

    def test_failed_or_cancelled_photo_actions_do_not_replace_last_undo(self):
        self.admin();self.model_in_editor()
        self.camera.frame=images()[2]
        self.app.capture_sample('NG')
        self.assertEqual(len(self.app.photo_undo.actions),1)
        with self.assertRaises(ValueError):self.app.capture_sample('NG')
        self.camera.frame=images()[2].copy();self.camera.frame[0,0]=[101,102,103]
        with patch('poka_yoke_mejorado5.messagebox.askyesno',return_value=False):
            self.app.retake_sample('NG')
            self.app.remove_sample('NG')
        self.assertEqual(len(self.app.photo_undo.actions),1)
        self.app.undo_photo()
        self.assertEqual(self.app.draft['samples'],[])

    def test_comparison_dialog_shows_real_mismatch_without_approving_or_counting(self):
        self.admin();m=self.model_in_editor()
        m=add_samples(self.app.store,m,truths=('NG',))
        c=copy.deepcopy(m['config'])
        good=images()[1].copy();good[0,0]=[111,112,113]
        c['samples'][0].update(self.app.store.put_image(m['id'],good))
        m=self.app.store.save(m['id'],c)
        self.app._load_editor(m)
        self.app._select_sample(c['samples'][0]['id'])
        self.app.compare_photo('NG');self.wait_idle();self.pump(.15)
        dialog=next(w for w in self.root.winfo_children() if isinstance(w,ctk.CTkToplevel))
        self.assertTrue(dialog.comparison['result']['ok'])
        self.assertEqual(dialog.comparison['entry']['truth'],'NG')
        left,right=dialog.comparison_canvases
        self.assertEqual(left.source.shape,right.source.shape)
        self.assertEqual(self.app.store.events(model_id=m['id']),[])
        self.assertIsNone(self.app.store.read(m['id'])['validation'])
        dialog.destroy()

    def test_guide_buttons_and_precise_zone_controls_fit_small_window(self):
        self.admin();self.model_in_editor()
        self.root.geometry('1000x680')
        self.app.control_tabs.select('Guía');self.pump(.15)
        for _,_,widget in self.app.guide_rows:self.assert_control_visible(widget)
        self.assert_control_visible(self.app.guide_next_button)
        self.app.control_tabs.select('Zonas');self.pump(.15)
        for widget in (self.app.zone_scales['1'],self.app.zone_inputs['1'],*self.app.zone_steps['1']):
            self.assert_control_visible(widget)

    def test_production_exposes_retirement_phase_and_zone_values_without_new_events(self):
        from tests.helpers import dummy_result
        self.app.running=True
        self.app._display_output(dict(annotated=images()[1],state='Resultado emitido · esperando retiro',
                                      result=dummy_result(False),match=.99))
        self.app.running=False
        self.assertEqual(self.app.phase_label.cget('text'),'RETIRE LA PIEZA')
        self.assertEqual(self.app.result_label.cget('text'),'NG')
        self.assertIn('Diferencia 100 / límite 6',self.app.zone_readout.rows[0][2].cget('text'))
        self.assertEqual(self.app.store.events(),[])

    def start_performance_model(self):
        m=record_validation(self.app.store,add_samples(self.app.store,make_model(self.app.store)))
        self.app._refresh_catalog();self.app.op_part.set(m['part']);self.app._select_operation()
        self.app.operator.set('Prueba rendimiento');self.app.lot.set('PERF')
        self.app.start_or_pause();self.pump(.6)
        self.assertTrue(self.app.running)
        return m

    def test_production_frames_do_not_reconfigure_the_admin_panel(self):
        self.start_performance_model()
        self.camera.frame=images()[1]
        with patch.object(self.app,'_set_admin_editable',wraps=self.app._set_admin_editable) as changes:
            self.pump(.8)
            changes.assert_not_called()
        self.app.pause();self.wait_idle()

    def test_camera_and_decision_stay_responsive_while_record_is_saving(self):
        import threading
        m=self.start_performance_model()
        entered,release=threading.Event(),threading.Event()
        original=self.app.store.record
        def slow_record(*args,**kw):
            entered.set();release.wait(3)
            return original(*args,**kw)
        with patch.object(self.app.store,'record',side_effect=slow_record):
            try:
                self.camera.frame=images()[1]
                deadline=time.monotonic()+3
                while not entered.is_set() and time.monotonic()<deadline:self.pump(.02)
                self.assertTrue(entered.is_set())
                self.pump(.06)
                self.assertEqual(self.app.result_label.cget('text'),'OK')
                self.assertEqual(self.app.store.counts(m['id'],'PERF'),(0,0))
                self.assertIn('guardando',self.app.message.get())
                newer=images()[1].copy();newer[0,0]=[77,88,99]
                self.camera.frame=newer;self.pump(.25)
                self.assertTrue(np.array_equal(self.app.video.source[0,0],newer[0,0]))
                with patch('poka_yoke_mejorado5.messagebox.showinfo'):
                    self.app.close()
                self.assertFalse(self.app.closed)
            finally:
                release.set();self.app.pause();self.wait_idle()
        self.assertEqual(self.app.store.counts(m['id'],'PERF'),(1,0))
        self.assertEqual(len(self.app.store.events(model_id=m['id'],kind='production')),1)

    def test_camera_preview_updates_while_the_engine_is_busy(self):
        import threading
        self.start_performance_model()
        entered,release=threading.Event(),threading.Event()
        original=self.app.cycle.process
        def slow_process(*args,**kw):
            entered.set();release.wait(3)
            return original(*args,**kw)
        with patch.object(self.app.cycle,'process',side_effect=slow_process):
            try:
                deadline=time.monotonic()+2
                while not entered.is_set() and time.monotonic()<deadline:self.pump(.02)
                self.assertTrue(entered.is_set())
                self.camera.frame=images()[1]
                self.pump(.25)
                self.assertTrue(np.array_equal(self.app.video.source,self.camera.frame))
                self.assertEqual(self.app.result_label.cget('text'),'SIN EVALUACIÓN')
            finally:
                release.set();self.app.pause();self.wait_idle()

    def test_video_preview_resizes_only_the_display_and_reuses_canvas_item(self):
        import cv2
        from poka_yoke_mejorado5 import ImageCanvas
        canvas=ImageCanvas(self.root,width=320,height=180,video=True)
        canvas.place(x=10,y=100,width=320,height=180);self.pump(.1)
        frame=np.full((1080,1920,3),42,np.uint8)
        with patch('poka_yoke_mejorado5.cv2.cvtColor',wraps=cv2.cvtColor) as conversion:
            canvas.display(frame,copy_image=False)
            shape=conversion.call_args[0][0].shape
        self.assertEqual(shape[:2],(180,320))
        self.assertIs(canvas.source,frame)
        self.assertEqual(canvas.source.shape,(1080,1920,3))
        item=canvas._image_item
        canvas.display(frame,copy_image=False)
        self.assertEqual(canvas._image_item,item)
        self.assertEqual(len(canvas.find_all()),1)
        self.assertEqual(canvas.image_point(160,90),(960,540))
        canvas.destroy()

    def test_video_paint_skips_duplicate_frames_and_caps_redraw_frequency(self):
        frame=images()[1]
        self.app.video_paints.clear()
        with patch('poka_yoke_mejorado5.time.monotonic',return_value=1000.) as clock:
            with patch.object(self.app.video,'display',wraps=self.app.video.display) as display:
                self.app._paint_video(self.app.video,frame,1)
                self.app._paint_video(self.app.video,frame,1)
                self.app._paint_video(self.app.video,frame,2)
                self.assertEqual(display.call_count,1)
                clock.return_value=1000.051
                self.app._paint_video(self.app.video,frame,2)
                self.assertEqual(display.call_count,2)

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
