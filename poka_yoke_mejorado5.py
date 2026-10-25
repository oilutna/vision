#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Poka-Yoke 5.1.0 — archivo único: Administración e Inspección.

Ejecutar: python poka_yoke_mejorado5.py
Dependencias: python -m pip install numpy opencv-python Pillow
Python recomendado: 3.12 (con Tkinter).
Los modelos y registros se guardan en datos, junto a este archivo.
Opcional: --data-dir "D:/PokaYokeDatos"
No requiere la carpeta pokayoke ni archivos .py adicionales.
"""
from __future__ import annotations

# Mostrar una instrucción útil si faltan las dependencias de escritorio.
try:
    import tkinter
    import cv2
    import numpy
    from PIL import Image, ImageTk
except ImportError as error:
    if __name__ == '__main__':
        import sys
        print(f'Falta una dependencia: {error}\nInstala con: python -m pip install numpy opencv-python Pillow\nPython debe incluir Tkinter.', file=sys.stderr)
        raise SystemExit(1)
    raise


# ========================================================================
# STORAGE
# ========================================================================
"""Modelos versionados, activos inmutables y trazabilidad local. Sin dependencias de GUI."""
import copy
import csv
import hashlib
import hmac
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import sqlite3
import tempfile
from datetime import datetime
import uuid
import zipfile

import cv2
import numpy as np

SCHEMA = 1
PREPARING = 'En preparación'
PENDING = 'Pendiente de validar'
READY = 'Disponible para inspección'


def now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
    fd, tmp = tempfile.mkstemp(prefix='.escribiendo-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def defaults():
    return {
        'camera': {'index': 0, 'width': 1280, 'height': 720,
                   'exposure': None, 'focus': None, 'white_balance': None},
        'brightness': 0, 'contrast': 100, 'tolerance': 5000,
        'zone_count': 3, 'zones': {str(i): {'name': f'Zona {i}', 'rect': None,
                                          'factor': 100} for i in range(1, 6)},
        'roi': None, 'references': [None] * 10, 'base_image': None,
        'samples': [], 'absence_seconds': 0.6, 'normalize_color': True,
    }


def canonical(config):
    # No references to mutable image filenames: image assets are addressed by SHA256.
    return hashlib.sha256(json.dumps(config, sort_keys=True, allow_nan=False,
                                    separators=(',', ':')).encode()).hexdigest()


def number(value, low, high, name, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{name}: introduce un número válido.')
    if integer and int(value) != value:
        raise ValueError(f'{name}: debe ser un entero.')
    if not low <= value <= high:
        raise ValueError(f'{name}: debe estar entre {low} y {high}.')


def asset_name(entry):
    if not isinstance(entry, dict) or not re.fullmatch(r'assets/[0-9a-f]{64}\.png', str(entry.get('path', ''))):
        raise ValueError('Referencia de imagen inválida.')
    if not isinstance(entry.get('processed', False), bool):
        raise ValueError('Estado de procesamiento inválido.')
    for key,limit in (('piece_id',80),('defect_type',120),('note',500)):
        if key in entry and (not isinstance(entry[key],str) or len(entry[key]) > limit):
            raise ValueError(f'Detalle de fotografía inválido: {key} (máximo {limit} caracteres).')
    return entry['path']


def validate_config(c, complete=False):
    if not isinstance(c, dict) or set(defaults()) - set(c):
        raise ValueError('Configuración incompleta o de formato desconocido.')
    cam = c['camera']
    if not isinstance(cam, dict):
        raise ValueError('Configuración de cámara inválida.')
    for k, lo, hi in [('index', 0, 20), ('width', 160, 8192), ('height', 120, 8192)]:
        number(cam.get(k), lo, hi, k, True)
    if cam['width'] * cam['height'] > 33_554_432:
        raise ValueError('La resolución excede el límite de 33 megapíxeles.')
    for key in ('exposure', 'focus', 'white_balance'):
        if cam.get(key) is not None:
            number(cam[key], -10000, 10000, key)
    number(c['brightness'], -100, 100, 'Brillo', True)
    number(c['contrast'], 50, 300, 'Contraste', True)
    number(c['tolerance'], 2000, 15000, 'Tolerancia', True)
    number(c['absence_seconds'], .1, 10, 'Tiempo de retiro')
    if not isinstance(c['normalize_color'], bool) or c['zone_count'] not in (3, 5):
        raise ValueError('Modo de zonas o normalización inválido.')
    roi = c['roi']
    if roi is not None:
        if not isinstance(roi, list) or len(roi) != 4:
            raise ValueError('Región general inválida.')
        for v in roi:
            number(v, 0, 8192, 'Coordenada', True)
        x1, y1, x2, y2 = roi
        if not (0 <= x1 < x2 <= cam['width'] and 0 <= y1 < y2 <= cam['height']
                and x2 - x1 >= 24 and y2 - y1 >= 24):
            raise ValueError('La región debe medir al menos 24 × 24 y estar dentro de la imagen.')
    elif complete:
        raise ValueError('Falta dibujar la región general de la pieza.')
    if not isinstance(c['zones'], dict) or any(str(i) not in c['zones'] for i in range(1,6)):
        raise ValueError('Zonas inválidas.')
    for i in range(1, c['zone_count'] + 1):
        z = c['zones'].get(str(i), {})
        if not isinstance(z.get('name'), str) or not z['name'].strip() or len(z['name']) > 80:
            raise ValueError(f'Falta un nombre válido para la zona {i}.')
        number(z.get('factor'), 50, 300, f'Límite de zona {i}', True)
        rect = z.get('rect')
        if rect is not None:
            if not isinstance(rect, list) or len(rect) != 4:
                raise ValueError(f'Rectángulo inválido en zona {i}.')
            for v in rect:
                number(v, 0, 1, f'Coordenada de zona {i}')
            if not (rect[0] < rect[2] and rect[1] < rect[3]):
                raise ValueError(f'La zona {i} no tiene área.')
        elif complete:
            raise ValueError(f'Falta dibujar la zona {i}.')
    refs = c['references']
    if not isinstance(refs, list) or len(refs) != 10:
        raise ValueError('El modelo debe tener exactamente 10 posiciones de referencia.')
    for ref in refs:
        if ref is not None:
            asset_name(ref)
        elif complete:
            raise ValueError('Se requieren 10 referencias OK válidas.')
    if c['base_image'] is not None:
        asset_name(c['base_image'])
    if not isinstance(c['samples'], list) or len(c['samples']) > 1000:
        raise ValueError('Lista de muestras inválida (máximo 1000).')
    seen_ids = set()
    for sample in c['samples']:
        asset_name(sample)
        if sample.get('truth') not in ('OK', 'NG') or sample.get('kind') not in ('full', 'crop'):
            raise ValueError('La muestra debe indicar OK/NG y su tipo de imagen.')
        if not isinstance(sample.get('piece_id'), str) or not sample['piece_id'].strip():
            raise ValueError('Cada muestra necesita un identificador de pieza.')
        if (len(sample['piece_id']) > 80 or not isinstance(sample.get('note',''),str)
                or len(sample.get('note','')) > 500):
            raise ValueError('Identificador o nota demasiado largos.')
        sid = sample.get('id')
        if not isinstance(sid, str) or not re.fullmatch('[0-9a-f]{32}',sid) or sid in seen_ids:
            raise ValueError('Identificador de muestra inválido o repetido.')
        if not isinstance(sample.get('identity_confirmed',True),bool):
            raise ValueError('Confirmación de identidad inválida.')
        seen_ids.add(sid)
    return c


def image_bytes(image):
    if image is None or image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
        raise ValueError('Se necesita una imagen BGR válida de 8 bits.')
    h, w = image.shape[:2]
    if min(w, h) < 2 or w * h > 33_554_432:
        raise ValueError('Dimensiones de imagen no compatibles.')
    ok, encoded = cv2.imencode('.png', image)
    if not ok:
        raise OSError('No se pudo codificar la imagen.')
    return encoded.tobytes()


def load_image(path):
    # imdecode supports accented/Unicode paths on Windows.
    path = Path(path)
    if path.stat().st_size > 100 * 1024 * 1024:
        raise ValueError('La imagen excede 100 MB.')
    image = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.shape[0] * image.shape[1] > 33_554_432:
        raise ValueError(f'No se puede leer la imagen: {path.name}')
    return image


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.models = self.root / 'modelos'
        self.models.mkdir(parents=True, exist_ok=True)
        (self.root / 'registros').mkdir(exist_ok=True)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, model_id TEXT NOT NULL,
                part TEXT NOT NULL, version INTEGER NOT NULL, fingerprint TEXT NOT NULL,
                kind TEXT NOT NULL, operator TEXT, lot TEXT, work_order TEXT,
                outcome TEXT NOT NULL, truth TEXT, piece_id TEXT, sample_id TEXT,
                image TEXT, annotated TEXT, result TEXT NOT NULL, note TEXT)''')
            db.execute('CREATE INDEX IF NOT EXISTS event_model ON events(model_id, version, kind)')
        self.catalog_errors = []

    def connect(self):
        db = sqlite3.connect(self.root / 'historial.sqlite3', timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def model_dir(self, mid):
        if not re.fullmatch(r'[0-9a-f]{32}', str(mid)):
            raise ValueError('Identificador de modelo inválido.')
        path = self.models / mid
        if not path.resolve().is_relative_to(self.models.resolve()):
            raise ValueError('El directorio del modelo apunta fuera de la ubicación de datos.')
        return path

    def read(self, mid):
        m = read_json(self.model_dir(mid) / 'modelo.json')
        if not isinstance(m,dict) or m.get('schema') != SCHEMA or m.get('id') != mid:
            raise ValueError('Modelo de formato incompatible.')
        if (not isinstance(m.get('part'),str) or not m['part'].strip() or len(m['part']) > 80
                or not isinstance(m.get('description'),str) or not isinstance(m.get('history'),list)
                or not isinstance(m.get('archived'),bool) or not isinstance(m.get('saved_at'),str)):
            raise ValueError('Metadatos de modelo inválidos.')
        number(m.get('version'),1,1000000,'Versión',True)
        if m.get('validation') is not None and not isinstance(m['validation'],dict):
            raise ValueError('Registro de validación inválido.')
        validate_config(m['config'])
        return m

    def list(self):
        rows, self.catalog_errors = [], []
        for folder in sorted(self.models.iterdir()):
            if folder.name.startswith('.'):
                continue
            try:
                rows.append(self.read(folder.name))
            except (ValueError, OSError, KeyError, TypeError) as e:
                self.catalog_errors.append(f'{folder.name}: {e}')
        return sorted(rows, key=lambda m: m['part'].casefold())

    def check_name(self, part, exclude=None):
        part = part.strip()
        if not part or len(part) > 80 or any(ord(c) < 32 for c in part):
            raise ValueError('Introduce un número de parte de 1 a 80 caracteres.')
        if any(m['id'] != exclude and m['part'].casefold() == part.casefold() for m in self.list()):
            raise ValueError('Ya existe un modelo con ese número de parte, incluso entre los archivados.')
        return part

    def create(self, part, description=''):
        part = self.check_name(part)
        mid = uuid.uuid4().hex
        m = dict(schema=SCHEMA, id=mid, part=part, description=description[:500],
                 archived=False, version=1, saved_at=now(), config=defaults(),
                 validation=None, history=[])
        atomic_json(self.model_dir(mid) / 'modelo.json', m)
        return m

    def save(self, mid, config):
        validate_config(config)
        m = self.read(mid)
        if canonical(config) == canonical(m['config']):
            return m
        old = {k: copy.deepcopy(v) for k, v in m.items() if k != 'history'}
        m['history'].append(old)
        m.update(config=copy.deepcopy(config), version=m['version'] + 1,
                 saved_at=now(), validation=None)
        atomic_json(self.model_dir(mid) / 'modelo.json', m)
        return m

    def restore_previous(self, mid):
        m = self.read(mid)
        if not m['history']:
            raise ValueError('No hay una versión anterior para recuperar.')
        return self.save(mid, m['history'][-1]['config'])

    def metadata(self, mid, *, part=None, description=None, archived=None):
        m = self.read(mid)
        if part is not None:
            m['part'] = self.check_name(part, mid)
        if description is not None:
            m['description'] = description[:500]
        if archived is not None:
            m['archived'] = bool(archived)
        atomic_json(self.model_dir(mid) / 'modelo.json', m)
        return m

    def put_image(self, mid, image, processed=False):
        data = image_bytes(image)
        digest = hashlib.sha256(data).hexdigest()
        rel = f'assets/{digest}.png'
        path = self.model_dir(mid) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            fd, tmp = tempfile.mkstemp(prefix='.imagen-', dir=path.parent)
            try:
                with os.fdopen(fd, 'wb') as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        return {'path': rel, 'processed': bool(processed)}

    def image(self, mid, entry):
        rel = asset_name(entry)
        path = self.model_dir(mid) / rel
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != Path(rel).stem:
            raise ValueError('La imagen fue modificada fuera del programa; vuelve a importarla.')
        return load_image(path)

    @staticmethod
    def entries(config):
        return [e for e in config['references'] + [config['base_image']] + config['samples'] if e]

    def integrity(self, m, complete=False):
        c = validate_config(m['config'], complete)
        if c['roi']:
            x1, y1, x2, y2 = c['roi']
            expected = (y2-y1, x2-x1)
            for i, ref in enumerate(c['references'], 1):
                if ref and self.image(m['id'], ref).shape[:2] != expected:
                    raise ValueError(f'Referencia OK {i}: dimensiones incompatibles con la región.')
        for e in self.entries(c):
            image = self.image(m['id'], e)
            if e in c['samples']:
                expected = (c['camera']['height'],c['camera']['width']) if e['kind'] == 'full' else (
                    (c['roi'][3]-c['roi'][1],c['roi'][2]-c['roi'][0]) if c['roi'] else None)
                if expected is None or image.shape[:2] != expected:
                    raise ValueError(f"Muestra {e['piece_id']}: dimensiones incompatibles.")
        return True

    def status(self, m):
        if m['archived']:
            return 'Archivado'
        try:
            validate_config(m['config'], True)
        except (ValueError, TypeError, KeyError):
            return PREPARING
        v = m.get('validation')
        if v and v.get('version') == m['version'] and v.get('fingerprint') == canonical(m['config']):
            return READY
        return PENDING

    def duplicate(self, mid, part):
        m = self.read(mid)
        self.integrity(m)
        n = self.create(part, m['description'])
        try:
            for e in self.entries(m['config']):
                target = self.model_dir(n['id']) / e['path']
                target.parent.mkdir(exist_ok=True)
                shutil.copy2(self.model_dir(mid) / e['path'], target)
            n['config'] = copy.deepcopy(m['config'])
            n['validation'] = None
            atomic_json(self.model_dir(n['id']) / 'modelo.json', n)
            return n
        except Exception:
            shutil.rmtree(self.model_dir(n['id']))
            raise

    def export_model(self, mid, destination):
        m = self.read(mid)
        self.integrity(m)
        # Export current recipe, not passwords, database or administrative settings.
        payload = copy.deepcopy(m)
        payload['history'] = []
        with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('modelo.json', json.dumps(payload, ensure_ascii=False))
            for name in sorted({e['path'] for e in self.entries(m['config'])}):
                z.write(self.model_dir(mid) / name, name)

    def import_model(self, source, new_part=None):
        with zipfile.ZipFile(source) as z:
            infos = z.infolist()
            names = [i.filename for i in infos]
            if len(names) != len(set(names)) or len(names) > 1100 or 'modelo.json' not in names:
                raise ValueError('Paquete inválido: archivos repetidos o manifiesto ausente.')
            if sum(i.file_size for i in infos) > 250 * 1024 * 1024:
                raise ValueError('El paquete descomprimido supera 250 MB.')
            for i in infos:
                if i.is_dir() or ((i.external_attr >> 16) & 0o170000) == 0o120000:
                    raise ValueError('El paquete contiene enlaces o directorios no admitidos.')
                if i.filename != 'modelo.json' and not re.fullmatch(r'assets/[0-9a-f]{64}\.png', i.filename):
                    raise ValueError('El paquete contiene rutas o archivos no permitidos.')
            if z.getinfo('modelo.json').file_size > 10 * 1024 * 1024:
                raise ValueError('Manifiesto demasiado grande.')
            raw = json.loads(z.read('modelo.json'))
            if raw.get('schema') != SCHEMA:
                raise ValueError('Versión de paquete no compatible.')
            config = validate_config(raw['config'])
            required = {e['path'] for e in self.entries(config)}
            if set(names) != required | {'modelo.json'}:
                raise ValueError('Las imágenes del paquete no corresponden con el manifiesto.')
            part = self.check_name(new_part or raw['part'])
            mid = uuid.uuid4().hex
            stage = self.models / ('.importando-' + mid)
            stage.mkdir()
            try:
                for name in required:
                    data = z.read(name)
                    if hashlib.sha256(data).hexdigest() != PurePosixPath(name).stem:
                        raise ValueError('Una imagen del paquete no coincide con su huella.')
                    target = stage / name
                    target.parent.mkdir(exist_ok=True)
                    target.write_bytes(data)
                    load_image(target)
                m = dict(schema=SCHEMA, id=mid, part=part,
                         description=str(raw.get('description', ''))[:500], archived=False,
                         version=1, saved_at=now(), config=copy.deepcopy(config),
                         validation=None, history=[])
                atomic_json(stage / 'modelo.json', m)
                os.replace(stage, self.model_dir(mid))
                try:
                    self.integrity(m)
                except Exception:
                    shutil.rmtree(self.model_dir(mid))
                    raise
                return m
            finally:
                if stage.exists():
                    shutil.rmtree(stage)

    def import_legacy(self, directory, name, new_part=None, resolution=None):
        folder = Path(directory).resolve()
        # Names come from discovered basenames; reject separators before joining paths.
        if Path(name).name != name or '/' in name or '\\' in name:
            raise ValueError('Nombre de modelo anterior inválido.')
        cfg_path = folder / f'{name}_config.json'
        old = read_json(cfg_path) if cfg_path.exists() else {}
        c = defaults()
        roi = np.load(folder / f'{name}_roi.npy', allow_pickle=False)
        if roi.shape != (4,) or not np.issubdtype(roi.dtype, np.number):
            raise ValueError('Región anterior inválida.')
        c['roi'] = [int(v) for v in roi]
        fullpath = folder / f'{name}_full.png'
        full = load_image(fullpath) if fullpath.exists() else None
        if full is not None:
            h, w = full.shape[:2]
        elif resolution:
            w, h = resolution
        else:
            raise ValueError('No se puede inferir la resolución anterior; selecciónala antes de importar.')
        c['camera'].update(width=w, height=h)
        c['brightness'] = int(old.get('brillo', 0))
        c['contrast'] = int(old.get('contraste', 100))
        c['tolerance'] = int(old.get('tolerancia', 5000))
        c['zone_count'] = int(old.get('modo_zonas', 5))
        for i in range(1, 6):
            key = str(i)
            c['zones'][key]['rect'] = old.get('zonas_relativas', {}).get(key)
            c['zones'][key]['factor'] = int(old.get('factores_zona', {}).get(key, 100))
        validate_config(c)
        m = self.create(new_part or name, 'Importado de versión 3/4; revisar y validar antes de producción.')
        try:
            if full is not None:
                c['base_image'] = self.put_image(m['id'], full, True)
            x1, y1, x2, y2 = c['roi']
            for i in range(10):
                path = folder / f'{name}_ref_{i}.png'
                if path.exists():
                    img = load_image(path)
                    if img.shape[:2] == (h, w):
                        img = img[y1:y2, x1:x2].copy()
                    if img.shape[:2] != (y2-y1, x2-x1):
                        raise ValueError(f'Referencia anterior {i+1}: tamaño incompatible.')
                    c['references'][i] = self.put_image(m['id'], img, True)
                ng = folder / f'{name}_ng_{i}.png'
                if ng.exists():
                    e = self.put_image(m['id'], load_image(ng), True)
                    e.update(id=uuid.uuid4().hex, piece_id=f'NG-anterior-{i+1}', truth='NG',
                             kind='full', identity_confirmed=False,
                             note='Importada; editar y confirmar identidad de pieza antes de validar.')
                    c['samples'].append(e)
            m = self.save(m['id'], c)
            self.integrity(m)
            return m
        except Exception:
            shutil.rmtree(self.model_dir(m['id']))
            raise

    def record(self, m, result, *, kind='production', operator='', lot='', work_order='',
               truth=None, piece_id='', sample_id='', image=None, annotated=None, note=''):
        if kind not in ('production', 'validation', 'test', 'manual'):
            raise ValueError('Tipo de evaluación inválido.')
        if truth not in (None, 'OK', 'NG'):
            raise ValueError('Etiqueta real inválida.')
        outcome = 'SIN_EVALUACION' if result is None else ('OK' if result['ok'] else 'NG')
        if kind == 'production' and result is None:
            raise ValueError('No se contabiliza producción sin una evaluación válida.')
        eid = uuid.uuid4().hex
        written = []
        try:
            files = []
            for suffix, img in [('original', image), ('anotada', annotated)]:
                if img is None:
                    files.append(None)
                    continue
                rel = f'registros/{eid}_{suffix}.png'
                dest = self.root / rel
                dest.write_bytes(image_bytes(img))
                written.append(dest)
                files.append(rel)
            row = dict(id=eid, timestamp=now(), model_id=m['id'], part=m['part'], version=m['version'],
                       fingerprint=canonical(m['config']), kind=kind, operator=operator, lot=lot,
                       work_order=work_order, outcome=outcome, truth=truth, piece_id=piece_id,
                       sample_id=sample_id, image=files[0], annotated=files[1],
                       result=json.dumps(result, ensure_ascii=False, allow_nan=False), note=note)
            with self.connect() as db:
                columns = ','.join(row)
                db.execute(f'INSERT INTO events ({columns}) VALUES ({",".join("?" for _ in row)})', list(row.values()))
            return row
        except Exception:
            for p in written:
                p.unlink(missing_ok=True)
            raise

    def events(self, *, model_id=None, lot=None, start='', end='', outcome='', kind='', limit=5000):
        clauses, args = [], []
        for key, value in [('model_id', model_id), ('lot', lot), ('outcome', outcome), ('kind', kind)]:
            if value is not None and value != '':
                clauses.append(f'{key}=?')
                args.append(value)
        if start:
            datetime.strptime(start, '%Y-%m-%d')
            clauses.append('substr(timestamp,1,10)>=?')
            args.append(start)
        if end:
            datetime.strptime(end, '%Y-%m-%d')
            clauses.append('substr(timestamp,1,10)<=?')
            args.append(end)
        sql = 'SELECT * FROM events' + (' WHERE ' + ' AND '.join(clauses) if clauses else '')
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql + ' ORDER BY timestamp DESC, rowid DESC LIMIT ?', args + [limit])]

    def counts(self, mid, lot):
        with self.connect() as db:
            rows = db.execute("SELECT outcome,COUNT(*) n FROM events WHERE model_id=? AND lot=? AND kind='production' GROUP BY outcome", (mid, lot))
            counts = {r['outcome']: r['n'] for r in rows}
        ok, ng = counts.get('OK', 0), counts.get('NG', 0)
        return ok, ng

    def validation_summary(self, m):
        rows = self.events(model_id=m['id'], kind='validation', limit=100000)
        latest = {}
        current_samples = {s['id']: s for s in m['config']['samples']}
        refs = {e['path'] for e in m['config']['references'] + [m['config']['base_image']] if e}
        used_images = set()
        for r in rows:
            sample = current_samples.get(r['sample_id'])
            if (r['version'] != m['version'] or r['fingerprint'] != canonical(m['config'])
                or not sample or not sample.get('identity_confirmed', True)
                or sample['truth'] != r['truth'] or sample['piece_id'] != r['piece_id']):
                continue
            key = (r['truth'], r['piece_id'].strip().casefold())
            if key in latest or sample['path'] in refs or sample['path'] in used_images:
                continue
            latest[key] = r
            used_images.add(sample['path'])
        summary = dict(ok_accepted=0, ok_rejected=0, ng_rejected=0, ng_accepted=0,
                       unevaluated=0, ok_pieces=0, ng_pieces=0)
        for (truth, _), r in latest.items():
            summary['ok_pieces' if truth == 'OK' else 'ng_pieces'] += 1
            if r['outcome'] == 'SIN_EVALUACION':
                summary['unevaluated'] += 1
            else:
                key = ('ok_accepted' if r['outcome'] == 'OK' else 'ok_rejected') if truth == 'OK' else (
                    'ng_rejected' if r['outcome'] == 'NG' else 'ng_accepted')
                summary[key] += 1
        return summary

    def approve(self, mid, operator, *, aligned_references):
        m = self.read(mid)
        self.integrity(m, True)
        if aligned_references != 10:
            raise ValueError('Faltan 10 referencias OK alineables.')
        s = self.validation_summary(m)
        if s['ok_pieces'] < 10 or s['ng_pieces'] < 10:
            raise ValueError('Se requieren pruebas de al menos 10 piezas OK y 10 NG distintas, fuera de las referencias.')
        if s['unevaluated'] or s['ok_rejected'] or s['ng_accepted']:
            raise ValueError('Hay pruebas incorrectas o sin evaluación; corrige y vuelve a probar esta versión.')
        m['validation'] = dict(version=m['version'], fingerprint=canonical(m['config']),
                               at=now(), administrator=operator, summary=s)
        atomic_json(self.model_dir(mid) / 'modelo.json', m)
        return m

    def export_csv(self, rows, destination):
        columns = ['timestamp', 'part', 'version', 'kind', 'operator', 'lot', 'work_order',
                   'outcome', 'truth', 'piece_id', 'sample_id', 'note', 'result', 'image', 'annotated']
        with open(destination, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            writer.writerow(columns)
            for row in rows:
                values = []
                for key in columns:
                    val = str(row.get(key) or '')
                    # Prevent spreadsheet formula execution when opening operator-provided fields.
                    values.append("'" + val if val.startswith(('=', '+', '-', '@')) else val)
                writer.writerow(values)


class Auth:
    def __init__(self, root):
        self.path = Path(root) / 'administracion.json'

    @property
    def configured(self):
        return self.path.exists()

    def set_password(self, password):
        if len(password) < 8:
            raise ValueError('Usa al menos 8 caracteres para la contraseña.')
        salt = secrets.token_bytes(16)
        iterations = 600_000
        digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, iterations).hex()
        atomic_json(self.path, dict(scheme='pbkdf2-sha256', salt=salt.hex(), iterations=iterations, digest=digest))
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def verify(self, password):
        d = read_json(self.path)
        if d.get('scheme') != 'pbkdf2-sha256' or not 100000 <= d.get('iterations', 0) <= 2_000_000:
            raise ValueError('Archivo de acceso administrativo inválido.')
        digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(d['salt']), d['iterations']).hex()
        return hmac.compare_digest(digest, d['digest'])


# ========================================================================
# ENGINE
# ========================================================================
"""Motor LAB/ECC conservado de v4; independiente de Tkinter."""
import copy
import time
import cv2
import numpy as np
VIDEO_W, VIDEO_H = 740, 360
MATCH_PRESENCIA = 0.45
MATCH_OK = 0.60
FRAMES_ESTABLES = 6
FRAMES_PROMEDIO = 8
ECC_ESCALA = 0.5
ECC_ROT_MAX = 6.0
ECC_DESP_MAX = 12.0
TOL_REF = 5000
CAL_MARGEN = 1.5
CAL_MIN = 12
BLOB_MIN_AREA = 6
LIM_MIN_BLOB = 6
CAL_MIN_BLOB = 4
TOL_POS = 3
TOL_REEVALUAR = 20
FRAMES_AUSENCIA = 8
UMBRAL_L_MIN, UMBRAL_L_MAX = 30, 80
UMBRAL_AB_MIN, UMBRAL_AB_MAX = 14, 45
K_STD = 3.0
MARGEN_ALIN = 4
LIM_MIN_PIX = 20
FACTOR_TOLERANCIA = 100000.0

class OriginalVision:
    def _umbral_pixel(self, std):
        """Umbral por píxel: tolera más donde las referencias varían (bordes, reflejos)."""
        u = np.empty(std.shape, np.float32)
        u[..., 0] = np.clip(K_STD * std[..., 0], UMBRAL_L_MIN, UMBRAL_L_MAX)
        u[..., 1:] = np.clip(K_STD * std[..., 1:], UMBRAL_AB_MIN, UMBRAL_AB_MAX)
        return u

    def _rect_zona(self, rel, ancho, alto):
        rx_1, ry_1, rx_2, ry_2 = rel
        zx1 = max(0, min(int(rx_1 * ancho), ancho - 2))
        zy1 = max(0, min(int(ry_1 * alto), alto - 2))
        zx2 = max(zx1 + 2, min(int(rx_2 * ancho), ancho))
        zy2 = max(zy1 + 2, min(int(ry_2 * alto), alto))
        return zx1, zy1, zx2, zy2

    def _warp_ecc(self, tpl, img):
        """Alineación fina (traslación + rotación pequeña) con ECC a media resolución.
        Devuelve la matriz que lleva 'img' sobre 'tpl', o None si no converge o es irreal."""
        s = ECC_ESCALA
        try:
            t_s = cv2.resize(tpl.astype(np.float32), None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
            i_s = cv2.resize(img.astype(np.float32), None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
            W = np.eye(2, 3, dtype=np.float32)
            crit = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 80, 1e-5)
            cc, W = cv2.findTransformECC(t_s, i_s, W, cv2.MOTION_EUCLIDEAN, crit, None, 5)
        except cv2.error:
            return None
        if not np.isfinite(cc) or cc < 0.5:
            return None
        ang = np.degrees(np.arctan2(W[1, 0], W[0, 0]))
        tx, ty = W[0, 2] / s, W[1, 2] / s          # la traslación vuelve a resolución completa
        if abs(ang) > ECC_ROT_MAX or abs(tx) > ECC_DESP_MAX or abs(ty) > ECC_DESP_MAX:
            return None
        W_full = np.float32([[W[0, 0], W[0, 1], tx], [W[1, 0], W[1, 1], ty]])
        # ECC devuelve la transformación inversa; aquí se invierte para aplicarla hacia adelante
        return cv2.invertAffineTransform(W_full).astype(np.float32)

    def _alinear_a(self, tpl, img):
        """Matriz (hacia adelante) que alinea 'img' sobre 'tpl'.
        Primero ECC; si no converge, cae a traslación por template matching."""
        M = self._warp_ecc(tpl, img)
        if M is not None:
            return M
        h, w = tpl.shape[:2]
        m = max(8, int(min(h, w) * 0.15))
        nucleo = np.ascontiguousarray(tpl[m:h - m, m:w - m])
        res = cv2.matchTemplate(np.ascontiguousarray(img), nucleo, cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(res)
        if not np.isfinite(score) or score < 0.5:
            return None
        return np.float32([[1, 0, m - loc[0]], [0, 1, m - loc[1]]])

    def _blobs(self, mask):
        """Devuelve (área del defecto mayor, cantidad de defectos) de una máscara binaria."""
        n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        if n <= 1:
            return 0, 0
        areas = stats[1:, cv2.CC_STAT_AREA]
        areas = areas[areas >= BLOB_MIN_AREA]
        if areas.size == 0:
            return 0, 0
        return int(areas.max()), int(areas.size)

    def _calibrar_limites(self, stack, cache, total_zonas):
        """Calibración automática por zona: cada referencia se compara contra el modelo
        formado por las demás (leave-one-out) y se mide su defecto mayor. El límite es ese
        peor caso con holgura."""
        n = stack.shape[0]
        if n < 4:
            return {}
        ancho, alto = cache["ancho"], cache["alto"]
        peores = {}
        for i in range(n):
            otros = np.delete(stack, i, axis=0)
            media_i = otros.mean(axis=0)
            umbral_i = self._umbral_pixel(otros.std(axis=0))
            for idx in range(1, total_zonas + 1):
                rel = self.zonas_relativas.get(str(idx))
                if not rel:
                    continue
                zx1, zy1, zx2, zy2 = self._rect_zona(rel, ancho, alto)
                dif = np.abs(stack[i, zy1:zy2, zx1:zx2] - media_i[zy1:zy2, zx1:zx2])
                mask = np.any(dif > umbral_i[zy1:zy2, zx1:zx2], axis=2).astype(np.uint8) * 255
                mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self._kernel_morf)
                area, _ = self._blobs(mask)
                peores[idx] = max(peores.get(idx, 0), area)
        return {str(idx): int(p * CAL_MARGEN) + CAL_MIN_BLOB for idx, p in peores.items()}

    def _roi_alineada(self, frame, tx, ty, cache):
        """Recorta la pieza en la frame actual, la alinea al modelo (ECC) y normaliza su luz."""
        aw, ah = cache["ancho"], cache["alto"]
        roi = frame[ty:ty + ah, tx:tx + aw]
        if roi.shape[0] != ah or roi.shape[1] != aw:
            return None
        lab = cv2.GaussianBlur(cv2.cvtColor(roi, cv2.COLOR_BGR2LAB), (5, 5), 0)
        M = self._alinear_a(cache["media_L"], lab[:, :, 0])
        if M is not None:
            lab = cv2.warpAffine(lab, M, (aw, ah), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
            self._ultimo_ajuste = (float(np.degrees(np.arctan2(M[1, 0], M[0, 0]))),
                                   float(M[0, 2]), float(M[1, 2]))
        else:
            self._ultimo_ajuste = None
        return self._normalizar_iluminacion(lab, cache)

    def _localizar(self, frame_gray, cache, h, w):
        """Busca la pieza en el área central: grueso a media resolución y refinado a resolución completa."""
        tpl = cache["tpl_gray"]
        tpl_s = cache["tpl_gray_s"]
        th, tw = tpl.shape[:2]

        x0, x1 = int(w * 0.15), int(w * 0.85)
        y0, y1 = int(h * 0.15), int(h * 0.85)
        area = frame_gray[y0:y1, x0:x1]
        if area.shape[0] <= th or area.shape[1] <= tw:
            return 0.0, 0, 0

        area_s = cv2.resize(area, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
        if area_s.shape[0] < tpl_s.shape[0] or area_s.shape[1] < tpl_s.shape[1]:
            return 0.0, 0, 0
        res = cv2.matchTemplate(area_s, tpl_s, cv2.TM_CCOEFF_NORMED)
        res = np.nan_to_num(res, nan=0.0, posinf=0.0, neginf=0.0)
        _, _, _, loc = cv2.minMaxLoc(res)
        cx = loc[0] * 2 + x0
        cy = loc[1] * 2 + y0

        R = 6
        rx0, ry0 = max(0, cx - R), max(0, cy - R)
        rx1, ry1 = min(w, cx + tw + R), min(h, cy + th + R)
        ventana = frame_gray[ry0:ry1, rx0:rx1]
        if ventana.shape[0] < th or ventana.shape[1] < tw:
            return 0.0, 0, 0
        res2 = cv2.matchTemplate(ventana, tpl, cv2.TM_CCOEFF_NORMED)
        res2 = np.nan_to_num(res2, nan=0.0, posinf=0.0, neginf=0.0)
        _, mv, _, loc2 = cv2.minMaxLoc(res2)
        return float(mv), rx0 + loc2[0], ry0 + loc2[1]

    def _normalizar_iluminacion(self, roi_lab, cache):
        """Ajusta la luminosidad (ganancia/offset global) y el balance de color de la
        pieza actual a la referencia, para que cambios de luz no generen NG falsos."""
        L = roi_lab[:, :, 0].astype(np.float32)
        mu, sd = float(L.mean()), float(L.std())
        gan = 1.0
        if sd > 1e-3:
            gan = float(np.clip(cache["L_std"] / sd, 0.75, 1.33))
        out = np.empty(roi_lab.shape, np.float32)
        out[:, :, 0] = (L - mu) * gan + cache["L_mean"]
        for c, clave, clave_std in ((1, "a_med", "a_std"), (2, "b_med", "b_std")):
            canal = roi_lab[:, :, c].astype(np.float32)
            med = float(np.median(canal[::4, ::4]))
            sd_c = float(canal.std())
            gan_c = 1.0
            if sd_c > 1e-3 and cache[clave_std] > 1e-3:
                # la luz también cambia la saturación: ajustar escala y balance de color
                gan_c = float(np.clip(cache[clave_std] / sd_c, 0.75, 1.33))
            out[:, :, c] = (canal - med) * gan_c + cache[clave]
        np.clip(out, 0, 255, out=out)
        return out

    def _evaluar_zonas(self, roi_f, cache, total_zonas, tolerancia, match):
        """Compara la ROI ya alineada y normalizada contra el modelo, zona por zona.
        El criterio es el área del defecto mayor (blob), no la suma de píxeles."""
        ancho_obj, alto_obj = cache["ancho"], cache["alto"]
        roi_u8 = np.clip(roi_f, 0, 255).astype(np.uint8)
        media_f, media_u8, umbral = cache["media_f"], cache["media_u8"], cache["umbral"]
        lim_cal = cache.get("lim_zona", {})
        frac = tolerancia / FACTOR_TOLERANCIA

        zonas_res = []
        todo_ok = True
        dif_max = 0
        for idx in range(1, total_zonas + 1):
            rel = self.zonas_relativas.get(str(idx))
            if not rel:
                return None
            zx1, zy1, zx2, zy2 = self._rect_zona(rel, ancho_obj, alto_obj)
            zw, zh = zx2 - zx1, zy2 - zy1

            # Realineación local de la zona (+/- MARGEN_ALIN px)
            ex1, ey1 = max(0, zx1 - MARGEN_ALIN), max(0, zy1 - MARGEN_ALIN)
            ex2, ey2 = min(ancho_obj, zx2 + MARGEN_ALIN), min(alto_obj, zy2 + MARGEN_ALIN)
            ventana = np.ascontiguousarray(roi_u8[ey1:ey2, ex1:ex2])
            zona_ref_u8 = np.ascontiguousarray(media_u8[zy1:zy2, zx1:zx2])
            res = cv2.matchTemplate(ventana, zona_ref_u8, cv2.TM_SQDIFF)
            _, _, loc_min, _ = cv2.minMaxLoc(res)
            ox, oy = loc_min
            sub = roi_f[ey1 + oy:ey1 + oy + zh, ex1 + ox:ex1 + ox + zw]

            dif = np.abs(sub - media_f[zy1:zy2, zx1:zx2])
            mascara = np.any(dif > umbral[zy1:zy2, zx1:zx2], axis=2).astype(np.uint8) * 255
            mascara = cv2.morphologyEx(mascara, cv2.MORPH_OPEN, self._kernel_morf)
            area, n_blobs = self._blobs(mascara)

            fz = self.factores_zona.get(str(idx), 100) / 100.0
            if str(idx) in lim_cal:
                lim = max(LIM_MIN_BLOB, int(lim_cal[str(idx)] * tolerancia / TOL_REF * fz))
            else:
                lim = max(LIM_MIN_BLOB, int(zw * zh * frac * fz))
            ok = area <= lim
            if not ok:
                todo_ok = False
            dif_max = max(dif_max, area)
            zonas_res.append({"idx": idx, "pix": area, "blobs": n_blobs, "lim": lim,
                              "ok": ok, "rect": (zx1, zy1, zx2, zy2)})

        return {
            "zonas": zonas_res,
            "ok": bool(todo_ok and match >= MATCH_OK),
            "dif_max": dif_max,
            "match": match,
        }


class VisionEngine(OriginalVision):
    def __init__(self, store, model):
        self.model = copy.deepcopy(model)
        self.config = c = validate_config(self.model['config'], True)
        store.integrity(model, True)
        self.zonas_relativas = {str(i): c['zones'][str(i)]['rect'] for i in range(1, c['zone_count']+1)}
        self.factores_zona = {str(i): c['zones'][str(i)]['factor'] for i in range(1, c['zone_count']+1)}
        self._kernel_morf = np.ones((3, 3), np.uint8)
        self._ultimo_ajuste = None
        refs = []
        for e in c['references']:
            img = store.image(model['id'], e)
            refs.append(img if e.get('processed') else self.adjust(img))
        if len(refs) != 10:
            raise ValueError('Se necesitan 10 referencias OK.')
        base_L = cv2.cvtColor(refs[0], cv2.COLOR_BGR2LAB)[..., 0]
        if float(base_L.std()) < 1:
            raise ValueError('La referencia 1 no tiene contraste suficiente para localizar la pieza.')
        h, w = refs[0].shape[:2]
        aligned, bad = [refs[0]], []
        for i, ref in enumerate(refs[1:], 2):
            matrix = self._alinear_a(base_L, cv2.cvtColor(ref, cv2.COLOR_BGR2LAB)[..., 0])
            if matrix is None:
                bad.append(i)
            else:
                aligned.append(cv2.warpAffine(ref, matrix, (w, h), flags=cv2.INTER_LINEAR,
                                               borderMode=cv2.BORDER_REPLICATE))
        if bad:
            raise ValueError('No se pudieron alinear estas referencias OK: '+', '.join(map(str, bad))+'. Reemplázalas.')
        stack = np.stack([cv2.GaussianBlur(cv2.cvtColor(r, cv2.COLOR_BGR2LAB), (5, 5), 0).astype(np.float32) for r in aligned])
        mean = stack.mean(axis=0)
        u8 = np.clip(mean, 0, 255).astype(np.uint8)
        gray = cv2.cvtColor(cv2.cvtColor(u8, cv2.COLOR_LAB2BGR), cv2.COLOR_BGR2GRAY)
        cache = dict(ancho=w, alto=h, media_f=mean, media_u8=u8, media_L=u8[...,0].copy(),
                     umbral=self._umbral_pixel(stack.std(axis=0)), tpl_gray=gray,
                     tpl_gray_s=cv2.resize(gray, None, fx=.5, fy=.5, interpolation=cv2.INTER_AREA),
                     L_mean=float(mean[...,0].mean()), L_std=float(mean[...,0].std()),
                     a_med=float(np.median(mean[...,1])), b_med=float(np.median(mean[...,2])),
                     a_std=float(mean[...,1].std()), b_std=float(mean[...,2].std()))
        cache['lim_zona'] = self._calibrar_limites(stack, cache, c['zone_count'])
        self.cache = cache
        self.aligned_references = len(aligned)

    def adjust(self, frame):
        return cv2.convertScaleAbs(frame, alpha=self.config['contrast']/100., beta=self.config['brightness'])

    def check_frame(self, frame):
        w, h = self.config['camera']['width'], self.config['camera']['height']
        if frame is None or frame.shape[:2] != (h, w):
            raise ValueError(f'Resolución incompatible. Este modelo requiere {w} × {h}.')

    def _normalizar_iluminacion(self, roi_lab, cache):
        normalized = super()._normalizar_iluminacion(roi_lab, cache)
        if not self.config['normalize_color']:
            normalized[...,1:] = roi_lab[...,1:]
        return normalized

    def _localizar(self, frame_gray, cache, h, w):
        # Whole-frame search supports administrator-defined ROIs anywhere in the image.
        tpl, small = cache['tpl_gray'], cache['tpl_gray_s']
        th, tw = tpl.shape[:2]
        if h < th or w < tw:
            return 0., 0, 0
        reduced = cv2.resize(frame_gray, None, fx=.5, fy=.5, interpolation=cv2.INTER_AREA)
        result = cv2.matchTemplate(reduced, small, cv2.TM_CCOEFF_NORMED)
        result = np.nan_to_num(result, nan=0., posinf=0., neginf=0.)
        _, _, _, loc = cv2.minMaxLoc(result)
        x, y = loc[0]*2, loc[1]*2
        x0, y0 = max(0, x-6), max(0, y-6)
        patch = frame_gray[y0:min(h, y+th+6), x0:min(w, x+tw+6)]
        result = cv2.matchTemplate(patch, tpl, cv2.TM_CCOEFF_NORMED)
        result = np.nan_to_num(result, nan=0., posinf=0., neginf=0.)
        _, score, _, loc = cv2.minMaxLoc(result)
        return float(score), x0+loc[0], y0+loc[1]

    def locate(self, adjusted):
        h, w = adjusted.shape[:2]
        return self._localizar(cv2.cvtColor(adjusted, cv2.COLOR_BGR2GRAY), self.cache, h, w)

    def evaluate_roi(self, roi, match):
        result = self._evaluar_zonas(roi, self.cache, self.config['zone_count'], self.config['tolerance'], match)
        for z in result['zonas']:
            z['name'] = self.config['zones'][str(z['idx'])]['name']
        result['adjustment'] = self._ultimo_ajuste
        return result

    def evaluate_sample(self, image, entry):
        adjusted = image if entry.get('processed') else self.adjust(image)
        if entry['kind'] == 'crop':
            if adjusted.shape[:2] != (self.cache['alto'], self.cache['ancho']):
                raise ValueError('La muestra recortada tiene dimensiones incompatibles.')
            # A crop is a labelled specimen ROI; use measured correlation, never a fabricated match.
            gray = cv2.cvtColor(adjusted, cv2.COLOR_BGR2GRAY)
            score = float(cv2.matchTemplate(gray, self.cache['tpl_gray'], cv2.TM_CCOEFF_NORMED)[0,0])
            x, y = 0, 0
        else:
            self.check_frame(image)
            score, x, y = self.locate(adjusted)
        if not np.isfinite(score) or score < MATCH_PRESENCIA:
            return None, adjusted.copy(), 'Sin coincidencia suficiente: muestra sin evaluación.'
        roi = self._roi_alineada(adjusted, x, y, self.cache)
        if roi is None:
            return None, adjusted.copy(), 'Región fuera de límites.'
        result = self.evaluate_roi(roi, score)
        return result, self.annotate(adjusted, result, x, y), ''

    def annotate(self, frame, result, x, y):
        out = frame.copy()
        if result:
            for z in result['zonas']:
                x1,y1,x2,y2 = z['rect']
                color = (0,200,0) if z['ok'] else (0,0,230)
                cv2.rectangle(out,(x+x1,y+y1),(x+x2,y+y2),color,2)
                cv2.putText(out, f"Z{z['idx']}", (x+x1,y+y1+18),cv2.FONT_HERSHEY_SIMPLEX,.5,color,1)
        return out


class InspectionCycle:
    """One event until timed visual absence; motion and pauses never unlatch an event."""
    def __init__(self, engine, require_clear=True):
        self.engine = engine
        self.require_clear = require_clear
        self.latched = False
        self.result = None
        self.absent_since = None
        self.interrupt()

    def interrupt(self):
        self.previous = None
        self.stable = 0
        self.accum = None
        self.count = 0
        self.absent_since = None

    def disconnected(self):
        self.require_clear = True
        self.result = None
        self.latched = False
        self.interrupt()

    def process(self, frame, timestamp=None):
        t = time.monotonic() if timestamp is None else timestamp
        e = self.engine
        e.check_frame(frame)
        adjusted = e.adjust(frame)
        score, x, y = e.locate(adjusted)
        output = dict(state='', result=None, event=None, annotated=adjusted, match=score)
        if score < MATCH_PRESENCIA:
            if self.absent_since is None:
                self.absent_since = t
            self.previous, self.stable, self.accum, self.count = None, 0, None, 0
            if t-self.absent_since >= e.config['absence_seconds']:
                self.latched, self.require_clear, self.result = False, False, None
                output['state'] = 'Esperando pieza · sin evaluación'
            else:
                output['state'] = 'Confirmando retiro · sin evaluación'
            return output
        self.absent_since = None
        if self.require_clear:
            output['state'] = 'Retira la pieza para armar el ciclo'
            return output
        if self.latched:
            output.update(state='Resultado emitido · esperando retiro', result=self.result,
                          annotated=e.annotate(adjusted, self.result, x, y))
            return output
        if self.previous and abs(x-self.previous[0]) <= TOL_POS and abs(y-self.previous[1]) <= TOL_POS:
            self.stable += 1
        else:
            self.stable, self.accum, self.count = 0, None, 0
        self.previous = (x,y)
        if self.stable < FRAMES_ESTABLES:
            output['state'] = f'Estabilizando {self.stable}/{FRAMES_ESTABLES}'
            return output
        roi = e._roi_alineada(adjusted, x,y,e.cache)
        if roi is None:
            self.interrupt()
            output['state'] = 'Sin evaluación · región fuera de límites'
            return output
        if self.accum is None:
            self.accum = np.zeros_like(roi, dtype=np.float32)
        self.accum += roi
        self.count += 1
        if self.count < FRAMES_PROMEDIO:
            output['state'] = f'Evaluando {self.count}/{FRAMES_PROMEDIO}'
            return output
        result = e.evaluate_roi(self.accum/self.count, score)
        self.result, self.latched = result, True
        output.update(state='Resultado emitido · esperando retiro', result=result,
                      event=result, annotated=e.annotate(adjusted,result,x,y))
        return output


# ========================================================================
# CAMERA
# ========================================================================
"""La cámara pertenece a un solo hilo. Tkinter nunca bloquea esperando read()."""
import copy
import queue
import threading
import time
import cv2


class Camera:
    def __init__(self):
        self.commands = queue.Queue()
        self.lock = threading.Lock()
        self.frame = None
        self.timestamp = 0.
        self.info = {'status': 'Cámara sin abrir', 'ready': False, 'generation': 0, 'properties': {}}
        self.generation = 0
        self._first_frame = None
        self._backend_name = 'Automático'
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name='camara-pokayoke', daemon=True)
        self.thread.start()

    def configure(self, settings):
        with self.lock:
            self.generation += 1
            generation = self.generation
            self.frame = None
            self.info = {'status': 'Abriendo cámara…', 'ready': False,
                         'generation': generation, 'properties': {}}
        self.commands.put((generation, copy.deepcopy(settings)))
        return generation

    def snapshot(self):
        with self.lock:
            frame = None if self.frame is None else self.frame.copy()
            return frame, self.timestamp, copy.deepcopy(self.info)

    def _publish(self, generation, frame=None, **info):
        with self.lock:
            if generation != self.generation:
                return
            if frame is not None:
                self.frame = frame
                self.timestamp = time.monotonic()
            elif not info.get('ready', True):
                self.frame = None
            self.info.update(info)
            self.info['generation'] = generation

    @staticmethod
    def backend_candidates():
        options = [(cv2.CAP_ANY, 'Automático')]
        if os.name == 'nt':
            options += [(cv2.CAP_DSHOW, 'DirectShow'), (cv2.CAP_MSMF, 'Media Foundation')]
        return options

    @staticmethod
    def _valid_frame(frame):
        return (isinstance(frame,np.ndarray) and frame.ndim == 3
                and frame.shape[2] == 3 and frame.size > 0)

    def _open(self, settings):
        self._first_frame = None
        failures = []
        for backend,name in self.backend_candidates():
            if self.stop_event.is_set():
                raise RuntimeError('Apertura de cámara cancelada.')
            cap = None
            try:
                cap = (cv2.VideoCapture(settings['index']) if backend == cv2.CAP_ANY
                       else cv2.VideoCapture(settings['index'],backend))
                if not cap.isOpened():
                    raise RuntimeError('dispositivo no disponible')
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, settings['width'])
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, settings['height'])
                # Algunas webcams necesitan varios intentos para entregar su primer cuadro.
                frame = None
                for _ in range(8):
                    if self.stop_event.is_set():
                        raise RuntimeError('apertura cancelada')
                    ok,candidate = cap.read()
                    if ok and self._valid_frame(candidate):
                        frame = candidate
                        break
                    self.stop_event.wait(.05)
                if frame is None:
                    raise RuntimeError('abierta, pero sin imágenes')
                props = self._configure_properties(cap,settings)
                self._first_frame = frame
                self._backend_name = name
                return cap,props
            except Exception as e:
                failures.append(f'{name}: {e}')
                if cap is not None:
                    cap.release()
        raise RuntimeError(
            f'No se obtiene imagen de la cámara {settings["index"]}. '
            'Comprueba el número de cámara en Administración, cierra otras aplicaciones que la usen '
            'y revisa el permiso de cámara para aplicaciones de escritorio en Windows. '
            + ' | '.join(failures))

    def _configure_properties(self,cap,settings):
        props = {}
        for key, prop, auto in (
            ('exposure', cv2.CAP_PROP_EXPOSURE, cv2.CAP_PROP_AUTO_EXPOSURE),
            ('focus', cv2.CAP_PROP_FOCUS, cv2.CAP_PROP_AUTOFOCUS),
            ('white_balance', cv2.CAP_PROP_WB_TEMPERATURE, cv2.CAP_PROP_AUTO_WB)):
            requested = settings.get(key)
            if requested is not None:
                # La escala de exposición depende del controlador; verificar la lectura real.
                cap.set(auto, .25 if key == 'exposure' else 0)
                accepted = bool(cap.set(prop, requested))
                actual = float(cap.get(prop))
                verified = accepted and abs(actual-requested) <= max(.05, abs(requested)*.02)
                props[key] = {'requested': requested, 'actual': actual, 'verified': verified}
            else:
                props[key] = {'requested': None, 'actual': float(cap.get(prop)), 'verified': None}
        return props

    def _run(self):
        cap = None
        generation = 0
        props = {}
        settings = None
        try:
            while not self.stop_event.is_set():
                command = None
                try:
                    while True:
                        command = self.commands.get_nowait()
                except queue.Empty:
                    pass
                if command:
                    generation, settings = command
                    if cap is not None:
                        cap.release()
                        cap = None
                    try:
                        cap, props = self._open(settings)
                    except Exception as e:
                        self._publish(generation, ready=False, status=str(e), properties={})
                if cap is None:
                    self.stop_event.wait(.03)
                    continue
                try:
                    if self._first_frame is not None:
                        frame,self._first_frame = self._first_frame,None
                        ret = True
                    else:
                        ret,frame = cap.read()
                except Exception as e:
                    self._publish(generation,ready=False,status=f'Error al leer la cámara: {e}. Pulsa Abrir / reconectar cámara.')
                    cap.release()
                    cap = None
                    continue
                if not ret or not self._valid_frame(frame):
                    self._publish(generation, ready=False, status='Cámara sin imagen. Revisa la conexión y pulsa Abrir cámara.')
                    cap.release()
                    cap = None
                    continue
                h, w = frame.shape[:2]
                mismatch = (w,h) != (settings['width'],settings['height'])
                failed = [key for key,v in props.items() if v['verified'] is False]
                status = f'Cámara {settings["index"]} · {self._backend_name} · resolución real {w} × {h}'
                if mismatch:
                    status += ' · resolución solicitada no aceptada'
                if failed:
                    status += ' · ajustes no comprobados: '+', '.join(failed)
                self._publish(generation, frame, ready=True, status=status,
                              width=w, height=h, compatible=not mismatch and not failed,
                              properties=props,backend=self._backend_name)
                self.stop_event.wait(.01)
        except Exception as e:
            self._publish(generation, ready=False, status=f'Error de cámara: {e}')
        finally:
            if cap is not None:
                cap.release()

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=2)


# ========================================================================
# WIDGETS
# ========================================================================
import tkinter as tk
from tkinter import ttk, messagebox
import cv2
from PIL import Image, ImageTk


class ImageCanvas(tk.Canvas):
    def __init__(self, parent, **kw):
        super().__init__(parent, bg='#0a111d', highlightthickness=0, **kw)
        self.source = None
        self.scale_factor = 1.
        self.offset = (0,0)
        self.bind('<Configure>', lambda e: self.redraw())

    def display(self, image):
        self.source = None if image is None else image.copy()
        self.redraw()

    def redraw(self):
        self.delete('all')
        if self.source is None:
            self.create_text(max(100,self.winfo_width()/2), max(80,self.winfo_height()/2),
                             text='Sin imagen de cámara', fill='#91a1b9', font=('Segoe UI',14))
            return
        h,w = self.source.shape[:2]
        cw,ch = max(10,self.winfo_width()),max(10,self.winfo_height())
        s = min(cw/w,ch/h)
        nw,nh = max(1,int(w*s)),max(1,int(h*s))
        self.scale_factor = s
        self.offset = ((cw-nw)//2,(ch-nh)//2)
        rgb = cv2.cvtColor(self.source, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(rgb).resize((nw,nh),Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(image)
        self.create_image(*self.offset, image=self.photo, anchor='nw')

    def image_point(self, x, y):
        if self.source is None:
            return None
        h,w = self.source.shape[:2]
        ox,oy = self.offset
        return max(0,min(w-1,int((x-ox)/self.scale_factor))), max(0,min(h-1,int((y-oy)/self.scale_factor)))


class RegionDialog(tk.Toplevel):
    def __init__(self, parent, image, title, initial=None, minimum=6):
        super().__init__(parent)
        self.title(title)
        self.geometry('940x650')
        self.transient(parent)
        self.result = None
        self.minimum = minimum
        self.start = None
        self.image = image
        self.rect = initial
        ttk.Label(self,text='Arrastra sobre la imagen para dibujar. Los bordes se guardan en coordenadas de la imagen.',
                  padding=10).pack(fill='x')
        self.canvas = ImageCanvas(self)
        self.canvas.pack(fill='both',expand=True,padx=10)
        self.canvas.display(image)
        self.canvas.bind('<ButtonPress-1>',self.press)
        self.canvas.bind('<B1-Motion>',self.drag)
        self.canvas.bind('<ButtonRelease-1>',self.release)
        self.canvas.bind('<Configure>',self.resize,add='+')
        controls = ttk.Frame(self,padding=10)
        controls.pack(fill='x')
        ttk.Button(controls,text='Cancelar',command=self.destroy).pack(side='right')
        ttk.Button(controls,text='Aceptar región',command=self.accept).pack(side='right',padx=8)
        self.bind('<Escape>',lambda e:self.destroy())
        self.grab_set()
        self.after(80,self.paint)

    def resize(self,event):
        self.after_idle(self.paint)

    def press(self,event):
        self.start = self.canvas.image_point(event.x,event.y)

    def drag(self,event):
        if self.start is None:
            return
        point = self.canvas.image_point(event.x,event.y)
        self.rect = [min(self.start[0],point[0]),min(self.start[1],point[1]),
                     max(self.start[0],point[0]),max(self.start[1],point[1])]
        self.paint()

    def release(self,event):
        self.drag(event)
        self.start = None

    def paint(self):
        self.canvas.delete('region')
        if self.rect:
            s = self.canvas.scale_factor
            ox,oy = self.canvas.offset
            x1,y1,x2,y2 = self.rect
            self.canvas.create_rectangle(ox+x1*s,oy+y1*s,ox+x2*s,oy+y2*s,
                                         outline='#35e0b5',width=3,tags='region')

    def accept(self):
        if not self.rect or self.rect[2]-self.rect[0] < self.minimum or self.rect[3]-self.rect[1] < self.minimum:
            messagebox.showwarning('Región incompleta',f'Dibuja una región de al menos {self.minimum} × {self.minimum} píxeles.',parent=self)
            return
        self.result = list(self.rect)
        self.destroy()


class ScrollFrame(ttk.Frame):
    def __init__(self,parent):
        super().__init__(parent)
        canvas = tk.Canvas(self,bg='#142033',highlightthickness=0)
        bar = ttk.Scrollbar(self,orient='vertical',command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        bar.pack(side='right',fill='y')
        canvas.pack(side='left',fill='both',expand=True)
        self.inner = ttk.Frame(canvas)
        win = canvas.create_window((0,0),window=self.inner,anchor='nw')
        self.inner.bind('<Configure>',lambda e:canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>',lambda e:canvas.itemconfigure(win,width=e.width))
        def wheel(event):
            canvas.yview_scroll(-1 if event.delta>0 or event.num==4 else 1,'units')
        self.scroll_callback = wheel
        def bind_children(widget):
            widget.bind('<MouseWheel>',wheel)
            widget.bind('<Button-4>',wheel)
            widget.bind('<Button-5>',wheel)
            for child in widget.winfo_children():
                bind_children(child)
        self.after(100,lambda:bind_children(self))


# ========================================================================
# UI
# ========================================================================
"""Interfaz local en español. Todos los widgets se actualizan en el hilo de Tk."""
import copy
from concurrent.futures import ThreadPoolExecutor
import functools
import json
from pathlib import Path
import time
import traceback
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
import uuid
import cv2
from PIL import Image, ImageTk


EVENT_TYPES = {'production':'Producción', 'validation':'Validación',
               'test':'Prueba', 'manual':'Reevaluación manual'}


def admin_action(fn):
    @functools.wraps(fn)
    def wrapped(self, *args, **kwargs):
        if not self.admin_mode:
            raise PermissionError('Entra a Administración para modificar modelos.')
        if self.task is not None:
            raise RuntimeError('Espera a que termine la operación en curso.')
        return fn(self, *args, **kwargs)
    return wrapped


def button(parent, text, command, **kw):
    b = ttk.Button(parent, text=text, command=command, **kw)
    b.pack(side='left', padx=(0,6), pady=3)
    return b


def table(parent, columns, height=10):
    frame = ttk.Frame(parent)
    frame.pack(fill='both',expand=True)
    tree = ttk.Treeview(frame, columns=[k for k,t,w in columns],show='headings',height=height,selectmode='browse')
    for key,title,width in columns:
        tree.heading(key,text=title)
        tree.column(key,width=width,minwidth=50,stretch=True)
    vs = ttk.Scrollbar(frame,orient='vertical',command=tree.yview)
    hs = ttk.Scrollbar(frame,orient='horizontal',command=tree.xview)
    tree.configure(yscrollcommand=vs.set,xscrollcommand=hs.set)
    vs.pack(side='right',fill='y')
    hs.pack(side='bottom',fill='x')
    tree.pack(fill='both',expand=True)
    return tree


class Application:
    def __init__(self, root, data_dir, camera=None):
        self.root = root
        self.store = Store(data_dir)
        self.auth = Auth(data_dir)
        self.camera = camera or Camera()
        self.executor = ThreadPoolExecutor(max_workers=1,thread_name_prefix='motor-pokayoke')
        self.task = None
        self.closed = False
        self.admin_mode = False
        self.model = self.draft = None
        self.active_model = self.engine = self.cycle = None
        self.running = self.loading = self.dirty = False
        self.last_stamp = 0.
        self.last_generation = None
        self.generation = None
        self.last_event = None
        self.gallery_slot = 0
        self.thumbnails = []
        self.root.title('Poka-Yoke 5 · Inspección y administración')
        width = min(1320,max(800,self.root.winfo_screenwidth()-40))
        height = min(840,max(600,self.root.winfo_screenheight()-40))
        self.root.geometry(f'{width}x{height}')
        self.root.minsize(min(1000,width),min(680,height))
        self.root.configure(bg='#0c1422')
        self._style()
        self.root.report_callback_exception = self._callback_error
        self.root.protocol('WM_DELETE_WINDOW',self.close)
        header = ttk.Frame(root,padding=(18,12))
        header.pack(fill='x')
        ttk.Label(header,text='POKA-YOKE',style='Title.TLabel').pack(side='left')
        self.mode_label = ttk.Label(header,text='INSPECCIÓN',style='Accent.TLabel')
        self.mode_label.pack(side='left',padx=18)
        self.access_button = ttk.Button(header,text='Administración',command=self.toggle_admin)
        self.access_button.pack(side='right')
        self.camera_button = ttk.Button(header,text='Abrir / reconectar cámara',command=self.reconnect_camera)
        self.camera_button.pack(side='right',padx=8)
        self.body = ttk.Frame(root,padding=(12,0))
        self.body.pack(fill='both',expand=True)
        self.ops = ttk.Frame(self.body)
        self.admin = ttk.Frame(self.body)
        self._build_operations()
        self._build_admin()
        self.ops.pack(fill='both',expand=True)
        footer = ttk.Frame(root,padding=(16,8))
        footer.pack(side='bottom',fill='x',before=self.body)
        self.message = tk.StringVar(value='Selecciona un modelo validado o entra a Administración para prepararlo.')
        ttk.Label(footer,textvariable=self.message,wraplength=1200).pack(anchor='w')
        self.camera_status = tk.StringVar(value='Cámara sin abrir')
        ttk.Label(footer,textvariable=self.camera_status,style='Muted.TLabel',wraplength=1200).pack(anchor='w')
        self._refresh_catalog()
        self.poll_id = self.root.after(80,self._poll)
        # La vista previa es independiente de preparar o validar un modelo.
        self._configure_preview(defaults()['camera'])

    def _style(self):
        s = ttk.Style(self.root)
        s.theme_use('clam')
        families = {str(f).casefold() for f in self.root.tk.call('font','families')}
        family = 'Segoe UI' if 'segoe ui' in families else 'Helvetica'
        s.configure('.',background='#142033',foreground='#e7eef9',fieldbackground='#1e3049',font=(family,10))
        s.configure('TFrame',background='#142033')
        s.configure('TLabel',background='#142033',foreground='#e7eef9')
        s.configure('Title.TLabel',font=(family,21,'bold'))
        s.configure('Accent.TLabel',foreground='#35e0b5',font=(family,11,'bold'))
        s.configure('Muted.TLabel',foreground='#9cabc1')
        s.configure('Result.TLabel',font=(family,30,'bold'),padding=12)
        s.configure('TButton',padding=(10,7),background='#243954')
        s.map('TButton',background=[('active','#365678'),('disabled','#1a293e')])
        s.configure('Treeview',background='#101b2c',fieldbackground='#101b2c',rowheight=30)
        s.configure('Treeview.Heading',background='#243954',font=(family,10,'bold'))
        s.map('Treeview',background=[('selected','#236f86')])
        s.configure('TNotebook.Tab',padding=(15,8))
        s.configure('TEntry',padding=5)
        s.configure('TCombobox',padding=5)
        s.map('TCombobox',fieldbackground=[('readonly','#1e3049')],foreground=[('readonly','#e7eef9')])

    def _build_operations(self):
        top = ttk.Frame(self.ops,padding=10)
        top.pack(fill='x')
        ttk.Label(top,text='Número de parte').grid(row=0,column=0,sticky='w')
        self.op_part = tk.StringVar()
        self.op_combo = ttk.Combobox(top,textvariable=self.op_part,state='readonly',width=28)
        self.op_combo.grid(row=1,column=0,padx=(0,12),sticky='ew')
        self.op_combo.bind('<<ComboboxSelected>>',lambda e:self._select_operation())
        self.operator = tk.StringVar()
        self.lot = tk.StringVar()
        self.order = tk.StringVar()
        self.context_widgets = []
        for col,label,var in [(1,'Operador',self.operator),(2,'Lote',self.lot),(3,'Orden (opcional)',self.order)]:
            ttk.Label(top,text=label).grid(row=0,column=col,sticky='w')
            entry = ttk.Entry(top,textvariable=var,width=20)
            entry.grid(row=1,column=col,sticky='ew',padx=(0,10))
            self.context_widgets.append(entry)
            top.columnconfigure(col,weight=1)
        self.lot.trace_add('write',lambda *args:self._refresh_counts())
        self.lot.trace_add('write',lambda *args:self._refresh_recent() if hasattr(self,'recent') else None)
        self.op_description = ttk.Label(self.ops,text='Sin modelo seleccionado',padding=(12,4))
        self.op_description.pack(fill='x')
        main = ttk.Panedwindow(self.ops,orient='horizontal')
        main.pack(fill='both',expand=True,pady=8)
        self.video = ImageCanvas(main,width=720,height=300)
        main.add(self.video,weight=4)
        side = ttk.Frame(main,padding=12)
        main.add(side,weight=1)
        self.result_label = ttk.Label(side,text='SIN EVALUACIÓN',style='Result.TLabel',foreground='#a6b6cb',wraplength=300)
        self.result_label.pack(fill='x')
        self.cycle_status = tk.StringVar(value='Inspección pausada')
        ttk.Label(side,textvariable=self.cycle_status,wraplength=280).pack(fill='x',pady=6)
        self.zone_results = ttk.Label(side,text='Las zonas aparecerán al evaluar.',wraplength=280,justify='left')
        self.zone_results.pack(fill='x',pady=12)
        self.ref_preview = ttk.Label(side,text='Referencia del modelo')
        self.ref_preview.pack(pady=8)
        self.counts = tk.StringVar(value='Total: 0   |   OK: 0   |   NG: 0   |   NG: 0.0 %')
        ttk.Label(self.ops,textvariable=self.counts,style='Accent.TLabel',padding=8).pack(fill='x')
        actions = ttk.Frame(self.ops,padding=(8,0))
        actions.pack(fill='x')
        self.start_button = button(actions,'Iniciar inspección',self.start_or_pause)
        self.choose_button = button(actions,'Buscar número de parte',self.choose_part)
        button(actions,'Última inspección',self.show_last)
        button(actions,'Historial y fotografías',self.show_history)
        self.recent = table(self.ops,[('time','Hora',130),('part','Parte',160),('lot','Lote',120),
                                     ('outcome','Resultado',110),('version','Versión',80)],height=3)
        self.recent.bind('<Double-1>',lambda e:self._open_event(self.recent))
        self.ops.bind('<Configure>',self._responsive_operation)

    def _responsive_operation(self,event):
        # Keep the camera, result and actions visible on smaller laptop screens.
        frame = self.recent.master
        if event.height < 650 and frame.winfo_manager():
            frame.pack_forget()
        elif event.height >= 650 and not frame.winfo_manager():
            frame.pack(fill='both',expand=True)

    def _build_admin(self):
        self.admin_tabs = ttk.Notebook(self.admin)
        self.admin_tabs.pack(fill='both',expand=True)
        catalog = ttk.Frame(self.admin_tabs,padding=10)
        self.editor = ttk.Frame(self.admin_tabs,padding=8)
        self.admin_tabs.add(catalog,text='Números de parte')
        self.admin_tabs.add(self.editor,text='Configuración del modelo')
        search = ttk.Frame(catalog)
        search.pack(fill='x',pady=(0,8))
        ttk.Label(search,text='Buscar:').pack(side='left')
        self.search = tk.StringVar()
        ttk.Entry(search,textvariable=self.search,width=35).pack(side='left',padx=8)
        self.archived = tk.BooleanVar(value=False)
        ttk.Checkbutton(search,text='Mostrar archivados',variable=self.archived,command=self._refresh_catalog).pack(side='left')
        self.search.trace_add('write',lambda *a:self._refresh_catalog())
        self.catalog = table(catalog,[('part','Número de parte',170),('description','Descripción',220),
                                      ('refs','OK / 10',80),('zones','Zonas',65),('state','Estado',210),
                                      ('version','Versión',65),('saved','Guardado',160)],height=12)
        self.catalog.bind('<Double-1>',lambda e:self.open_model())
        self.catalog.bind('<<TreeviewSelect>>',lambda e:self._catalog_thumbnail())
        self.catalog_photo = ttk.Label(catalog,text='Selecciona un modelo para ver su referencia.')
        self.catalog_photo.pack(anchor='w',pady=6)
        actions = ttk.Frame(catalog)
        actions.pack(fill='x')
        for text,action in [('Nuevo',self.new_model),('Abrir',self.open_model),('Renombrar / descripción',self.rename_model),
                            ('Duplicar',self.duplicate_model),('Archivar / restaurar',self.archive_model)]:
            button(actions,text,action)
        extra = ttk.Frame(catalog)
        extra.pack(fill='x')
        for text,action in [('Exportar modelo',self.export_model),('Importar paquete',self.import_model),
                            ('Importar modelos v3/v4',self.import_legacy),('Cambiar contraseña',self.change_password),
                            ('Errores del catálogo',self.show_catalog_errors)]:
            button(extra,text,action)
        bar = ttk.Frame(self.editor)
        bar.pack(fill='x')
        self.model_title = ttk.Label(bar,text='Abre o crea un número de parte.',style='Accent.TLabel')
        self.model_title.pack(side='left',fill='x',expand=True)
        self.dirty_label = ttk.Label(bar,text='',foreground='#ffca78')
        self.dirty_label.pack(side='right')
        actions = ttk.Frame(self.editor)
        actions.pack(fill='x',pady=(5,8))
        for text,action in [('Guardar configuración',self.save_model),('Restaurar guardada',self.discard_changes),
                            ('Recuperar versión anterior',self.restore_previous),('Comprobar / probar ajustes',self.try_settings)]:
            button(actions,text,action)
        body = ttk.Panedwindow(self.editor,orient='horizontal')
        body.pack(fill='both',expand=True)
        scroll = ScrollFrame(body)
        body.add(scroll,weight=1)
        controls = scroll.inner
        self.fields = {}
        def field(key,label,value,values=None):
            ttk.Label(controls,text=label).pack(anchor='w',padx=8,pady=(8,2))
            var = tk.StringVar(value=str(value))
            self.fields[key] = var
            widget = ttk.Combobox(controls,textvariable=var,values=values,state='readonly') if values else ttk.Entry(controls,textvariable=var)
            widget.pack(fill='x',padx=8)
            var.trace_add('write',self._mark_dirty)
        field('camera.index','Número de cámara',0,list(range(11)))
        field('camera.width','Ancho solicitado (píxeles)',1280)
        field('camera.height','Alto solicitado (píxeles)',720)
        for key,label in [('exposure','Exposición'),('focus','Enfoque'),('white_balance','Temperatura de blanco')]:
            field('camera.'+key,label+' (vacío = sin solicitar ajuste)','')
        row = ttk.Frame(controls,padding=8)
        row.pack(fill='x')
        button(row,'Abrir cámara',self.open_camera)
        button(row,'Usar resolución real',self.use_actual_resolution)
        details = ttk.Frame(controls,padding=(8,0))
        details.pack(fill='x')
        button(details,'Detalles de cámara',self.camera_details)
        field('brightness','Brillo (-100 a 100)',0)
        field('contrast','Contraste (50 a 300)',100)
        field('tolerance','Tolerancia base (2000 a 15000)',5000)
        ttk.Label(controls,text='Mayor tolerancia o porcentaje admite diferencias mayores.',wraplength=320,
                  style='Muted.TLabel').pack(fill='x',padx=8,pady=5)
        field('absence_seconds','Segundos para confirmar retiro (0.1 a 10)',.6)
        field('zone_count','Zonas activas',3,[3,5])
        self.normalize_color = tk.BooleanVar(value=True)
        ttk.Checkbutton(controls,text='Normalizar color (comportamiento de v4)',variable=self.normalize_color,
                        command=self._mark_dirty).pack(anchor='w',padx=8,pady=6)
        ttk.Label(controls,text='Puede ocultar cambios de color. Desactívalo si el color es un criterio y valida nuevamente.',
                  wraplength=320,style='Muted.TLabel').pack(fill='x',padx=8)
        self.zone_fields = {}
        for i in range(1,6):
            box = ttk.LabelFrame(controls,text=f'Zona {i}',padding=6)
            box.pack(fill='x',padx=8,pady=5)
            name = tk.StringVar(value=f'Zona {i}')
            factor = tk.StringVar(value='100')
            ttk.Entry(box,textvariable=name).pack(fill='x')
            row = ttk.Frame(box)
            row.pack(fill='x',pady=4)
            ttk.Label(row,text='Límite (% de calibración):').pack(side='left')
            ttk.Spinbox(row,from_=50,to=300,textvariable=factor,width=6).pack(side='right')
            button(box,'Dibujar / editar zona',lambda idx=i:self.edit_zone(idx))
            state = ttk.Label(box,text='Sin dibujar',style='Muted.TLabel')
            state.pack(anchor='w')
            name.trace_add('write',self._mark_dirty)
            factor.trace_add('write',self._mark_dirty)
            self.zone_fields[str(i)] = (name,factor,state)
        right = ttk.Panedwindow(body,orient='vertical')
        body.add(right,weight=3)
        preview = ttk.Frame(right)
        right.add(preview,weight=2)
        self.admin_video = ImageCanvas(preview,width=650,height=190)
        self.admin_video.pack(fill='both',expand=True)
        preview_controls = ttk.Frame(preview)
        preview_controls.pack(side='bottom',fill='x',before=self.admin_video)
        row = ttk.Frame(preview_controls,padding=(4,2))
        row.pack(fill='x')
        button(row,'Dibujar región general',self.edit_roi)
        button(row,'Abrir / reconectar cámara',self.open_camera)
        self.roi_label = ttk.Label(preview_controls,text='Región: sin configurar',padding=(6,2))
        self.roi_label.pack(fill='x')
        self.editor_tabs = ttk.Notebook(right)
        right.add(self.editor_tabs,weight=3)
        self.photo_fields = {}
        self.photo_targets = {}
        self.photo_baselines = {}
        self._photo_loading = False
        self.sample_galleries = {}
        self.selected_sample_ids = {'OK':None,'NG':None}
        gallery = ttk.Frame(self.editor_tabs)
        self.reference_page = gallery
        self.editor_tabs.add(gallery,text='Referencias OK')
        self.gallery_info = ttk.Label(gallery,text='0 / 10 referencias OK',style='Accent.TLabel')
        self.gallery_info.pack(anchor='w',pady=4)
        row = ttk.Frame(gallery)
        row.pack(fill='x')
        self.capture_ok_button = button(row,'CAPTURAR OK',self.capture_reference)
        button(row,'Repetir seleccionada',self.retake_reference)
        button(row,'Importar imagen',self.import_reference)
        button(row,'Eliminar',self.remove_reference)
        self.ref_buttons = []
        split = ttk.Panedwindow(gallery,orient='horizontal')
        split.pack(fill='both',expand=True)
        ref_scroll = ScrollFrame(split)
        split.add(ref_scroll,weight=1)
        self.reference_scroller = ref_scroll
        grid = ref_scroll.inner
        details = ttk.Frame(split)
        split.add(details,weight=1)
        for i in range(10):
            b = ttk.Button(grid,text=f'OK {i+1}\nSin imagen',command=lambda idx=i:self.select_reference(idx))
            b.grid(row=i//2,column=i%2,sticky='nsew',padx=3,pady=3)
            b.bind('<Double-1>',lambda e,idx=i:self.view_reference(idx))
            self.ref_buttons.append(b)
        for i in range(2):
            grid.columnconfigure(i,weight=1)
        self._build_photo_details(details,'reference')
        for truth,title in [('OK','Pruebas OK'),('NG','Fotos NG')]:
            page = ttk.Frame(self.editor_tabs)
            self.editor_tabs.add(page,text=title)
            ttk.Label(page,text=('Piezas OK independientes de las referencias del patrón.' if truth == 'OK'
                                else 'Captura NG y selecciona su miniatura para describir el defecto.'),
                      wraplength=750).pack(fill='x',pady=2)
            row = ttk.Frame(page)
            row.pack(fill='x')
            capture = button(row,'CAPTURAR '+truth,lambda value=truth:self.capture_sample(value))
            button(row,'Repetir seleccionada',lambda value=truth:self.retake_sample(value))
            button(row,'Importar imagen',lambda value=truth:self.import_sample(value))
            button(row,'Eliminar',lambda value=truth:self.remove_sample(value))
            split = ttk.Panedwindow(page,orient='horizontal')
            split.pack(fill='both',expand=True)
            left = ttk.Frame(split)
            split.add(left,weight=1)
            thumbnails = ScrollFrame(left)
            thumbnails.pack(fill='both',expand=True)
            grid = thumbnails.inner
            nav = ttk.Frame(left)
            nav.pack(side='bottom',fill='x',before=thumbnails)
            previous = button(nav,'‹',lambda value=truth:self.sample_page(value,-1))
            next_button = button(nav,'›',lambda value=truth:self.sample_page(value,1))
            info = ttk.Label(nav,text='Sin fotografías')
            info.pack(side='left',padx=4)
            self.sample_galleries[truth] = dict(page=page,grid=grid,offset=0,images=[],scroller=thumbnails,
                info=info,previous=previous,next=next_button,capture=capture)
            details = ttk.Frame(split)
            split.add(details,weight=1)
            self._build_photo_details(details,truth)
        validation_scroll = ScrollFrame(self.editor_tabs)
        validation = validation_scroll.inner
        self.editor_tabs.add(validation_scroll,text='Validación')
        ttk.Label(validation,text='Identifica cada pieza física. Varias fotografías de la misma pieza cuentan como una sola.',
                  wraplength=650).pack(fill='x',pady=4)
        self.samples_table = table(validation,[('piece','Pieza',120),('truth','Real',55),
                                  ('defect','Tipo de defecto',140),('note','Notas',180)],height=5)
        self.samples_table.bind('<Double-1>',lambda e:self.edit_sample())
        row = ttk.Frame(validation)
        row.pack(fill='x',pady=4)
        button(row,'Ver fotografía',self.view_sample)
        button(row,'Editar detalles',self.edit_sample)
        row = ttk.Frame(validation)
        row.pack(fill='x')
        button(row,'Ejecutar pruebas guardadas',self.run_validation)
        button(row,'Habilitar versión para inspección',self.approve_model)
        self.validation_info = ttk.Label(validation,text='Sin pruebas de esta versión.',wraplength=650,justify='left',padding=8)
        self.validation_info.pack(fill='x')
        ttk.Label(validation,text='La habilitación requiere 10 OK y 10 NG distintas, sin errores ni muestras sin evaluación. '
                  'Estas pruebas iniciales no garantizan precisión futura.',wraplength=650,style='Muted.TLabel').pack(fill='x',pady=8)

    def _build_photo_details(self,parent,key):
        box = ttk.LabelFrame(parent,text='Detalles de la fotografía seleccionada',padding=6)
        box.pack(fill='both',expand=True,padx=3,pady=3)
        variables = {name:tk.StringVar() for name in ('piece_id','defect_type','note')}
        title = ttk.Label(box,text='Selecciona una miniatura.',wraplength=290)
        title.grid(row=0,column=0,columnspan=2,sticky='w')
        ttk.Label(box,text='ID de pieza:').grid(row=1,column=0,sticky='w',pady=3)
        piece = ttk.Entry(box,textvariable=variables['piece_id'],width=14)
        piece.grid(row=1,column=1,sticky='ew',padx=4)
        ttk.Label(box,text='Defecto:' if key == 'NG' else 'Detalle:').grid(row=2,column=0,sticky='w')
        defect = ttk.Combobox(box,textvariable=variables['defect_type'],width=18,
                  values=['Faltante','Mal colocado','Invertido','Dañado','Incorrecto','Rayadura'] if key == 'NG' else ['Conforme'])
        defect.grid(row=2,column=1,sticky='ew',padx=4,pady=3)
        ttk.Label(box,text='Notas:').grid(row=3,column=0,sticky='w')
        note = ttk.Entry(box,textvariable=variables['note'])
        note.grid(row=3,column=1,sticky='ew',padx=4,pady=3)
        row = ttk.Frame(box)
        row.grid(row=4,column=0,columnspan=2,sticky='ew')
        button(row,'Aplicar detalles' if key == 'reference' else 'Aplicar y confirmar pieza',
               lambda value=key:self.apply_photo_details(value))
        button(row,'Ampliar',lambda value=key:self.view_photo(value))
        ttk.Label(box,text='Misma pieza física = mismo ID.',style='Muted.TLabel').grid(row=5,column=0,columnspan=2,sticky='w')
        box.columnconfigure(1,weight=1)
        self.photo_fields[key] = dict(variables=variables,title=title,widgets=[piece,defect,note])
        for var in variables.values():
            var.trace_add('write',lambda *args,value=key:self._photo_edited(value))
        self._fill_photo_details(key,None)

    def _photo_entry(self,key):
        if self.draft is None:
            return None
        target = self.photo_targets.get(key)
        if target is None:
            return None
        if key == 'reference':
            return self.draft['references'][target]
        return next((e for e in self.draft['samples'] if e['id'] == target and e['truth'] == key),None)

    def _photo_edited(self,key):
        if not self._photo_loading and self.photo_targets.get(key) is not None:
            self._mark_dirty()

    def _fill_photo_details(self,key,entry,target=None):
        panel = self.photo_fields[key]
        self._photo_loading = True
        try:
            self.photo_targets[key] = target if entry else None
            values = dict(piece_id=entry.get('piece_id','') if entry else '',
                          defect_type=entry.get('defect_type','') if entry else '',
                          note=entry.get('note','') if entry else '')
            if key == 'reference' and entry and not values['piece_id']:
                values['piece_id'] = f'REF-{int(target)+1:02d}'
            for name,var in panel['variables'].items():
                var.set(values[name])
            self.photo_baselines[key] = values
            for w in panel['widgets']:
                w.state(['!disabled'] if entry else ['disabled'])
            caption = ('Referencia OK '+str(int(target)+1)) if key == 'reference' and entry else (
                      f"Foto {key} · {entry['piece_id']}" if entry else 'Selecciona una miniatura o captura una fotografía.')
            if entry and key != 'reference' and not entry.get('identity_confirmed',True):
                caption += ' · confirmar ID'
            panel['title'].configure(text=caption)
        finally:
            self._photo_loading = False

    def _flush_photo_details(self,key=None):
        if self.loading or not self.admin_mode or self.draft is None:
            return False
        changed = False
        for name in ([key] if key else self.photo_fields):
            entry = self._photo_entry(name)
            if entry is None:
                continue
            values = {k:v.get().strip() for k,v in self.photo_fields[name]['variables'].items()}
            if values == self.photo_baselines.get(name):
                continue
            if not values['piece_id']:
                raise ValueError('Escribe el identificador de pieza de la foto seleccionada.')
            check = dict(entry,**values)
            asset_name(check)
            if name != 'reference' and values['piece_id'] != entry['piece_id']:
                entry['identity_confirmed'] = False
            entry.update(values)
            self.photo_baselines[name] = values
            changed = True
        if changed:
            self._mark_dirty()
        return changed

    @admin_action
    def apply_photo_details(self,key):
        if self._photo_entry(key) is None:
            raise ValueError('Selecciona primero una fotografía.')
        self._flush_photo_details()
        if key != 'reference':
            self._photo_entry(key)['identity_confirmed'] = True
            self._mark_dirty()
        self._refresh_gallery()
        self._refresh_samples()
        self.message.set('Detalles aplicados al borrador. Pulsa Guardar configuración para conservarlos por número de parte.')

    def view_photo(self,key):
        self._flush_photo_details(key)
        entry = self._photo_entry(key)
        if entry is None:
            raise ValueError('Selecciona primero una miniatura.')
        title = f"{entry.get('piece_id','Referencia OK')} · {entry.get('defect_type','')} · {entry.get('note','')} · {entry.get('captured_at','')}"
        self._show_image(self.store.image(self.model['id'],entry),title)

    def _select_sample(self,sid):
        self._flush_photo_details()
        self._need_model()
        entry = next(e for e in self.draft['samples'] if e['id'] == sid)
        truth = entry['truth']
        entries = [e for e in self.draft['samples'] if e['truth'] == truth]
        index = next(i for i,e in enumerate(entries) if e['id'] == sid)
        self.sample_galleries[truth]['offset'] = (index//8)*8
        self.selected_sample_ids[truth] = sid
        self._render_sample_gallery(truth)
        self._fill_photo_details(truth,entry,sid)
        if self.samples_table.exists(sid):
            self.samples_table.selection_set(sid)

    def sample_page(self,truth,delta):
        self._flush_photo_details()
        gallery = self.sample_galleries[truth]
        count = sum(e['truth'] == truth for e in (self.draft or {}).get('samples',[]))
        gallery['offset'] = max(0,min(max(0,(count-1)//8)*8,gallery['offset']+delta*8))
        self._render_sample_gallery(truth)

    def _render_sample_gallery(self,truth):
        gallery = self.sample_galleries[truth]
        for w in gallery['grid'].winfo_children():
            w.destroy()
        entries = [e for e in (self.draft or {}).get('samples',[]) if e['truth'] == truth]
        count = len(entries)
        offset = gallery['offset'] = min(gallery['offset'],max(0,(count-1)//8)*8)
        gallery['images'] = []
        for i,entry in enumerate(entries[offset:offset+8]):
            selected = entry['id'] == self.selected_sample_ids[truth]
            text = ('[*] ' if selected else '')+entry['piece_id'][:16]
            detail = entry.get('defect_type','') or entry.get('note','')
            if detail:
                text += '\n'+detail[:20]
            photo = ''
            try:
                photo = self._thumbnail(self.store.image(self.model['id'],entry),(105,60))
                gallery['images'].append(photo)
            except (OSError,ValueError):
                text += '\nImagen inválida'
            b = ttk.Button(gallery['grid'],text=text,image=photo,compound='top',
                           command=lambda sid=entry['id']:self._select_sample(sid))
            b.grid(row=i//2,column=i%2,sticky='nsew',padx=3,pady=3)
            b.bind('<Double-1>',lambda event,key=truth:self.view_photo(key))
            for event in ('<MouseWheel>','<Button-4>','<Button-5>'):
                b.bind(event,gallery['scroller'].scroll_callback)
        for i in range(2):
            gallery['grid'].columnconfigure(i,weight=1)
        pages = max(1,(count+7)//8)
        gallery['info'].configure(text=f'{count} fotos {truth} · página {offset//8+1} de {pages}')
        gallery['previous'].state(['!disabled'] if offset else ['disabled'])
        gallery['next'].state(['!disabled'] if offset+8 < count else ['disabled'])

    def _callback_error(self, exc, value, tb):
        traceback.print_exception(exc,value,tb)
        messagebox.showerror('No se completó la operación',str(value),parent=self.root)

    def _submit(self, work, done, text):
        if self.task is not None:
            raise RuntimeError('Hay una operación en curso; espera a que termine.')
        self.message.set(text)
        self.task = (self.executor.submit(work),done)
        self._set_admin_editable(False)

    def _set_admin_editable(self, enabled):
        def walk(parent):
            for w in parent.winfo_children():
                if isinstance(w, (ttk.Button, ttk.Entry, ttk.Combobox, ttk.Spinbox, ttk.Checkbutton)):
                    w.state(['!disabled'] if enabled else ['disabled'])
                walk(w)
        walk(self.admin)
        if enabled:
            for key,panel in self.photo_fields.items():
                if self.photo_targets.get(key) is None:
                    for w in panel['widgets']:
                        w.state(['disabled'])

    def _need_model(self):
        if self.model is None or self.draft is None:
            raise ValueError('Abre o crea primero un número de parte.')

    def _mark_dirty(self,*args):
        if self.loading or not self.admin_mode or self.model is None:
            return
        self.dirty = True
        self.dirty_label.configure(text='Cambios sin guardar')

    def _sync(self):
        self._need_model()
        self._flush_photo_details()
        c = copy.deepcopy(self.draft)
        try:
            for key,var in self.fields.items():
                text = var.get().strip()
                if key.startswith('camera.'):
                    sub = key.split('.')[1]
                    c['camera'][sub] = (float(text) if text else None) if sub in ('exposure','focus','white_balance') else int(text)
                else:
                    c[key] = float(text) if key == 'absence_seconds' else int(text)
            c['normalize_color'] = self.normalize_color.get()
            for key,(name,factor,_) in self.zone_fields.items():
                c['zones'][key]['name'] = name.get().strip()
                c['zones'][key]['factor'] = int(factor.get())
        except ValueError:
            raise ValueError('Revisa los campos numéricos de cámara, tolerancia y zonas.') from None
        old_cam = self.draft['camera']
        if (c['camera']['width'],c['camera']['height']) != (old_cam['width'],old_cam['height']):
            if (c['roi'] or any(c['references']) or c['samples']) and not messagebox.askyesno(
                    'Cambio de resolución',
                    'Cambiar la resolución vaciará región, zonas, referencias y muestras del borrador. '
                    'La versión guardada se conserva hasta guardar estos cambios. ¿Continuar?',parent=self.root):
                self.loading = True
                self.fields['camera.width'].set(str(old_cam['width']))
                self.fields['camera.height'].set(str(old_cam['height']))
                self.loading = False
                raise ValueError('Cambio de resolución cancelado.')
            c['roi'] = c['base_image'] = None
            c['references'],c['samples'] = [None]*10,[]
            for z in c['zones'].values():
                z['rect'] = None
        validate_config(c)
        self.draft = c
        if (c['camera']['width'],c['camera']['height']) != (old_cam['width'],old_cam['height']):
            self._refresh_gallery()
            self._refresh_samples()
            for _,_,state in self.zone_fields.values():
                state.configure(text='Sin dibujar')
        return c

    def _load_editor(self,m):
        self.loading = True
        try:
            same_model = self.model is not None and self.model['id'] == m['id']
            self.photo_targets.clear()
            self.photo_baselines.clear()
            if not same_model:
                self.selected_sample_ids = {'OK':None,'NG':None}
            self.model = copy.deepcopy(m)
            self.draft = c = copy.deepcopy(m['config'])
            for key,var in self.fields.items():
                value = c['camera'].get(key.split('.')[1]) if key.startswith('camera.') else c[key]
                var.set('' if value is None else str(value))
            self.normalize_color.set(c['normalize_color'])
            for key,(name,factor,state) in self.zone_fields.items():
                zone = c['zones'].get(key,defaults()['zones'][key])
                name.set(zone['name'])
                factor.set(str(zone['factor']))
                state.configure(text='Dibujada' if zone['rect'] else 'Sin dibujar')
            self.model_title.configure(text=f"{m['part']} · v{m['version']} · {self.store.status(m)}")
            self.dirty = False
            self.dirty_label.configure(text='Configuración guardada')
            if not same_model:
                self.gallery_slot = next((i for i,e in enumerate(c['references']) if not e),0)
            self._refresh_gallery()
            self._refresh_samples()
            self._refresh_validation()
        finally:
            self.loading = False
        self._configure_preview(c['camera'])

    def _resolve_dirty(self):
        if not self.dirty:
            return True
        answer = messagebox.askyesnocancel('Cambios pendientes','¿Guardar los cambios de este modelo?\nSí: guardar. No: descartar. Cancelar: permanecer aquí.',parent=self.root)
        if answer is None:
            return False
        if answer:
            self.save_model()
        else:
            self._load_editor(self.store.read(self.model['id']))
        return True

    def toggle_admin(self):
        if self.task is not None:
            if self.running:
                self.pause()
                def wait_for_frame():
                    if self.closed:
                        return
                    if self.task is None:
                        self.toggle_admin()
                    else:
                        self.root.after(100,wait_for_frame)
                self.root.after(100,wait_for_frame)
                return
            raise RuntimeError('Espera a que termine la operación en curso.')
        if self.admin_mode:
            if not self._resolve_dirty():
                return
            self.admin_mode = False
            self.admin.pack_forget()
            self.ops.pack(fill='both',expand=True)
            self.mode_label.configure(text='INSPECCIÓN')
            self.access_button.configure(text='Administración')
            self._refresh_catalog()
            self.message.set('Administración bloqueada. Selecciona un modelo disponible para inspección.')
            return
        password = simpledialog.askstring('Acceso a Administración',
                    'Contraseña:' if self.auth.configured else 'Crea una contraseña administrativa (mínimo 8 caracteres):',
                    show='*',parent=self.root)
        if password is None:
            return
        if self.auth.configured:
            if not self.auth.verify(password):
                raise ValueError('Contraseña incorrecta.')
        else:
            again = simpledialog.askstring('Confirmar contraseña','Repite la contraseña:',show='*',parent=self.root)
            if again != password:
                raise ValueError('Las contraseñas no coinciden.')
            self.auth.set_password(password)
        self.pause()
        self.admin_mode = True
        self.ops.pack_forget()
        self.admin.pack(fill='both',expand=True)
        self.mode_label.configure(text='ADMINISTRACIÓN')
        self.access_button.configure(text='Salir y bloquear Administración')
        self.message.set('Los ajustes de prueba no se guardan automáticamente.')
        self._refresh_catalog()

    @admin_action
    def change_password(self):
        current = simpledialog.askstring('Cambiar contraseña','Contraseña actual:',show='*',parent=self.root)
        if current is None:
            return
        if not self.auth.verify(current):
            raise ValueError('Contraseña actual incorrecta.')
        new = simpledialog.askstring('Cambiar contraseña','Nueva contraseña (mínimo 8 caracteres):',show='*',parent=self.root)
        if new is None:
            return
        again = simpledialog.askstring('Cambiar contraseña','Repite la nueva contraseña:',show='*',parent=self.root)
        if new != again:
            raise ValueError('Las contraseñas no coinciden.')
        self.auth.set_password(new)
        self.message.set('Contraseña actualizada.')

    def _refresh_catalog(self):
        rows = self.store.list()
        self.op_models = {m['part']:m for m in rows if self.store.status(m) == READY}
        self.op_combo['values'] = list(self.op_models)
        if self.op_part.get() and self.op_part.get() not in self.op_models:
            self.op_part.set('')
            self.active_model = self.engine = self.cycle = None
            self._clear_result('Sin modelo disponible')
            self.op_description.configure(text='Sin modelo seleccionado')
            self.ref_preview.configure(image='',text='Sin referencia')
        self.catalog.delete(*self.catalog.get_children())
        query = self.search.get().casefold()
        for m in rows:
            if m['archived'] and not self.archived.get():
                continue
            if query and query not in (m['part']+' '+m['description']).casefold():
                continue
            c = m['config']
            n = sum(bool(e) for e in c['references'])
            self.catalog.insert('', 'end',iid=m['id'],values=(m['part'],m['description'],n,c['zone_count'],
                                self.store.status(m),m['version'],m['saved_at'][:19].replace('T',' ')))
        if self.store.catalog_errors:
            self.message.set('Hay modelos ilegibles. Consulta “Errores del catálogo”; los originales se conservan.')

    def _selected_catalog(self):
        selected = self.catalog.selection()
        if not selected:
            raise ValueError('Selecciona un número de parte en el catálogo.')
        return self.store.read(selected[0])

    def _catalog_thumbnail(self):
        selected = self.catalog.selection()
        if not selected:
            return
        m = self.store.read(selected[0])
        entry = next((e for e in m['config']['references'] if e),None)
        if entry:
            image = self._thumbnail(self.store.image(m['id'],entry),(180,100))
            self.catalog_photo.configure(image=image,text=m['part'],compound='left')
            self.catalog_photo.image = image
        else:
            self.catalog_photo.configure(image='',text='Modelo sin referencia.')

    @admin_action
    def show_catalog_errors(self):
        messagebox.showinfo('Estado del catálogo','\n'.join(self.store.catalog_errors) or 'Todos los modelos se pudieron leer.',parent=self.root)

    @admin_action
    def new_model(self):
        if not self._resolve_dirty():
            return
        part = simpledialog.askstring('Nuevo número de parte','Número de parte:',parent=self.root)
        if part is None:
            return
        description = simpledialog.askstring('Descripción','Descripción (opcional):',parent=self.root)
        m = self.store.create(part,description or '')
        self._load_editor(m)
        self._refresh_catalog()
        self.admin_tabs.select(self.editor)

    @admin_action
    def open_model(self):
        if not self._resolve_dirty():
            return
        self._load_editor(self._selected_catalog())
        self.admin_tabs.select(self.editor)
        self.message.set('Configuración cargada. Abre la cámara para capturar o probar.')

    @admin_action
    def save_model(self):
        c = self._sync()
        m = self.store.save(self.model['id'],c)
        self._load_editor(m)
        self._refresh_catalog()
        self.message.set(f"Configuración de {m['part']} guardada en versión {m['version']}. La validación corresponde a esa versión.")

    @admin_action
    def discard_changes(self):
        self._need_model()
        if self.dirty and not messagebox.askyesno('Restaurar','¿Descartar los ajustes de prueba y cargar la configuración guardada?',parent=self.root):
            return
        self._load_editor(self.store.read(self.model['id']))
        self.message.set('Configuración guardada restaurada.')

    @admin_action
    def restore_previous(self):
        self._need_model()
        if not self._resolve_dirty():
            return
        if not messagebox.askyesno('Recuperar versión','Se recuperarán los ajustes anteriores como una nueva versión pendiente de validar. ¿Continuar?',parent=self.root):
            return
        self._load_editor(self.store.restore_previous(self.model['id']))
        self._refresh_catalog()

    @admin_action
    def rename_model(self):
        if not self._resolve_dirty():
            return
        m = self._selected_catalog()
        part = simpledialog.askstring('Número de parte','Número de parte:',initialvalue=m['part'],parent=self.root)
        if part is None:
            return
        desc = simpledialog.askstring('Descripción','Descripción:',initialvalue=m['description'],parent=self.root)
        if desc is None:
            return
        m = self.store.metadata(m['id'],part=part,description=desc)
        if self.model and self.model['id'] == m['id']:
            self._load_editor(m)
        self._refresh_catalog()

    @admin_action
    def duplicate_model(self):
        if not self._resolve_dirty():
            return
        m = self._selected_catalog()
        part = simpledialog.askstring('Duplicar','Número de parte del nuevo modelo:',initialvalue=m['part']+'-copia',parent=self.root)
        if part is None:
            return
        self._load_editor(self.store.duplicate(m['id'],part))
        self._refresh_catalog()
        self.admin_tabs.select(self.editor)

    @admin_action
    def archive_model(self):
        if not self._resolve_dirty():
            return
        m = self._selected_catalog()
        if not messagebox.askyesno('Archivar / restaurar',f"¿{'Restaurar' if m['archived'] else 'Archivar'} {m['part']}? Sus datos se conservarán.",parent=self.root):
            return
        m = self.store.metadata(m['id'],archived=not m['archived'])
        if self.model and self.model['id'] == m['id']:
            self._load_editor(m)
        self._refresh_catalog()

    @admin_action
    def export_model(self):
        if not self._resolve_dirty():
            return
        m = self._selected_catalog()
        path = filedialog.asksaveasfilename(parent=self.root,title='Exportar modelo completo',defaultextension='.zip',filetypes=[('Modelo Poka-Yoke','*.zip')])
        if path:
            self._submit(lambda:self.store.export_model(m['id'],path),lambda _:self.message.set('Modelo exportado con imágenes y configuración.'),'Exportando modelo…')

    @admin_action
    def import_model(self):
        if not self._resolve_dirty():
            return
        path = filedialog.askopenfilename(parent=self.root,title='Importar modelo',filetypes=[('Modelo Poka-Yoke','*.zip')])
        if not path:
            return
        part = simpledialog.askstring('Importar como nuevo modelo','Número de parte para la copia importada:',parent=self.root)
        if not part:
            return
        def done(m):
            self._load_editor(m)
            self._refresh_catalog()
            self.admin_tabs.select(self.editor)
            self.message.set('Modelo importado como copia nueva. Revisa y valida antes de inspeccionar.')
        self._submit(lambda:self.store.import_model(path,part),done,'Validando e importando paquete…')

    @admin_action
    def import_legacy(self):
        if not self._resolve_dirty():
            return
        directory = filedialog.askdirectory(parent=self.root,title='Selecciona modelos_referencia de la versión anterior')
        if not directory:
            return
        names = sorted({p.name.rsplit('_roi.npy',1)[0] for p in Path(directory).glob('*_roi.npy')})
        if not names:
            raise ValueError('No se encontraron archivos *_roi.npy en esa carpeta.')
        dialog = tk.Toplevel(self.root)
        dialog.title('Importar modelo v3/v4')
        dialog.transient(self.root)
        dialog.grab_set()
        box = ttk.Frame(dialog,padding=15)
        box.pack(fill='both',expand=True)
        ttk.Label(box,text='Modelo anterior:').pack(anchor='w')
        selected = tk.StringVar(value=names[0])
        ttk.Combobox(box,textvariable=selected,values=names,state='readonly',width=40).pack(fill='x',pady=8)
        ttk.Label(box,text='Resolución original si falta la foto completa:').pack(anchor='w')
        resolution = tk.StringVar(value='1280x720')
        ttk.Combobox(box,textvariable=resolution,values=['640x480','1280x720','1920x1080','2560x1440','2592x1944'],width=40).pack(fill='x',pady=8)
        ttk.Label(box,text='Los archivos originales se conservan. La copia quedará pendiente de validar.',wraplength=400).pack(fill='x',pady=8)
        def perform():
            if not self.admin_mode:
                return
            name = selected.get()
            part = simpledialog.askstring('Importar como','Número de parte en el catálogo nuevo:',initialvalue=name,parent=dialog)
            if not part:
                return
            try:
                w,h = [int(v) for v in resolution.get().lower().split('x')]
            except ValueError:
                raise ValueError('Resolución inválida. Usa el formato 1280x720.') from None
            dialog.destroy()
            def done(m):
                self._load_editor(m)
                self._refresh_catalog()
                self.admin_tabs.select(self.editor)
                self.message.set('Modelo anterior importado. Los originales no fueron modificados.')
            self._submit(lambda:self.store.import_legacy(directory,name,part,(w,h)),done,'Importando copia del modelo anterior…')
        ttk.Button(box,text='Importar copia',command=perform).pack(pady=8)

    @admin_action
    def open_camera(self):
        c = self._sync()
        self._configure_preview(c['camera'],force=True)
        self.message.set('Abriendo cámara. Comprueba la resolución y los ajustes reales en el estado inferior.')

    def _configure_preview(self,settings,force=False):
        if self.closed:
            return
        _,_,info = self.camera.snapshot()
        if (not force and getattr(self,'camera_requested',None) == settings
                and (info.get('ready') or 'Abriendo' in info.get('status',''))):
            return
        self.generation = self.camera.configure(settings)
        self.camera_requested = copy.deepcopy(settings)
        self.camera_status.set('Abriendo cámara…')

    def reconnect_camera(self):
        if self.closed:
            return
        if self.running or getattr(self,'pending_start',False):
            self.pause()
        if self.admin_mode and self.model is not None:
            # Reutiliza la validación y los ajustes del editor sin guardarlos.
            if self.task is not None:
                raise ValueError('Espera la operación en curso antes de reconectar la cámara.')
            settings = self._sync()['camera']
        elif self.active_model is not None:
            settings = self.active_model['config']['camera']
        else:
            settings = defaults()['camera']
        if self.cycle is not None:
            self.executor.submit(self.cycle.disconnected)
        self._configure_preview(settings,force=True)
        self.message.set('Abriendo vista previa. La inspección permanece pausada; comprueba la imagen antes de iniciar.')

    @admin_action
    def use_actual_resolution(self):
        self._need_model()
        frame,_,info = self.camera.snapshot()
        if frame is None or not info.get('ready'):
            raise ValueError('Abre primero la cámara y espera una imagen válida.')
        h,w = frame.shape[:2]
        self.fields['camera.width'].set(str(w))
        self.fields['camera.height'].set(str(h))
        self.open_camera()

    def _fresh_frame(self,c):
        frame,stamp,info = self.camera.snapshot()
        if frame is None or not info.get('ready') or time.monotonic()-stamp > 1.:
            raise ValueError('No hay una imagen reciente de cámara. Abre o reconecta la cámara.')
        if not info.get('compatible') or getattr(self,'camera_requested',None) != c['camera']:
            raise ValueError('Abre la cámara con estos ajustes y verifica que sean compatibles antes de capturar.')
        if frame.shape[:2] != (c['camera']['height'],c['camera']['width']):
            raise ValueError('La resolución real no coincide con este modelo.')
        return frame

    @admin_action
    def camera_details(self):
        _,_,info = self.camera.snapshot()
        labels = {'exposure':'Exposición','focus':'Enfoque','white_balance':'Temperatura de blanco'}
        lines = [info['status']]
        for key, values in info.get('properties',{}).items():
            requested = values.get('requested')
            verdict = ('comprobado' if values.get('verified') else 'no comprobado') if requested is not None else 'sin ajuste solicitado'
            lines.append(f"{labels.get(key,key)}: solicitado {requested if requested is not None else '—'}; lectura {values.get('actual')}; {verdict}.")
        lines.append('La disponibilidad y las unidades dependen del controlador de la cámara.')
        messagebox.showinfo('Detalles de cámara','\n\n'.join(lines),parent=self.root)

    @admin_action
    def edit_roi(self):
        c = self._sync()
        frame = self._fresh_frame(c)
        dlg = RegionDialog(self.root,frame,'Región general de la pieza',initial=c['roi'],minimum=24)
        self.root.wait_window(dlg)
        if dlg.result is None or dlg.result == c['roi']:
            return
        if any(c['references']) and not messagebox.askyesno('Cambio de región','Cambiar la región vacía las referencias OK del borrador. La versión guardada permanece recuperable. ¿Continuar?',parent=self.root):
            return
        c['roi'] = dlg.result
        c['references'] = [None]*10
        c['base_image'] = self.store.put_image(self.model['id'],frame)
        for zone in c['zones'].values():
            zone['rect'] = None
        self.draft = c
        self._mark_dirty()
        self._refresh_gallery()
        for _,_,label in self.zone_fields.values():
            label.configure(text='Sin dibujar')
        self.message.set('Región de prueba configurada. Captura las 10 referencias y dibuja las zonas.')

    def _reference_image(self,index=None):
        self._need_model()
        entries = self.draft['references']
        entry = entries[index] if index is not None else next((e for e in entries if e),None)
        if entry is None:
            raise ValueError('Primero captura o importa una referencia OK.')
        return self.store.image(self.model['id'],entry)

    @admin_action
    def edit_zone(self,index):
        c = self._sync()
        if index > c['zone_count']:
            raise ValueError('Esta zona está desactivada. Cambia primero la cantidad de zonas.')
        image = self._reference_image()
        h,w = image.shape[:2]
        zone = c['zones'][str(index)]
        initial = [int(v*(w if i%2==0 else h)) for i,v in enumerate(zone['rect'])] if zone['rect'] else None
        dlg = RegionDialog(self.root,image,f"Dibujar zona: {zone['name']}",initial=initial)
        self.root.wait_window(dlg)
        if dlg.result:
            zone['rect'] = [v/(w if i%2==0 else h) for i,v in enumerate(dlg.result)]
            self.draft = c
            self.zone_fields[str(index)][2].configure(text='Dibujada')
            self._mark_dirty()

    def _thumbnail(self,image,size=(110,75)):
        im = Image.fromarray(cv2.cvtColor(image,cv2.COLOR_BGR2RGB))
        im.thumbnail(size,Image.Resampling.LANCZOS)
        return ImageTk.PhotoImage(im)

    def _refresh_gallery(self):
        if self.draft is None:
            return
        self.thumbnails = []
        count = 0
        for i,entry in enumerate(self.draft['references']):
            b = self.ref_buttons[i]
            photo = ''
            text = f'OK {i+1}' + (' [*]' if i == self.gallery_slot else '')
            if entry:
                try:
                    photo = self._thumbnail(self.store.image(self.model['id'],entry),(80,45))
                    self.thumbnails.append(photo)
                    count += 1
                    if entry.get('piece_id'):
                        text += '\n'+entry['piece_id'][:16]
                except Exception:
                    text += '\nImagen inválida'
            else:
                text += '\nSin imagen'
            b.configure(image=photo,text=text,compound='top')
        self.gallery_info.configure(text=f'{count} / 10 referencias legibles · seleccionada: {self.gallery_slot+1} · alineación pendiente de comprobar')
        self.roi_label.configure(text=f"Región general: {self.draft['roi'] or 'sin configurar'}")
        self._fill_photo_details('reference',self.draft['references'][self.gallery_slot],self.gallery_slot)

    def select_reference(self,index):
        self._flush_photo_details()
        self.gallery_slot = index
        self._refresh_gallery()

    def _show_image(self,image,title):
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.geometry('960x670')
        canvas = ImageCanvas(dialog)
        canvas.pack(fill='both',expand=True)
        canvas.display(image)
        ttk.Button(dialog,text='Cerrar',command=dialog.destroy).pack(pady=8)
        return dialog

    def view_reference(self,index):
        self._flush_photo_details('reference')
        self._show_image(self._reference_image(index),f'Referencia OK {index+1}')

    def _replace_reference(self,image,*,base_frame=None):
        c = self._sync()
        if c['roi'] is None:
            raise ValueError('Dibuja primero la región general de la pieza en Cámara y región.')
        x1,y1,x2,y2 = c['roi']
        if image.shape[:2] == (c['camera']['height'],c['camera']['width']):
            base_frame = image
            image = image[y1:y2,x1:x2].copy()
        if image.shape[:2] != (y2-y1,x2-x1):
            raise ValueError(f'La imagen debe ser un cuadro completo o un recorte de {x2-x1} × {y2-y1}.')
        if c['references'][self.gallery_slot] and not messagebox.askyesno('Reemplazar referencia',f'¿Reemplazar la referencia OK {self.gallery_slot+1} del borrador?',parent=self.root):
            return
        old = c['references'][self.gallery_slot] or {}
        entry = self.store.put_image(self.model['id'],image)
        entry.update(piece_id=old.get('piece_id',f'REF-{self.gallery_slot+1:02d}'),
                     defect_type=old.get('defect_type','Conforme'),note=old.get('note',''),captured_at=now())
        c['references'][self.gallery_slot] = entry
        if base_frame is not None and (self.gallery_slot == 0 or not c['base_image']):
            c['base_image'] = self.store.put_image(self.model['id'],base_frame)
        self.draft = c
        self._mark_dirty()
        selected = self.gallery_slot
        self._refresh_gallery()
        self.message.set(f'Referencia OK {selected+1} preparada. Guarda cuando termines los cambios.')

    @admin_action
    def capture_reference(self):
        c = self._sync()
        if c['roi'] is None:
            raise ValueError('Dibuja primero la región general de la pieza.')
        empty = next((i for i,e in enumerate(c['references']) if not e),None)
        if empty is None:
            raise ValueError('Las 10 referencias OK están completas. Usa Repetir seleccionada para reemplazar una.')
        image = self._fresh_frame(c)
        self.preview_hold_until = 0
        if c['references'][self.gallery_slot]:
            self.gallery_slot = empty
        self._replace_reference(image)

    @admin_action
    def retake_reference(self):
        c = self._sync()
        if not c['references'][self.gallery_slot]:
            raise ValueError('Selecciona una referencia existente para repetirla.')
        self._replace_reference(self._fresh_frame(c))

    @admin_action
    def import_reference(self):
        self._need_model()
        path = filedialog.askopenfilename(parent=self.root,title='Importar referencia OK',filetypes=[('Imágenes','*.png *.jpg *.jpeg *.bmp *.tif *.tiff')])
        if path:
            self._replace_reference(load_image(path))

    @admin_action
    def remove_reference(self):
        self._need_model()
        if not self.draft['references'][self.gallery_slot]:
            return
        if messagebox.askyesno('Eliminar referencia','¿Quitar esta referencia del borrador? La versión guardada sigue disponible.',parent=self.root):
            self.draft['references'][self.gallery_slot] = None
            self._mark_dirty()
            self._refresh_gallery()

    def _sample_labels(self,initial=None):
        initial = initial or {}
        truth = simpledialog.askstring('Condición real','Escribe OK o NG según la condición REAL de la pieza:',initialvalue=initial.get('truth',''),parent=self.root)
        if truth is None:
            return None
        truth = truth.strip().upper()
        if truth not in ('OK','NG'):
            raise ValueError('La condición debe ser OK o NG.')
        piece = simpledialog.askstring('Identificar pieza','Identificador de la pieza física (repite el mismo si es la misma pieza):',initialvalue=initial.get('piece_id',''),parent=self.root)
        if not piece:
            return None
        note = simpledialog.askstring('Defecto o nota','Defecto esperado o nota (opcional):',initialvalue=initial.get('note',''),parent=self.root)
        return truth,piece.strip(),note or ''

    def _next_piece_id(self,truth):
        existing = {s['piece_id'].casefold() for s in self.draft['samples']}
        index = 1
        while f'{truth}-{index:02d}'.casefold() in existing:
            index += 1
        return f'{truth}-{index:02d}'

    def _add_sample(self,image,truth=None):
        c = self._sync()
        if truth is None:
            labels = self._sample_labels()
            if labels is None:
                return
            truth,piece,note = labels
        else:
            if truth not in ('OK','NG'):
                raise ValueError('La sección de captura debe indicar OK o NG.')
            piece,note = self._next_piece_id(truth),''
        cam = c['camera']
        if image.shape[:2] == (cam['height'],cam['width']):
            kind = 'full'
        elif c['roi'] and image.shape[:2] == (c['roi'][3]-c['roi'][1],c['roi'][2]-c['roi'][0]):
            kind = 'crop'
        else:
            raise ValueError('La muestra debe coincidir con la resolución del modelo o con el recorte de su región.')
        e = self.store.put_image(self.model['id'],image)
        if any(s['path'] == e['path'] for s in c['samples']):
            raise ValueError('Esta imagen ya está entre las muestras. No cuenta como otra pieza.')
        if any(r and r['path'] == e['path'] for r in c['references']) or (c['base_image'] and c['base_image']['path'] == e['path']):
            raise ValueError('Usa muestras independientes de las imágenes de referencia.')
        e.update(id=uuid.uuid4().hex,piece_id=piece,truth=truth,kind=kind,note=note,
                 defect_type='Conforme' if truth == 'OK' else '',captured_at=now(),identity_confirmed=False)
        c['samples'].append(e)
        validate_config(c)
        self.draft = c
        self.selected_sample_ids[truth] = e['id']
        count = sum(s['truth'] == truth for s in c['samples'])
        self.sample_galleries[truth]['offset'] = ((count-1)//8)*8
        self._mark_dirty()
        self._refresh_samples()
        self.editor_tabs.select(self.sample_galleries[truth]['page'])
        self.message.set(f'Foto {truth} capturada. Edita sus detalles y confirma el ID de pieza. Reutiliza el ID si es la misma pieza.')
        return e

    @admin_action
    def capture_sample(self,truth=None):
        c = self._sync()
        self.preview_hold_until = 0
        if truth is None:
            truth = 'NG' if self.editor_tabs.select() == str(self.sample_galleries['NG']['page']) else 'OK'
        return self._add_sample(self._fresh_frame(c),truth)

    @admin_action
    def import_sample(self,truth=None):
        self._need_model()
        path = filedialog.askopenfilename(parent=self.root,title=f'Importar muestra {truth or "OK o NG"}',
                    filetypes=[('Imágenes','*.png *.jpg *.jpeg *.bmp *.tif *.tiff')])
        if path:
            return self._add_sample(load_image(path),truth)

    @admin_action
    def retake_sample(self,truth):
        c = self._sync()
        entry = self._selected_sample(truth)
        image = self._fresh_frame(c)
        asset = self.store.put_image(self.model['id'],image)
        if any(s['id'] != entry['id'] and s['path'] == asset['path'] for s in c['samples']):
            raise ValueError('Esta imagen ya pertenece a otra muestra.')
        if any(e and e['path'] == asset['path'] for e in c['references']+[c['base_image']]):
            raise ValueError('Las muestras de prueba deben ser independientes de las referencias.')
        if not messagebox.askyesno('Repetir fotografía',f"¿Reemplazar la foto de {entry['piece_id']} conservando sus detalles?",parent=self.root):
            return
        entry.update(asset,kind='full',captured_at=now())
        self._mark_dirty()
        self._refresh_samples()
        self.message.set('Fotografía repetida; se conservaron su identificador, tipo y notas en el borrador.')

    def _refresh_samples(self):
        selection = self.samples_table.selection()
        self.samples_table.delete(*self.samples_table.get_children())
        if self.draft:
            for e in self.draft['samples']:
                piece = e['piece_id'] + (' · identificar' if not e.get('identity_confirmed',True) else '')
                self.samples_table.insert('','end',iid=e['id'],values=(piece,e['truth'],e.get('defect_type',''),e['note']))
        if selection and self.samples_table.exists(selection[0]):
            self.samples_table.selection_set(selection[0])
        for truth in ('OK','NG'):
            entries = [e for e in (self.draft or {}).get('samples',[]) if e['truth'] == truth]
            sid = self.selected_sample_ids[truth]
            entry = next((e for e in entries if e['id'] == sid),None)
            if entry is None:
                entry = entries[0] if entries else None
                self.selected_sample_ids[truth] = entry['id'] if entry else None
            self._render_sample_gallery(truth)
            self._fill_photo_details(truth,entry,entry['id'] if entry else None)

    def _selected_sample(self,truth=None):
        self._need_model()
        if truth:
            sid = self.selected_sample_ids.get(truth)
        else:
            selected = self.samples_table.selection()
            sid = selected[0] if selected else None
            if sid is None:
                for kind,gallery in self.sample_galleries.items():
                    if self.editor_tabs.select() == str(gallery['page']):
                        sid = self.selected_sample_ids[kind]
        entry = next((e for e in self.draft['samples'] if e['id'] == sid),None)
        if entry is None or (truth and entry['truth'] != truth):
            raise ValueError('Selecciona una miniatura de esta sección.')
        return entry

    def view_sample(self):
        self._flush_photo_details()
        e = self._selected_sample()
        self._show_image(self.store.image(self.model['id'],e),
                         f"{e['piece_id']} · real {e['truth']} · {e.get('defect_type','')} · {e.get('note','')}")

    @admin_action
    def edit_sample(self):
        e = self._selected_sample()
        self._select_sample(e['id'])
        self.editor_tabs.select(self.sample_galleries[e['truth']]['page'])
        self.photo_fields[e['truth']]['widgets'][0].focus_set()

    @admin_action
    def remove_sample(self,truth=None):
        self._flush_photo_details()
        e = self._selected_sample(truth)
        if messagebox.askyesno('Eliminar muestra','¿Quitar esta muestra del borrador?',parent=self.root):
            self.draft['samples'] = [s for s in self.draft['samples'] if s['id'] != e['id']]
            self._mark_dirty()
            self._refresh_samples()

    def _refresh_validation(self):
        if not self.model:
            return
        s = self.store.validation_summary(self.model)
        unidentified = sum(not e.get('identity_confirmed',True) for e in self.model['config']['samples'])
        self.validation_info.configure(text=(
            f"Versión guardada {self.model['version']} · piezas distintas OK: {s['ok_pieces']} / NG: {s['ng_pieces']}\n"
            f"OK aceptadas: {s['ok_accepted']} · OK rechazadas: {s['ok_rejected']}\n"
            f"NG rechazadas: {s['ng_rejected']} · NG aceptadas: {s['ng_accepted']}\n"
            f"Sin evaluación: {s['unevaluated']} · Sin identificar: {unidentified}\nEstado: {self.store.status(self.model)}"))

    @admin_action
    def try_settings(self):
        c = copy.deepcopy(self._sync())
        m = copy.deepcopy(self.model)
        m['config'] = c
        frame = self._fresh_frame(c)
        def work():
            engine = VisionEngine(self.store,m)
            result,annotated,note = engine.evaluate_sample(frame,{'kind':'full','processed':False})
            return engine.aligned_references,result,annotated,note
        def done(value):
            n,result,annotated,note = value
            self.admin_video.display(annotated)
            text = 'SIN EVALUACIÓN' if result is None else ('OK' if result['ok'] else 'NG')
            self.message.set(f'Prueba temporal: {text} · {n}/10 referencias alineadas. {note} No se alteraron ajustes ni contadores de producción.')
            self.preview_hold_until = time.monotonic()+3
        self._submit(work,done,'Comprobando referencias y probando ajustes temporales…')

    @admin_action
    def run_validation(self):
        self._sync()
        if self.dirty or canonical(self.draft) != canonical(self.model['config']):
            raise ValueError('Guarda o descarta los cambios antes de validar una versión.')
        m = self.store.read(self.model['id'])
        if not m['config']['samples']:
            raise ValueError('Agrega muestras OK y NG independientes y guarda primero.')
        def work():
            engine = VisionEngine(self.store,m)
            rows = []
            for entry in m['config']['samples']:
                image = self.store.image(m['id'],entry)
                result,annotated,note = engine.evaluate_sample(image,entry)
                details = ' · '.join(v for v in (entry.get('defect_type',''),entry.get('note','')) if v)
                rows.append(self.store.record(m,result,kind='validation',operator='Administrador',
                    truth=entry['truth'],piece_id=entry['piece_id'],sample_id=entry['id'],image=image,
                    annotated=annotated,note=' | '.join(v for v in (note,details) if v)))
            return rows
        def done(rows):
            self._refresh_validation()
            self.message.set(f'{len(rows)} pruebas terminadas para la versión {m["version"]}. Revisa aciertos, errores y muestras sin evaluación.')
        self._submit(work,done,'Ejecutando pruebas OK/NG sobre fotografías guardadas…')

    @admin_action
    def approve_model(self):
        self._sync()
        if self.dirty or canonical(self.draft) != canonical(self.model['config']):
            raise ValueError('Guarda los cambios y ejecuta sus pruebas antes de habilitar.')
        m = self.store.read(self.model['id'])
        if m['archived']:
            raise ValueError('Restaura el modelo archivado antes de habilitarlo.')
        if not messagebox.askyesno('Habilitar para inspección','¿Confirmas que revisaste las pruebas, los defectos y las condiciones de captura?\nEstas muestras iniciales no garantizan precisión futura.',parent=self.root):
            return
        def work():
            engine = VisionEngine(self.store,m)
            return self.store.approve(m['id'],'Administrador',aligned_references=engine.aligned_references)
        def done(value):
            self._load_editor(value)
            self._refresh_catalog()
            self.message.set('Esta versión está disponible en Inspección. Los cambios posteriores requerirán nueva validación.')
        self._submit(work,done,'Verificando requisitos para habilitar la versión…')

    def _clear_result(self,state):
        self.result_label.configure(text='SIN EVALUACIÓN',foreground='#a6b6cb')
        self.zone_results.configure(text='')
        self.cycle_status.set(state)

    def _select_operation(self):
        if self.task is not None:
            self.op_part.set(self.active_model['part'] if self.active_model else '')
            raise RuntimeError('Espera a que termine la operación en curso.')
        self.pause()
        self.active_model = copy.deepcopy(self.op_models.get(self.op_part.get()))
        self.engine = self.cycle = None
        self.last_event = None
        self._clear_result('Modelo cargado · inspección pausada')
        m = self.active_model
        if m:
            self._configure_preview(m['config']['camera'])
            self.op_description.configure(text=f"{m['part']} · {m['description']} · configuración v{m['version']}")
            entry = next((e for e in m['config']['references'] if e),None)
            if entry:
                photo = self._thumbnail(self.store.image(m['id'],entry),(180,90))
                self.ref_preview.configure(image=photo,text='Referencia OK',compound='top')
                self.ref_preview.image = photo
        else:
            self.op_description.configure(text='Sin modelo seleccionado')
            self.ref_preview.configure(image='',text='Sin referencia')
        self._refresh_counts()
        self._refresh_recent()

    def choose_part(self):
        if self.running or getattr(self,'pending_start',False) or self.task is not None:
            raise ValueError('Pausa y espera la operación en curso antes de cambiar de modelo.')
        self._refresh_catalog()
        if not self.op_models:
            raise ValueError('No hay modelos disponibles. Prepara y valida un modelo en Administración.')
        dialog = tk.Toplevel(self.root)
        dialog.title('Buscar número de parte disponible')
        dialog.geometry('760x450')
        dialog.transient(self.root)
        dialog.grab_set()
        query = tk.StringVar()
        ttk.Label(dialog,text='Buscar por número de parte o descripción:',padding=10).pack(anchor='w')
        entry = ttk.Entry(dialog,textvariable=query)
        entry.pack(fill='x',padx=10,pady=4)
        tree = table(dialog,[('part','Número de parte',200),('description','Descripción',350)],height=8)
        choices = {m['id']:m for m in self.op_models.values()}
        def refresh(*args):
            text = query.get().strip().casefold()
            tree.delete(*tree.get_children())
            for mid,m in choices.items():
                if text in (m['part']+' '+m['description']).casefold():
                    tree.insert('','end',iid=mid,values=(m['part'],m['description']))
        def accept():
            selected = tree.selection()
            if selected:
                self.op_part.set(choices[selected[0]]['part'])
                self._select_operation()
                dialog.destroy()
        query.trace_add('write',refresh)
        tree.bind('<Double-1>',lambda e:accept())
        ttk.Button(dialog,text='Seleccionar modelo',command=accept).pack(pady=10)
        refresh()
        entry.focus_set()

    def _context_enabled(self,enabled):
        for w in self.context_widgets:
            w.configure(state='normal' if enabled else 'disabled')
        self.op_combo.configure(state='readonly' if enabled else 'disabled')
        self.choose_button.configure(state='normal' if enabled else 'disabled')

    def pause(self):
        was_running = self.running or getattr(self,'pending_start',False)
        self.running = self.pending_start = False
        self.start_button.configure(text='Iniciar inspección')
        self._context_enabled(True)
        self._clear_result('Inspección pausada')
        # Interrupt on the engine thread after any in-flight frame; never mutate a cycle concurrently.
        if was_running and self.cycle is not None:
            self.executor.submit(self.cycle.interrupt)

    def start_or_pause(self):
        if self.running or getattr(self,'pending_start',False):
            self.pause()
            self.message.set('Inspección pausada. Al continuar se conservará el bloqueo de una pieza ya evaluada.')
            return
        if self.admin_mode:
            raise ValueError('Sal de Administración para iniciar la inspección de producción.')
        if self.task is not None:
            raise RuntimeError('Espera a que termine la operación en curso.')
        if not self.active_model:
            raise ValueError('Selecciona un número de parte disponible.')
        if not self.operator.get().strip() or not self.lot.get().strip():
            raise ValueError('Introduce operador y lote antes de iniciar.')
        m = self.store.read(self.active_model['id'])
        if self.store.status(m) != READY:
            self._refresh_catalog()
            raise ValueError('Este modelo ya no está disponible. Revisa su validación en Administración.')
        self.active_model = m
        self.production_context = dict(operator=self.operator.get().strip(),lot=self.lot.get().strip(),work_order=self.order.get().strip())
        if getattr(self,'camera_requested',None) != m['config']['camera'] or not self.camera.snapshot()[2].get('ready'):
            self.generation = self.camera.configure(m['config']['camera'])
            self.camera_requested = copy.deepcopy(m['config']['camera'])
        self.pending_start = True
        self._context_enabled(False)
        self.start_button.configure(text='Cancelar inicio')
        if self.engine and self.engine.model['id'] == m['id'] and self.engine.model['version'] == m['version']:
            self.message.set('Esperando imagen de cámara para continuar…')
            return
        def done(engine):
            self.engine = engine
            self.cycle = InspectionCycle(engine,require_clear=True)
            self.message.set('Modelo comprobado. Esperando cámara; retira la pieza para armar el primer ciclo.')
        self._submit(lambda:VisionEngine(self.store,m),done,'Cargando modelo y comprobando 10 referencias OK…')

    def _refresh_counts(self):
        if not hasattr(self,'counts'):
            return
        ok,ng = self.store.counts(self.active_model['id'],self.lot.get()) if self.active_model else (0,0)
        total = ok+ng
        self.counts.set(f'Total: {total}   |   OK: {ok}   |   NG: {ng}   |   NG: {100*ng/total if total else 0:.1f} %')

    def _refresh_recent(self):
        self.recent.delete(*self.recent.get_children())
        if not self.active_model:
            return
        for r in self.store.events(model_id=self.active_model['id'],lot=self.lot.get() or None,kind='production',limit=20):
            self.recent.insert('','end',iid=r['id'],values=(r['timestamp'][11:19],r['part'],r['lot'],r['outcome'],r['version']))

    def _display_output(self,out):
        if not self.running:
            self._refresh_counts()
            self._refresh_recent()
            return
        self.video.display(out['annotated'])
        self.cycle_status.set(out['state']+f" · coincidencia {out['match']:.2f}")
        result = out['result']
        if result:
            self.result_label.configure(text='OK' if result['ok'] else 'NG',foreground='#35e0b5' if result['ok'] else '#ff6b7b')
            lines = [f"{z['name']}: {'OK' if z['ok'] else 'DIFERENCIA'} ({z['pix']}/{z['lim']})" for z in result['zonas']]
            if not result['ok'] and all(z['ok'] for z in result['zonas']):
                lines.append('Coincidencia global insuficiente.')
            self.zone_results.configure(text='\n\n'.join(lines))
        else:
            self.result_label.configure(text='SIN EVALUACIÓN',foreground='#a6b6cb')
            self.zone_results.configure(text='')
        if out.get('record'):
            self.last_event = out['record']
            self._refresh_counts()
            self._refresh_recent()
            if not out['result']['ok']:
                try:
                    import winsound
                    self.executor.submit(winsound.Beep,2500,200)
                except ImportError:
                    self.root.bell()

    def _poll(self):
        if self.closed:
            return
        try:
            if self.task is not None:
                future,done = self.task
                if future.done():
                    self.task = None
                    self._set_admin_editable(True)
                    try:
                        value = future.result()
                        done(value)
                    except Exception as e:
                        traceback.print_exc()
                        self.pause()
                        self.message.set('Operación no completada: '+str(e))
                        messagebox.showerror('Operación no completada',str(e),parent=self.root)
            frame,stamp,info = self.camera.snapshot()
            self.camera_status.set(info['status'])
            fresh = frame is not None and info.get('ready') and time.monotonic()-stamp <= 1.
            if getattr(self,'pending_start',False) and self.engine and self.task is None:
                if fresh and info.get('compatible'):
                    self.running,self.pending_start = True,False
                    self.start_button.configure(text='Pausar inspección')
                    self.message.set('Inspección activa. El ciclo requiere retiro confirmado entre piezas.')
                elif info.get('ready') and not info.get('compatible'):
                    self.pause()
                    self.message.set('Inicio bloqueado: resolución o propiedades de cámara incompatibles. Revisa Administración.')
                elif 'Abriendo' not in info['status']:
                    self.pause()
                    self.message.set('No se pudo iniciar: '+info['status'])
            if not fresh:
                if self.running:
                    self.pause()
                    if self.cycle:
                        self.executor.submit(self.cycle.disconnected)
                    self.message.set('Inspección detenida: no hay imagen reciente. Revisa cámara y vuelve a iniciar.')
                self.video.display(None)
                self.admin_video.display(None)
            elif self.admin_mode:
                if time.monotonic() >= getattr(self,'preview_hold_until',0):
                    preview = frame.copy()
                    try:
                        preview = cv2.convertScaleAbs(preview,alpha=float(self.fields['contrast'].get())/100.,beta=float(self.fields['brightness'].get()))
                    except ValueError:
                        pass
                    if self.draft and self.draft['roi']:
                        x1,y1,x2,y2 = self.draft['roi']
                        cv2.rectangle(preview,(x1,y1),(x2,y2),(255,220,40),2)
                    self.admin_video.display(preview)
            elif self.running:
                if self.task is None and stamp != self.last_stamp:
                    self.last_stamp = stamp
                    cycle,m = self.cycle,copy.deepcopy(self.active_model)
                    context = copy.deepcopy(self.production_context)
                    def work(frame=frame,stamp=stamp,cycle=cycle,m=m,context=context):
                        out = cycle.process(frame,stamp)
                        if out['event']:
                            out['record'] = self.store.record(m,out['event'],image=frame,annotated=out['annotated'],**context)
                        return out
                    self.task = (self.executor.submit(work),self._display_output)
            else:
                self.video.display(frame)
        except Exception as e:
            self.pause()
            self.message.set('Error de operación: '+str(e))
            traceback.print_exc()
        finally:
            if not self.closed:
                self.poll_id = self.root.after(70,self._poll)

    def _event_by_id(self,eid):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM events WHERE id=?',(eid,)).fetchone()
        if row is None:
            raise ValueError('No se encontró la inspección seleccionada.')
        return dict(row)

    def _open_event(self,tree):
        selected = tree.selection()
        if selected:
            self._show_event(self._event_by_id(selected[0]))

    def show_last(self):
        if not self.active_model:
            raise ValueError('Selecciona un número de parte.')
        rows = self.store.events(model_id=self.active_model['id'],lot=self.lot.get() or None,kind='production',limit=1)
        if not rows:
            raise ValueError('No hay inspecciones registradas para esta selección.')
        self._show_event(rows[0])

    def _show_event(self,row):
        dialog = tk.Toplevel(self.root)
        dialog.title(f"Inspección {row['part']} · {row['outcome']}")
        dialog.geometry('1000x740')
        text = f"{row['timestamp']} · {row['part']} · v{row['version']} · {EVENT_TYPES.get(row['kind'], row['kind'])}\nOperador: {row['operator']} · Lote: {row['lot']} · Orden: {row['work_order']}"
        if row['truth']:
            text += f"\nPieza: {row['piece_id']} · condición real: {row['truth']}"
        result = json.loads(row['result'])
        if result:
            text += f"\nResultado: {row['outcome']} · coincidencia: {result['match']:.3f}"
            for z in result['zonas']:
                text += f"\n{z.get('name', 'Zona '+str(z['idx']))}: {'OK' if z['ok'] else 'Diferencia detectada'} · defecto mayor {z['pix']} / límite {z['lim']}"
        else:
            text += '\nSIN EVALUACIÓN · '+row['note']
        ttk.Label(dialog,text=text,padding=10,wraplength=960,justify='left').pack(fill='x')
        canvas = ImageCanvas(dialog)
        canvas.pack(fill='both',expand=True)
        def display(key):
            if not row.get(key):
                raise ValueError('No se guardó esa imagen para esta evaluación.')
            path = (self.store.root / row[key]).resolve()
            if not path.is_relative_to(self.store.root):
                raise ValueError('Ruta de imagen inválida.')
            canvas.display(load_image(path))
        actions = ttk.Frame(dialog,padding=8)
        actions.pack(fill='x')
        button(actions,'Imagen original',lambda:display('image'))
        button(actions,'Imagen con zonas',lambda:display('annotated'))
        button(actions,'Cerrar',dialog.destroy)
        if row.get('annotated') or row.get('image'):
            display('annotated' if row.get('annotated') else 'image')

    def show_history(self):
        dialog = tk.Toplevel(self.root)
        dialog.title('Historial · consultas y fotografías')
        dialog.geometry('1150x650')
        controls = ttk.Frame(dialog,padding=10)
        controls.pack(fill='x')
        models = {m['part']:m['id'] for m in self.store.list()}
        variables = {k:tk.StringVar() for k in ('part','lot','start','end','outcome','kind')}
        variables['part'].set(self.active_model['part'] if self.active_model else '')
        variables['lot'].set(self.lot.get())
        for i,(key,label,values) in enumerate([
                ('part','Número de parte',['']+list(models)),('lot','Lote',None),
                ('start','Desde (AAAA-MM-DD)',None),('end','Hasta (AAAA-MM-DD)',None),
                ('outcome','Resultado',['','OK','NG','SIN_EVALUACION']),
                ('kind','Tipo',['']+list(EVENT_TYPES.values()))]):
            ttk.Label(controls,text=label).grid(row=0,column=i,sticky='w',padx=4)
            if values is not None:
                widget = ttk.Combobox(controls,textvariable=variables[key],values=values,state='readonly',width=16)
            else:
                widget = ttk.Entry(controls,textvariable=variables[key],width=16)
            widget.grid(row=1,column=i,padx=4,sticky='ew')
            controls.columnconfigure(i,weight=1)
        tree = table(dialog,[('date','Fecha / hora',155),('part','Parte',130),('lot','Lote',90),
                             ('outcome','Resultado',110),('truth','Real',55),('version','Versión',65),('kind','Tipo',90)],height=12)
        rows = []
        def refresh():
            nonlocal rows
            vals = {k:v.get().strip() for k,v in variables.items()}
            vals['kind'] = next((key for key,label in EVENT_TYPES.items() if label == vals['kind']),'')
            rows = self.store.events(model_id=models.get(vals.pop('part')),lot=vals.pop('lot') or None,**vals,limit=10000)
            tree.delete(*tree.get_children())
            for r in rows:
                tree.insert('','end',iid=r['id'],values=(r['timestamp'][:19].replace('T',' '),r['part'],r['lot'],
                                                       r['outcome'],r['truth'] or '',r['version'],EVENT_TYPES.get(r['kind'],r['kind'])))
            count.configure(text=f'{len(rows)} registros mostrados (máximo 10 000). Reduce el rango para consultas mayores.')
        def export():
            path = filedialog.asksaveasfilename(parent=dialog,title='Exportar registros filtrados',defaultextension='.csv',filetypes=[('CSV','*.csv')])
            if path:
                self.store.export_csv(rows,path)
                messagebox.showinfo('Exportación','Se exportaron los registros mostrados, incluyendo mediciones por zona.',parent=dialog)
        tree.bind('<Double-1>',lambda e:self._open_event(tree))
        actions = ttk.Frame(dialog,padding=8)
        actions.pack(fill='x')
        button(actions,'Buscar / actualizar',refresh)
        button(actions,'Ver inspección y fotografías',lambda:self._open_event(tree))
        button(actions,'Exportar CSV',export)
        button(actions,'Cerrar',dialog.destroy)
        count = ttk.Label(dialog,padding=8)
        count.pack(fill='x')
        refresh()

    def close(self):
        if self.task is not None:
            messagebox.showinfo('Operación en curso','Espera a que termine la operación. Puedes pausar la inspección para cerrar después.',parent=self.root)
            self.pause()
            return
        if self.admin_mode and not self._resolve_dirty():
            return
        self.closed = True
        self.running = self.pending_start = False
        self.root.after_cancel(self.poll_id)
        self.camera.close()
        self.executor.shutdown(wait=False,cancel_futures=True)
        self.root.destroy()


# INICIO DE LA APLICACIÓN
"""Punto de entrada de Poka-Yoke 5, archivo único."""
from pathlib import Path
import argparse
import sys


def main():
    parser = argparse.ArgumentParser(description='Poka-Yoke 5 · Administración e inspección local')
    parser.add_argument('--data-dir',type=Path,default=Path(__file__).resolve().parent/'datos',
                        help='Ubicación de modelos, contraseña y registros; por defecto datos junto al programa')
    parser.add_argument('--version',action='version',version='Poka-Yoke 5.1.0')
    args = parser.parse_args()
    try:
        import tkinter as tk
        from tkinter import messagebox
    except ImportError as e:
        print(f'Falta una dependencia: {e}\nInstala con: python -m pip install numpy opencv-python Pillow',file=sys.stderr)
        return 1
    try:
        root = tk.Tk()
    except tk.TclError as e:
        print(f'No hay un entorno gráfico disponible: {e}',file=sys.stderr)
        return 1
    try:
        Application(root,args.data_dir)
        root.mainloop()
    except Exception as e:
        messagebox.showerror('No se pudo iniciar',f'{e}\nComprueba permisos de escritura en: {args.data_dir}')
        root.destroy()
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
