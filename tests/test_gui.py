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

    def test_validation_and_enable_from_gui_with_actual_engine(self):
        self.admin();m=self.model_in_editor()
        m=add_samples(self.app.store,m)
        self.app._load_editor(m)
        self.app.run_validation();self.wait_idle()
        s=self.app.store.validation_summary(m)
        self.assertEqual((s['ok_accepted'],s['ng_rejected']),(10,10))
        with patch('poka_yoke_mejorado5.messagebox.askyesno',return_value=True):self.app.approve_model()
        self.wait_idle()
        self.assertIn(m['part'],self.app.op_models)
        self.assertIsNotNone(self.app.model['validation'])

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
