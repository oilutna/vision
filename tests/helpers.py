import copy
import uuid
import numpy as np
import cv2
from poka_yoke_mejorado5 import defaults


def images():
    rng = np.random.default_rng(42)
    gray = rng.integers(40,210,(120,120),dtype=np.uint8)
    gray = cv2.GaussianBlur(gray,(5,5),0)
    obj = cv2.cvtColor(gray,cv2.COLOR_GRAY2BGR)
    cv2.rectangle(obj,(12,20),(32,45),(10,30,220),-1)
    cv2.rectangle(obj,(72,70),(92,95),(220,100,20),-1)
    frame = np.full((200,240,3),25,np.uint8)
    frame[40:160,60:180] = obj
    defect = frame.copy()
    defect[60:86,72:93] = 120
    return obj,frame,defect


def make_model(store,part='PIEZA-01'):
    m=store.create(part,'Ensamble de prueba sintética')
    obj,frame,defect=images()
    c=defaults()
    c['camera'].update(width=240,height=200)
    c['roi']=[60,40,180,160]
    c['absence_seconds']=.1
    c['references']=[store.put_image(m['id'],obj) for _ in range(10)]
    c['base_image']=store.put_image(m['id'],frame)
    for i in range(1,4):
        c['zones'][str(i)]['rect']=[(i-1)/3,0,i/3,1]
        c['zones'][str(i)]['name']=f'Componente {i}'
    return store.save(m['id'],c)


def add_samples(store,m,n=10):
    _,frame,defect=images()
    c=copy.deepcopy(m['config'])
    for truth,base in [('OK',frame),('NG',defect)]:
        for i in range(n):
            sample=base.copy()
            sample[0,0]=[50+i,100,10 if truth=='OK' else 220]
            e=store.put_image(m['id'],sample)
            e.update(id=uuid.uuid4().hex,piece_id=f'{truth}-{i+1}',truth=truth,kind='full',note='Sintética de prueba de software')
            c['samples'].append(e)
    return store.save(m['id'],c)


def dummy_result(ok=True):
    return dict(ok=ok,zonas=[dict(idx=1,name='Conector',pix=0 if ok else 100,lim=6,blobs=0 if ok else 1,
                                  ok=ok,rect=[0,0,40,120])],dif_max=0 if ok else 100,match=.99)


def record_validation(store,m):
    for s in m['config']['samples']:
        store.record(m,dummy_result(s['truth']=='OK'),kind='validation',truth=s['truth'],
                     piece_id=s['piece_id'],sample_id=s['id'])
    return store.approve(m['id'],'Prueba',aligned_references=10)
