#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Poka-Yoke 5.5.1 — interfaz industrial con CustomTkinter, archivo único.

Ejecutar: python poka_yoke_mejorado5.py
Dependencias: python -m pip install numpy opencv-python Pillow customtkinter
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
    import customtkinter as ctk
    from PIL import Image, ImageTk
except ImportError as error:
    if __name__ == '__main__':
        import sys
        print(f'Falta una dependencia: {error}\nInstala con: python -m pip install numpy opencv-python Pillow customtkinter\nPython debe incluir Tkinter.', file=sys.stderr)
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
    return hashlib.sha256(('complete-reference-engine-1:' + json.dumps(config, sort_keys=True, allow_nan=False,
                                    separators=(',', ':'))).encode()).hexdigest()


def reference_samples(config):
    """Las diez fotos OK, sin copias ni incorporación a las muestras NG."""
    return [dict(entry,id=f'reference:{i}',truth='OK',kind='crop',
                 piece_id=entry.get('piece_id') or f'REF-{i+1:02d}',note=entry.get('note',''))
            for i,entry in enumerate(config['references']) if entry]


def ng_samples(config):
    # Se conservan muestras antiguas adicionales, pero el flujo usa sólo diez NG.
    return [entry for entry in config['samples'] if entry['truth'] == 'NG'][:10]


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
        number(z.get('factor'), 1, 300, f'Límite de zona {i}', True)
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
                image TEXT, annotated TEXT, result TEXT NOT NULL, note TEXT, diagnostic TEXT)''')
            if 'diagnostic' not in {row[1] for row in db.execute('PRAGMA table_info(events)')}:
                db.execute('ALTER TABLE events ADD COLUMN diagnostic TEXT')
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
               truth=None, piece_id='', sample_id='', image=None, annotated=None, note='',diagnostic=''):
        if kind not in ('production', 'validation', 'test', 'manual', 'reference_check'):
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
                       result=json.dumps(result, ensure_ascii=False, allow_nan=False), note=note,diagnostic=diagnostic)
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

    def photo_results(self,m):
        entries = {e['id']:e for e in reference_samples(m['config'])+ng_samples(m['config'])}
        report = {}
        for kind in ('reference_check','validation'):
            for row in self.events(model_id=m['id'],kind=kind,limit=100000):
                sid = row['sample_id']
                if (sid in report or sid not in entries or row['version'] != m['version']
                        or row['fingerprint'] != canonical(m['config']) or row['truth'] != entries[sid]['truth']):
                    continue
                info = describe_photo_result(entries[sid],row)
                info.update(row_id=row['id'],path=entries[sid]['path'])
                report[sid] = info
        return report

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
        # Informar la comprobación de las referencias aparte de la validación independiente.
        checks = self.events(model_id=m['id'],kind='reference_check',limit=100000)
        current_refs = {entry['id']:entry for entry in reference_samples(m['config'])}
        seen = set()
        summary.update(reference_total=0,reference_ok=0,reference_ng=0,reference_unevaluated=0,
                       ng_photos=0,ng_photo_rejected=0,ng_photo_accepted=0,ng_photo_unevaluated=0)
        for row in checks:
            sid = row['sample_id']
            if (sid in seen or sid not in current_refs or row['truth'] != 'OK'
                    or row['version'] != m['version'] or row['fingerprint'] != canonical(m['config'])):
                continue
            seen.add(sid)
            summary['reference_total'] += 1
            key = {'OK':'reference_ok','NG':'reference_ng','SIN_EVALUACION':'reference_unevaluated'}[row['outcome']]
            summary[key] += 1
        current_ng = {entry['id']:entry for entry in ng_samples(m['config'])}
        seen,images = set(),set()
        for row in rows:
            sid = row['sample_id']
            entry = current_ng.get(sid)
            if (not entry or sid in seen or entry['path'] in refs or entry['path'] in images
                    or row['truth'] != 'NG' or row['version'] != m['version']
                    or row['fingerprint'] != canonical(m['config']) or row['piece_id'] != entry['piece_id']):
                continue
            seen.add(sid)
            images.add(entry['path'])
            summary['ng_photos'] += 1
            key = {'NG':'ng_photo_rejected','OK':'ng_photo_accepted','SIN_EVALUACION':'ng_photo_unevaluated'}[row['outcome']]
            summary[key] += 1
        return summary

    def approve(self, mid, operator, *, aligned_references):
        m = self.read(mid)
        self.integrity(m, True)
        if aligned_references != 10:
            raise ValueError('Faltan 10 referencias OK alineables.')
        s = self.validation_summary(m)
        if s['reference_total'] != 10 or s['ng_photos'] != 10:
            raise ValueError('Comprueba las mismas 10 fotos OK de referencia y las 10 fotos NG. No se requieren más fotos OK.')
        if (s['reference_unevaluated'] or s['reference_ng']
                or s['ng_photo_unevaluated'] or s['ng_photo_accepted']):
            raise ValueError('Hay pruebas incorrectas o sin evaluación; corrige y vuelve a probar esta versión.')
        m['validation'] = dict(version=m['version'], fingerprint=canonical(m['config']),
                               at=now(), administrator=operator, summary=s,
                               mode='reference_and_ng_check',independent_ok_validation=False)
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

def describe_photo_result(entry,row):
    expected = entry['truth']
    result = json.loads(row['result']) if isinstance(row.get('result'),str) else row.get('result')
    outcome,code = row['outcome'],row.get('diagnostic') or ''
    note = row.get('note') or ''
    if outcome == 'SIN_EVALUACION':
        invalid = code in ('INVALID_PHOTO','NOT_NG','REFERENCE_ALIGNMENT')
        label = 'NO CUENTA COMO NG' if code == 'NOT_NG' else ('FOTO INVÁLIDA' if invalid else 'SIN EVALUAR')
        return dict(state='fail' if invalid else 'unevaluated',label=label,
                    reason=note or 'No se pudo evaluar esta fotografía.',match=None)
    match = result.get('match') if result else None
    if outcome != expected:
        label = 'NG pasa como OK' if expected == 'NG' else 'OK pasa como NG'
        reason = ('Está etiquetada NG, pero el detector la acepta como OK. Revisa las zonas y el umbral: el defecto no fue detectado.'
                  if expected == 'NG' else 'Está etiquetada OK, pero el detector la rechaza. Revisa posición, iluminación, zonas y umbrales.')
        if result:
            failed = [z.get('name',f"Zona {z['idx']}") for z in result.get('zonas',[]) if not z['ok']]
            if failed:
                reason += ' Diferencias en: '+', '.join(failed)+'.'
        return dict(state='fail',label=label,reason=reason,match=match)
    return dict(state='pass',label='OK correcta' if expected == 'OK' else 'NG detectada',
                reason='El resultado coincide con la etiqueta de esta fotografía.',match=match)


class ReferencePreparationError(ValueError):
    def __init__(self,message,issues):
        super().__init__(message)
        self.issues = issues


def check_saved_photos(store,model):
    """Registra cada resultado, incluso fotos inválidas; una NG no impide evaluar las demás."""
    engine,issues,model_error = None,{},''
    try:
        engine = VisionEngine(store,model)
    except ReferencePreparationError as error:
        issues = error.issues
        model_error = str(error)
    except (ValueError,OSError,cv2.error) as error:
        model_error = str(error)
    c = model['config']
    ref_paths = {e['path'] for e in c['references']+[c['base_image']] if e}
    seen_ng = set()
    rows = []
    for entry in reference_samples(c)+ng_samples(c):
        sid = entry['id']
        image = annotated = result = None
        note,code = '', ''
        try:
            image = store.image(model['id'],entry)
            if sid in issues:
                note,code = issues[sid],'REFERENCE_ALIGNMENT'
            elif entry['truth'] == 'NG' and entry['path'] in ref_paths:
                note,code = 'Esta imagen es idéntica a una OK; no cuenta como foto NG. Repite su captura.','NOT_NG'
            elif entry['truth'] == 'NG' and entry['path'] in seen_ng:
                note,code = 'Esta foto NG repite exactamente otra imagen; no cuenta como otra foto NG.','NOT_NG'
            else:
                if entry['truth'] == 'NG':
                    seen_ng.add(entry['path'])
                expected_shape = (c['roi'][3]-c['roi'][1],c['roi'][2]-c['roi'][0]) if entry['kind']=='crop' and c['roi'] else (
                    c['camera']['height'],c['camera']['width'])
                if image.shape[:2] != expected_shape:
                    note,code = 'El tamaño de esta foto no coincide con la región o resolución del modelo. Repite su captura.','INVALID_PHOTO'
                elif engine is None:
                    note,code = 'No se pudo formar el patrón OK: '+model_error,'MODEL_UNAVAILABLE'
                else:
                    result,annotated,note = engine.evaluate_sample(image,entry)
                    if result is None:
                        code = 'LOW_MATCH'
        except (ValueError,OSError,cv2.error) as error:
            note,code = 'Foto ausente, dañada o incompatible: '+str(error),'INVALID_PHOTO'
        details = ' · '.join(v for v in (entry.get('defect_type',''),entry.get('note','')) if v)
        kind = 'reference_check' if sid.startswith('reference:') else 'validation'
        rows.append(store.record(model,result,kind=kind,operator='Administrador',truth=entry['truth'],
                    piece_id=entry['piece_id'],sample_id=sid,image=image,annotated=annotated,
                    diagnostic=code,note=' | '.join(v for v in (note,details) if v)))
    return rows



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
        media_f, media_u8, umbral = cache["media_f"], cache.get("evaluation_u8",cache["media_u8"]), cache["umbral"]
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
        missing = {f'reference:{i}':'Falta esta foto OK de referencia.'
                   for i,e in enumerate(model['config']['references']) if e is None}
        if missing:
            raise ReferencePreparationError('Se necesitan las 10 fotos OK de referencia.',missing)
        self.config = c = validate_config(self.model['config'], True)
        self.zonas_relativas = {str(i): c['zones'][str(i)]['rect'] for i in range(1, c['zone_count']+1)}
        self.factores_zona = {str(i): c['zones'][str(i)]['factor'] for i in range(1, c['zone_count']+1)}
        self._kernel_morf = np.ones((3, 3), np.uint8)
        self._ultimo_ajuste = None
        refs,issues = [],{}
        x1,y1,x2,y2 = c['roi']
        for i,e in enumerate(c['references']):
            try:
                img = store.image(model['id'], e)
                if img.shape[:2] != (y2-y1,x2-x1):
                    raise ValueError('El tamaño no coincide con la región de referencia.')
                refs.append(img if e.get('processed') else self.adjust(img))
            except (ValueError,OSError,cv2.error) as error:
                issues[f'reference:{i}'] = str(error)
        if issues:
            raise ReferencePreparationError('Hay fotos OK inválidas; revisa las miniaturas marcadas.',issues)
        if len(refs) != 10:
            raise ValueError('Se necesitan 10 referencias OK.')
        base_L = cv2.cvtColor(refs[0], cv2.COLOR_BGR2LAB)[..., 0]
        if float(base_L.std()) < 1:
            raise ReferencePreparationError('La referencia 1 no tiene contraste suficiente para localizar la pieza.',
                                            {'reference:0':'La foto no tiene contraste suficiente. Repite la captura con iluminación y enfoque adecuados.'})
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
            raise ReferencePreparationError('No se pudieron alinear estas referencias OK: '+', '.join(map(str, bad))+'. Reemplázalas.',
                {f'reference:{i-1}':'Esta foto OK no se alinea con la primera referencia. Revisa posición, orientación y enfoque.' for i in bad})
        # Keep a real representative (medoid), never a per-pixel blend of variants.
        labs = [cv2.cvtColor(r, cv2.COLOR_BGR2LAB) for r in aligned]
        smooth = [cv2.GaussianBlur(a,(5,5),0).astype(np.float32) for a in labs]
        distances = np.array([[float(np.mean(np.abs(a-b))) for b in smooth] for a in smooth])
        centrality = distances.mean(axis=1)
        eligible = np.flatnonzero(centrality <= centrality.min()+1e-6)
        representative = int(max(eligible,key=lambda i:cv2.Laplacian(labs[i][...,0],cv2.CV_64F).var()))
        self.reference_caches=[]
        for i,(image,lab,measurement) in enumerate(zip(aligned,labs,smooth)):
            gray = cv2.cvtColor(image,cv2.COLOR_BGR2GRAY)
            if gray.std()<1:
                raise ReferencePreparationError('Una referencia no tiene textura suficiente.',
                    {f'reference:{i}':'Repite esta foto OK con textura y enfoque suficientes.'})
            # Variation BETWEEN accepted variants must never increase defect tolerance.
            cache=dict(ancho=w,alto=h,media_f=measurement,media_u8=lab,
                       evaluation_u8=measurement.astype(np.uint8),media_L=measurement[...,0].copy(),
                       umbral=self._umbral_pixel(np.zeros_like(measurement)),
                       tpl_gray=gray,tpl_gray_s=cv2.resize(gray,None,fx=.5,fy=.5,interpolation=cv2.INTER_AREA),
                       L_mean=float(measurement[...,0].mean()),L_std=float(measurement[...,0].std()),
                       a_med=float(np.median(measurement[...,1])),b_med=float(np.median(measurement[...,2])),
                       a_std=float(measurement[...,1].std()),b_std=float(measurement[...,2].std()),
                       lim_zona={str(z):CAL_MIN_BLOB for z in range(1,c['zone_count']+1)},
                       reference_index=i,reference_bgr=image.copy())
            self.reference_caches.append(cache)
        self.representative_index=representative
        self.pattern_cache=self.reference_caches[representative]
        self.cache=self.pattern_cache
        self.aligned_references=len(aligned)
        self._candidates=[]
        self._last_alignment_valid=False

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
        h,w=adjusted.shape[:2]
        gray=cv2.cvtColor(adjusted,cv2.COLOR_BGR2GRAY)
        matches=[(self._localizar(gray,cache,h,w),cache) for cache in self.reference_caches]
        best,cache=max(matches,key=lambda pair:pair[0][0])
        score,x,y=best
        # Compare complete references only at the location of this same object.
        self._candidates=[(m[0],c) for m,c in matches
                          if m[0]>=MATCH_PRESENCIA and abs(m[1]-x)<=TOL_POS and abs(m[2]-y)<=TOL_POS]
        self.cache=cache
        return best

    def _roi_alineada(self, frame, tx, ty, cache):
        aw,ah=cache['ancho'],cache['alto']
        patch=frame[ty:ty+ah,tx:tx+aw]
        self._last_alignment_valid=False
        if patch.shape[:2]!=(ah,aw): return None
        # Do not normalize to one variant before testing all the others.
        lab=cv2.GaussianBlur(cv2.cvtColor(patch,cv2.COLOR_BGR2LAB),(5,5),0).astype(np.float32)
        self._last_alignment_valid=True
        return lab

    def evaluate_roi(self, roi, match):
        if not self._last_alignment_valid or not np.isfinite(roi).all():
            return None
        candidates=self._candidates or [(match,c) for c in self.reference_caches]
        results=[]
        for score,cache in candidates:
            matrix=self._alinear_a(cache['media_L'],roi[...,0])
            if matrix is None or not np.isfinite(matrix).all(): continue
            angle=float(np.degrees(np.arctan2(matrix[1,0],matrix[0,0])))
            if abs(angle)>ECC_ROT_MAX or np.max(np.abs(matrix[:,2]))>ECC_DESP_MAX: continue
            aligned=cv2.warpAffine(roi,matrix,(cache['ancho'],cache['alto']),
                                   flags=cv2.INTER_LINEAR,borderMode=cv2.BORDER_REPLICATE)
            normalized=self._normalizar_iluminacion(aligned,cache)
            result=self._evaluar_zonas(normalized,cache,self.config['zone_count'],self.config['tolerance'],score)
            if result is None: continue
            for z in result['zonas']: z['name']=self.config['zones'][str(z['idx'])]['name']
            result['adjustment']=(angle,float(matrix[0,2]),float(matrix[1,2]))
            result['reference_index']=cache['reference_index']
            result['reference_path']=self.config['references'][cache['reference_index']]['path']
            result['engine']='complete-reference-engine-1'
            penalty=max(z['pix']/max(1,z['lim']) for z in result['zonas'])
            results.append((not result['ok'],penalty,-score,cache,result,normalized))
        if not results: return None
        _,_,_,cache,result,normalized=min(results,key=lambda item:item[:3])
        self.cache=cache; self._ultimo_ajuste=result['adjustment']
        self.last_comparison_roi=normalized
        return result

    def evaluate_sample(self, image, entry):
        self._candidates=[]
        adjusted=image if entry.get('processed') else self.adjust(image)
        if entry['kind']=='crop':
            if adjusted.shape[:2]!=(self.cache['alto'],self.cache['ancho']):
                raise ValueError('La muestra recortada tiene dimensiones incompatibles.')
            gray=cv2.cvtColor(adjusted,cv2.COLOR_BGR2GRAY)
            self._candidates=[(float(cv2.matchTemplate(gray,c['tpl_gray'],cv2.TM_CCOEFF_NORMED)[0,0]),c)
                              for c in self.reference_caches]
            self._candidates=[(score,c) for score,c in self._candidates if np.isfinite(score) and score>=MATCH_PRESENCIA]
            score=max((m for m,c in self._candidates),default=0.)
            x,y=0,0
        else:
            self.check_frame(image); score,x,y=self.locate(adjusted)
        if not np.isfinite(score) or score<MATCH_PRESENCIA:
            return None,adjusted.copy(),'Sin coincidencia suficiente: muestra sin evaluación.'
        roi=self._roi_alineada(adjusted,x,y,self.cache)
        if roi is None: return None,adjusted.copy(),'Región fuera de límites.'
        result=self.evaluate_roi(roi,score)
        note='' if result is not None else 'Sin evaluación: no se obtuvo una alineación confiable.'
        return result,self.annotate(adjusted,result,x,y),note

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
        self._decision_signature = None
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
        # A changing component must not disappear into an eight-frame average.
        current=e.evaluate_roi(roi,score)
        if current is None:
            self.interrupt()
            output['state']='Sin evaluación · alineación insuficiente'
            return output
        signature=(current['ok'],current.get('reference_index'),
                   tuple(z['ok'] for z in current['zonas']))
        if self._decision_signature != signature:
            self.accum,self.count=None,0
        self._decision_signature=signature
        if self.accum is None:
            self.accum = np.zeros_like(roi, dtype=np.float32)
        self.accum += roi
        self.count += 1
        if self.count < FRAMES_PROMEDIO:
            output['state'] = f'Evaluando {self.count}/{FRAMES_PROMEDIO}'
            return output
        result = e.evaluate_roi(self.accum/self.count, score)
        if result is not None and (result['ok'],result.get('reference_index'),tuple(z['ok'] for z in result['zonas']))!=signature:
            self.accum,self.count=None,0
            output['state']='Estabilizando · cambió la comparación de pieza'
            return output
        if result is None:
            self.interrupt()
            output['state']='Sin evaluación · alineación insuficiente'
            return output
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

    def snapshot(self,previous_stamp=None,previous_frame=None):
        with self.lock:
            if self.frame is None:
                frame = None
            elif previous_frame is not None and previous_stamp == self.timestamp:
                # Reutiliza sólo la copia privada del consumidor, nunca el buffer de captura.
                frame = previous_frame
            else:
                frame = self.frame.copy()
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

UI_FONT = 'Segoe UI' if os.name == 'nt' else 'DejaVu Sans'


class ImageCanvas(tk.Canvas):
    def __init__(self, parent, empty_text='Sin imagen de cámara', video=False, **kw):
        self.video = video
        self._image_item = None
        self._empty_item = None
        super().__init__(parent, bg='#0a111d', highlightthickness=0, **kw)
        self.empty_text = empty_text
        self.source = None
        self.scale_factor = 1.
        self.offset = (0,0)
        self.bind('<Configure>', lambda e: self.redraw())

    def display(self, image, copy_image=True):
        if image is None and self.source is None and self._empty_item is not None:
            return
        self.source = None if image is None else (image.copy() if copy_image else image)
        self.redraw()

    def redraw(self):
        if self.source is None:
            self.delete('all')
            self._image_item = None
            self._empty_item = self.create_text(max(100,self.winfo_width()/2), max(80,self.winfo_height()/2),
                             text=self.empty_text, fill='#91a1b9', font=(UI_FONT,14))
            return
        if self._empty_item is not None:
            self.delete(self._empty_item)
            self._empty_item = None
        h,w = self.source.shape[:2]
        cw,ch = max(10,self.winfo_width()),max(10,self.winfo_height())
        scale = min(cw/w,ch/h)
        nw,nh = max(1,int(w*scale)),max(1,int(h*scale))
        self.scale_factor = scale
        self.offset = ((cw-nw)//2,(ch-nh)//2)
        if self.video:
            # Sólo se reduce la vista previa. La inspección conserva el cuadro original.
            small = cv2.resize(self.source,(nw,nh),interpolation=cv2.INTER_LINEAR)
            image = Image.fromarray(cv2.cvtColor(small,cv2.COLOR_BGR2RGB))
        else:
            rgb = cv2.cvtColor(self.source,cv2.COLOR_BGR2RGB)
            image = Image.fromarray(rgb).resize((nw,nh),Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(image)
        if self._image_item is None:
            self._image_item = self.create_image(*self.offset,image=self.photo,anchor='nw')
        else:
            self.coords(self._image_item,*self.offset)
            self.itemconfigure(self._image_item,image=self.photo)

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
        canvas = tk.Canvas(self,bg='#151e2b',highlightthickness=0)
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
               'test':'Prueba', 'manual':'Reevaluación manual',
               'reference_check':'Comprobación de referencia OK'}


def admin_action(fn):
    @functools.wraps(fn)
    def wrapped(self, *args, **kwargs):
        if not self.admin_mode:
            raise PermissionError('Entra a Administración para modificar modelos.')
        if self.task is not None:
            raise RuntimeError('Espera a que termine la operación en curso.')
        return fn(self, *args, **kwargs)
    return wrapped


class ActionButton(ctk.CTkButton):
    """Botón compacto con el mismo control de habilitación que los widgets de Tk."""
    def __init__(self,parent,text,command,tone='neutral',**kw):
        colors = {'neutral':('#243449','#334c69'), 'primary':('#087e68','#096653'),
                  'danger':('#803543','#a24251'), 'warning':('#88591c','#a36c25')}
        color,hover = colors[tone]
        from tkinter import font as tkfont
        font = tkfont.Font(root=parent,family=UI_FONT,size=10)
        width = kw.pop('width',max(64,font.measure(text)+24))
        self._measure_font = font
        super().__init__(parent,text=text,command=command,width=width,height=30,
                         corner_radius=5,border_width=1,border_color='#40516a',
                         fg_color=color,hover_color=hover,text_color='#edf3fa',
                         text_color_disabled='#8291a5',font=(UI_FONT,13),**kw)

    def configure(self,**kw):
        if 'text' in kw and 'width' not in kw:
            kw['width'] = max(64,self._measure_font.measure(kw['text'])+24)
        super().configure(**kw)
        if isinstance(self.master,ActionBar):
            self.master._last_width = None
            self.master._reflow()

    def state(self,spec=None):
        if spec is not None:
            self.configure(state='disabled' if 'disabled' in spec else 'normal')
        return ('disabled',) if self.cget('state') == 'disabled' else ()

    def instate(self,spec):
        disabled = self.cget('state') == 'disabled'
        return all(disabled if s == 'disabled' else not disabled for s in spec)


class ZoneSlider(ctk.CTkSlider):
    """Incluye redondeo visible y permite cargar valores con set(), igual que antes."""
    def __init__(self,parent,command):
        self.on_change = command
        super().__init__(parent,from_=1,to=300,number_of_steps=299,height=18,
                         progress_color='#19b99a',button_color='#32d4b0',
                         button_hover_color='#78ead0',fg_color='#304057',command=command)

    def set(self,value):
        super().set(value)
        self.on_change(self.get())

    def state(self,spec=None):
        if spec is not None:
            self.configure(state='disabled' if 'disabled' in spec else 'normal')
        return ('disabled',) if self.cget('state') == 'disabled' else ()

    def instate(self,spec):
        disabled = self.cget('state') == 'disabled'
        return all(disabled if s == 'disabled' else not disabled for s in spec)


class ActionBar(ctk.CTkFrame):
    """Distribuye acciones en las filas necesarias, sin ocultar botones a la derecha."""
    def __init__(self,parent,**kw):
        super().__init__(parent,fg_color='transparent',corner_radius=0,height=34,**kw)
        self.actions = []
        self._last_width = None
        self.bind('<Configure>',self._reflow,add='+')

    def add(self,widget):
        self.actions.append(widget)
        widget.place(x=0,y=2)
        self._last_width = None

    def _reflow(self,event=None):
        width = self.winfo_width()
        if width < 2 or width == self._last_width:
            return
        self._last_width = width
        row = used = 0
        for widget in self.actions:
            needed = widget.winfo_reqwidth()+6
            if used and used+needed > width:
                row,used = row+1,0
            widget.place_configure(x=used,y=row*34+2)
            used += needed
        self.configure(height=(row+1)*34)


class TabPanel(ctk.CTkFrame):
    """Pestañas oscuras; mantiene los identificadores de página usados por la aplicación."""
    def __init__(self,parent,**kw):
        super().__init__(parent,fg_color='#151e2b',corner_radius=6,**kw)
        self.pages = []
        self.current = None
        self.grid_columnconfigure(0,weight=1)
        self.grid_rowconfigure(1,weight=1)
        self.selector = ctk.CTkSegmentedButton(self,values=[' '],height=30,corner_radius=5,
            font=(UI_FONT,13),fg_color='#0c131e',selected_color='#285776',
            selected_hover_color='#316c91',unselected_color='#1d2a3c',
            unselected_hover_color='#2c405a',text_color='#edf3fa',command=self.select)
        self.selector.grid(row=0,column=0,sticky='ew',padx=4,pady=(4,6))

    def add(self,page,*,text):
        self.pages.append((page,text))
        self.selector.configure(values=[title for _,title in self.pages])
        if self.current is None:
            self.select(page)

    def select(self,page=None):
        if page is None:
            return str(self.current) if self.current else ''
        target = next((w for i,(w,title) in enumerate(self.pages)
                       if page == w or str(page) == str(w) or page == title or page == i),None)
        if target is None:
            raise ValueError('Pestaña desconocida.')
        if self.current is not None and self.current != target:
            self.current.grid_remove()
        self.current = target
        target.grid(row=1,column=0,sticky='nsew',padx=4,pady=(0,4))
        self.selector.set(next(title for w,title in self.pages if w == target))
        self.event_generate('<<NotebookTabChanged>>')
        return str(target)

    def tabs(self):
        return tuple(str(w) for w,_ in self.pages)


def button(parent, text, command, **kw):
    b = ActionButton(parent,text,command,**kw)
    if isinstance(parent,ActionBar):
        parent.add(b)
    else:
        b.pack(side='left',padx=(0,6),pady=2)
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


class PhotoUndoHistory:
    """Historial temporal: revierte sólo fotos modificadas, conserva los demás ajustes."""
    def __init__(self,limit=20):
        self.limit = limit
        self.actions = []

    def clear(self):
        self.actions.clear()

    def remember(self,before,after,label):
        refs = {i:copy.deepcopy(old) for i,(old,new) in enumerate(zip(before['references'],after['references'])) if old != new}
        old = {e['id']:(i,e) for i,e in enumerate(before['samples'])}
        new = {e['id']:e for e in after['samples']}
        samples = {sid:copy.deepcopy(old.get(sid)) for sid in old.keys()|new.keys()
                   if (old[sid][1] if sid in old else None) != new.get(sid)}
        base_changed = before['base_image'] != after['base_image']
        if refs or samples or base_changed:
            self.actions.append(dict(refs=refs,samples=samples,base_changed=base_changed,
                                     base=copy.deepcopy(before['base_image']),label=label))
            self.actions = self.actions[-self.limit:]

    def undo(self,config):
        if not self.actions:
            raise ValueError('No hay movimientos de fotos para deshacer desde el último guardado.')
        action = self.actions.pop()
        c = copy.deepcopy(config)
        for index,entry in action['refs'].items():
            c['references'][index] = entry
        changes = action['samples']
        c['samples'] = [entry for entry in c['samples'] if entry['id'] not in changes]
        for position,entry in sorted(value for value in changes.values() if value is not None):
            c['samples'].insert(min(position,len(c['samples'])),entry)
        if action['base_changed']:
            c['base_image'] = action['base']
        return c,action['label']


def zone_feedback(result):
    """Traduce mediciones sin alterar el criterio del motor ni sus límites."""
    rows = []
    for z in result.get('zonas',[]) if result else []:
        area,limit = z['pix'],z['lim']
        message = 'Dentro del límite' if z['ok'] else f'Supera el límite por {area-limit} px'
        rows.append(dict(idx=z['idx'],name=z.get('name',f"Zona {z['idx']}"),ok=z['ok'],
                         text=f"Diferencia {area} / límite {limit} px",message=message,
                         fraction=min(1.,max(0.,area/max(1,limit)))))
    return rows


def production_phase(state):
    if 'esperando retiro' in state.lower() or state.startswith(('Retira','Confirmando retiro')):
        return 'RETIRE LA PIEZA','#ffca78'
    if state.startswith('Esperando pieza'):
        return 'ESPERANDO PIEZA','#35d7b3'
    if state.startswith('Estabilizando'):
        return 'MANTENGA LA PIEZA FIJA','#ffca78'
    if state.startswith('Evaluando'):
        return 'EVALUANDO','#78c4ff'
    return 'SIN EVALUACIÓN','#a6b6cb'


def build_photo_comparison(store,model,entry):
    """Prepara una vista de la región que realmente evalúa el motor; nunca registra producción."""
    image = store.image(model['id'],entry)
    c = model['config']
    packet = dict(reference=None,sample=image,result=None,entry=copy.deepcopy(entry),
                  reference_title='Referencia OK disponible',sample_title='Foto completa',note='',zones=[])
    for available in c['references']:
        if not available:
            continue
        try:
            packet['reference'] = store.image(model['id'],available)
            break
        except (ValueError,OSError,cv2.error):
            pass
    try:
        engine = VisionEngine(store,model)
    except (ValueError,OSError,cv2.error) as error:
        packet['note'] = 'Sin evaluación: '+str(error)
        return packet
    packet['reference'] = engine.pattern_cache['reference_bgr'].copy()
    packet['reference_title'] = f'OK {engine.representative_index+1} · referencia real representativa'
    result,_,note = engine.evaluate_sample(image,entry)
    packet.update(result=result,note=note)
    if result is None:
        packet['sample_title'] = 'Foto sin localizar · sin evaluación'
        return packet
    packet['reference'] = engine.cache['reference_bgr'].copy()
    packet['reference_title'] = f'OK {result["reference_index"]+1} · referencia completa comparada'
    adjusted = image if entry.get('processed') else engine.adjust(image)
    _,x,y = engine.locate(adjusted) if entry['kind'] != 'crop' else (0,0,0)
    roi = engine.last_comparison_roi
    packet['sample'] = cv2.cvtColor(np.clip(roi,0,255).astype(np.uint8),cv2.COLOR_LAB2BGR)
    packet['sample_title'] = 'Foto seleccionada · alineada y normalizada'
    packet['zones'] = result['zonas']
    if not result['ok'] and all(z['ok'] for z in result['zonas']):
        packet['note'] = 'Las zonas pasan, pero la coincidencia global es insuficiente.'
    return packet


class ZoneReadout(ttk.Frame):
    """Mediciones y barras compactas reutilizadas entre producción y diagnóstico."""
    def __init__(self,parent):
        super().__init__(parent)
        self.rows = []
        self.signature = None
        for _ in range(5):
            frame = ttk.Frame(self,padding=(0,2))
            title = ttk.Label(frame,wraplength=235)
            title.pack(fill='x')
            value = ttk.Label(frame,wraplength=235,style='Muted.TLabel')
            value.pack(fill='x')
            bar = ctk.CTkProgressBar(frame,height=5,fg_color='#304057',progress_color='#35d7b3')
            bar.pack(fill='x',pady=(2,1))
            self.rows.append((frame,title,value,bar))
        self.global_note = ttk.Label(self,wraplength=235,foreground='#ffca78')

    def display(self,result):
        signature = json.dumps(result,sort_keys=True) if result else ''
        if signature == self.signature:
            return
        self.signature = signature
        feedback = zone_feedback(result)
        for i,(frame,title,value,bar) in enumerate(self.rows):
            if i >= len(feedback):
                frame.pack_forget()
                continue
            row = feedback[i]
            color = '#35d7b3' if row['ok'] else '#ff6b7b'
            title.configure(text=row['name']+' · '+('OK' if row['ok'] else 'NG'),foreground=color)
            value.configure(text=row['text']+'\n'+row['message'])
            bar.configure(progress_color=color)
            bar.set(row['fraction'])
            frame.pack(fill='x',pady=(0,3))
        self.global_note.pack_forget()
        if result and not result['ok'] and all(z['ok'] for z in result['zonas']):
            self.global_note.configure(text='Coincidencia global insuficiente.')
            self.global_note.pack(fill='x')


class Application:
    def __init__(self, root, data_dir, camera=None):
        self.root = root
        self.store = Store(data_dir)
        self.auth = Auth(data_dir)
        self.camera = camera or Camera()
        self.executor = ThreadPoolExecutor(max_workers=1,thread_name_prefix='motor-pokayoke')
        self.task = None
        self.task_locks_admin = False
        self.production_progress = queue.Queue()
        self.production_preview = None
        self.video_paints = {}
        self.preview_frame,self.preview_stamp = None,None
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
        self.photo_report = {}
        self.photo_undo = PhotoUndoHistory()
        self.guide_signature = None
        self.guide_summary = {}
        self.report_idle_id = None
        self.live_running = False
        self.live_generation = 0
        self.live_future = None
        self.live_cycle = None
        self.live_output = None
        self.live_last_stamp = 0.
        ctk.set_appearance_mode('dark')
        self.root.title('Poka-Yoke 5.5.1 · Inspección industrial')
        width = min(1440,max(800,self.root.winfo_screenwidth()-40))
        height = min(900,max(600,self.root.winfo_screenheight()-60))
        # CTk recibe unidades lógicas; winfo_screen* devuelve píxeles físicos.
        scale = ctk.ScalingTracker.get_window_scaling(self.root) if isinstance(self.root,ctk.CTk) else 1.
        self.root.geometry(f'{int(width/scale)}x{int(height/scale)}')
        self.root.minsize(int(min(1000,width)/scale),int(min(680,height)/scale))
        if isinstance(self.root,ctk.CTk):
            self.root.configure(fg_color='#0c131e')
        else:
            self.root.configure(bg='#0c131e')
        self._style()
        self.root.report_callback_exception = self._callback_error
        self.root.protocol('WM_DELETE_WINDOW',self.close)
        header = ctk.CTkFrame(root,fg_color='#0c131e',corner_radius=0)
        header.pack(fill='x',padx=14,pady=(8,6))
        header.grid_columnconfigure(1,weight=1)
        ctk.CTkLabel(header,text='POKA-YOKE',font=(UI_FONT,23,'bold'),
                     text_color='#edf3fa').grid(row=0,column=0,sticky='w')
        self.mode_label = ctk.CTkLabel(header,text='INSPECCIÓN',font=(UI_FONT,12,'bold'),
                                     text_color='#35d7b3')
        self.mode_label.grid(row=0,column=1,sticky='w',padx=16)
        self.camera_button = ActionButton(header,'Reconectar cámara',self.reconnect_camera)
        self.camera_button.grid(row=0,column=2,padx=6)
        self.access_button = ActionButton(header,'Administración',self.toggle_admin)
        self.access_button.grid(row=0,column=3)
        self.body = ctk.CTkFrame(root,fg_color='#0c131e',corner_radius=0)
        self.body.pack(fill='both',expand=True,padx=10)
        self.ops = ctk.CTkFrame(self.body,fg_color='#151e2b',corner_radius=6)
        self.admin = ctk.CTkFrame(self.body,fg_color='#151e2b',corner_radius=6)
        self._build_operations()
        self._build_admin()
        self.ops.pack(fill='both',expand=True)
        self.footer = ttk.Frame(root,padding=(12,5))
        self.footer.pack(side='bottom',fill='x',before=self.body)
        self.message = tk.StringVar(value='Selecciona un modelo validado o entra a Administración para prepararlo.')
        self.message_label = ttk.Label(self.footer,textvariable=self.message,wraplength=1100)
        self.message_label.pack(anchor='w',fill='x')
        self.camera_status = tk.StringVar(value='Cámara sin abrir')
        self.camera_status_label = ttk.Label(self.footer,textvariable=self.camera_status,style='Muted.TLabel',wraplength=1100)
        self.camera_status_label.pack(anchor='w',fill='x')
        self.root.bind('<Configure>',self._resize_shell,add='+')
        self._refresh_catalog()
        self.poll_id = self.root.after(80,self._poll)
        # La vista previa es independiente de preparar o validar un modelo.
        self._configure_preview(defaults()['camera'])

    def _resize_shell(self,event):
        if event.widget == self.root:
            for label in (self.message_label,self.camera_status_label):
                label.configure(wraplength=max(400,event.width-32))

    def _style(self):
        s = ttk.Style(self.root)
        s.theme_use('clam')
        families = {str(f).casefold() for f in self.root.tk.call('font','families')}
        family = UI_FONT if UI_FONT.casefold() in families else 'Helvetica'
        s.configure('.',background='#151e2b',foreground='#e4ecf7',fieldbackground='#1c293b',font=(family,10))
        s.configure('TFrame',background='#151e2b')
        s.configure('TLabel',background='#151e2b',foreground='#e4ecf7')
        s.configure('Title.TLabel',font=(family,20,'bold'))
        s.configure('Accent.TLabel',foreground='#35d7b3',font=(family,10,'bold'))
        s.configure('Muted.TLabel',foreground='#a0afc3')
        s.configure('Result.TLabel',font=(family,38,'bold'),padding=6)
        s.configure('Neutral.Result.TLabel',font=(family,18,'bold'),padding=6)
        s.map('TSpinbox',fieldbackground=[('invalid','#633244')])
        s.configure('TButton',padding=(8,4),background='#243449',borderwidth=1)
        s.map('TButton',background=[('active','#334c69'),('disabled','#1a2534')])
        s.configure('Treeview',background='#111a27',fieldbackground='#111a27',rowheight=27,borderwidth=0)
        s.configure('Treeview.Heading',background='#243449',font=(family,10,'bold'),padding=5)
        s.map('Treeview',background=[('selected','#285776')])
        s.configure('TEntry',padding=(6,4),borderwidth=1,lightcolor='#40516a',darkcolor='#40516a')
        s.configure('Compact.TEntry',padding=(5,2))
        s.configure('Compact.TCombobox',padding=(5,2))
        s.configure('TCombobox',padding=(6,4),arrowsize=14)
        s.map('TCombobox',fieldbackground=[('readonly','#1c293b')],foreground=[('readonly','#e4ecf7')])
        s.configure('TCheckbutton',padding=3)
        s.configure('TLabelframe',bordercolor='#304057',borderwidth=1)
        s.configure('TLabelframe.Label',foreground='#b2c4d9')
        s.configure('Vertical.TScrollbar',background='#33465e',troughcolor='#151e2b',arrowsize=12)
        for name,color in [('Pass','#1c5545'),('Fail','#7b293b'),('Warning','#6c4d1c'),('Pending','#243449')]:
            s.configure('Photo'+name+'.TButton',background=color,foreground='#ffffff',borderwidth=2,padding=4)
            s.map('Photo'+name+'.TButton',background=[('active',color),('pressed',color)])
        self.samples_colors = {'fail':'#692739','unevaluated':'#604919','pass':'#174436','pending':'#243449'}

    def _build_operations(self):
        top = ttk.Frame(self.ops,padding=8)
        top.pack(fill='x')
        self.op_part = tk.StringVar()
        self.op_combo = ttk.Combobox(top,textvariable=self.op_part,state='readonly',width=24)
        ttk.Label(top,text='Número de parte').grid(row=0,column=0,sticky='w',pady=(0,3))
        self.op_combo.grid(row=1,column=0,padx=(0,8),sticky='ew')
        self.op_combo.bind('<<ComboboxSelected>>',lambda e:self._select_operation())
        top.columnconfigure(0,weight=2)
        self.operator,self.lot,self.order = tk.StringVar(),tk.StringVar(),tk.StringVar()
        self.context_widgets = []
        for col,label,var in [(1,'Operador',self.operator),(2,'Lote',self.lot),(3,'Orden (opcional)',self.order)]:
            ttk.Label(top,text=label).grid(row=0,column=col,sticky='w',pady=(0,3))
            entry = ttk.Entry(top,textvariable=var,width=14)
            entry.grid(row=1,column=col,sticky='ew',padx=(0,8))
            self.context_widgets.append(entry)
            top.columnconfigure(col,weight=1)
        self.lot.trace_add('write',lambda *args:self._refresh_counts())
        self.lot.trace_add('write',lambda *args:self._refresh_recent() if hasattr(self,'recent') else None)
        self.op_description = ttk.Label(self.ops,text='Sin modelo seleccionado',padding=(8,2))
        self.op_description.pack(fill='x')
        main = ctk.CTkFrame(self.ops,fg_color='#0b111b',corner_radius=6)
        main.pack(fill='both',expand=True,padx=8,pady=6)
        main.grid_columnconfigure(0,weight=1)
        main.grid_columnconfigure(1,minsize=270)
        main.grid_rowconfigure(0,weight=1)
        self.video = ImageCanvas(main,width=640,height=280,video=True)
        self.video.grid(row=0,column=0,sticky='nsew',padx=4,pady=4)
        side = ttk.Frame(main,padding=10,width=270)
        side.grid(row=0,column=1,sticky='nsew',padx=(0,4),pady=4)
        self.phase_label = ctk.CTkLabel(side,text='PAUSADA',font=(UI_FONT,14,'bold'),text_color='#a6b6cb',wraplength=240)
        self.phase_label.pack(fill='x',pady=(0,3))
        self.result_label = ttk.Label(side,text='SIN EVALUACIÓN',style='Neutral.Result.TLabel',foreground='#a6b6cb',wraplength=240)
        self.result_label.pack(fill='x')
        self.cycle_status = tk.StringVar(value='Inspección pausada')
        ttk.Label(side,textvariable=self.cycle_status,wraplength=240).pack(fill='x',pady=4)
        diagnostic_scroll = ScrollFrame(side)
        diagnostic_scroll.pack(fill='both',expand=True)
        self.zone_readout = ZoneReadout(diagnostic_scroll.inner)
        self.zone_readout.pack(fill='x')
        self.zone_results = ttk.Label(diagnostic_scroll.inner,text='Las zonas aparecerán al evaluar.',wraplength=235,justify='left')
        self.zone_results.pack(fill='x',pady=3)
        self.ref_preview = ttk.Label(diagnostic_scroll.inner,text='Referencia del modelo')
        self.ref_preview.pack(pady=4)
        self.counts = tk.StringVar(value='Total: 0   |   OK: 0   |   NG: 0   |   NG: 0.0 %')
        ttk.Label(self.ops,textvariable=self.counts,style='Accent.TLabel',padding=(8,4)).pack(fill='x')
        actions = ActionBar(self.ops)
        actions.pack(fill='x',padx=8,pady=(0,4))
        self.start_button = button(actions,'Iniciar inspección',self.start_or_pause,tone='primary')
        self.choose_button = button(actions,'Buscar parte',self.choose_part)
        button(actions,'Última inspección',self.show_last)
        button(actions,'Historial y fotos',self.show_history)
        self.recent = table(self.ops,[('time','Hora',130),('part','Parte',160),('lot','Lote',120),
                                     ('outcome','Resultado',110),('version','Versión',80)],height=3)
        self.recent.bind('<Double-1>',lambda e:self._open_event(self.recent))
        self.ops.bind('<Configure>',self._responsive_operation)

    def _responsive_operation(self,event):
        frame = self.recent.master
        if event.height < 700 and frame.winfo_manager():
            frame.pack_forget()
        elif event.height >= 700 and not frame.winfo_manager():
            frame.pack(fill='both',expand=False)

    def _build_admin(self):
        self.admin_tabs = TabPanel(self.admin)
        self.admin_tabs.pack(fill='both',expand=True)
        catalog = ttk.Frame(self.admin_tabs,padding=8)
        self.editor = ttk.Frame(self.admin_tabs,padding=6)
        self.admin_tabs.add(catalog,text='Números de parte')
        self.admin_tabs.add(self.editor,text='Configurar modelo')
        search = ttk.Frame(catalog)
        search.pack(fill='x',pady=(0,6))
        ttk.Label(search,text='Buscar parte').pack(side='left')
        self.search = tk.StringVar()
        ttk.Entry(search,textvariable=self.search,width=28).pack(side='left',padx=8)
        self.archived = tk.BooleanVar(value=False)
        ttk.Checkbutton(search,text='Mostrar archivados',variable=self.archived,command=self._refresh_catalog).pack(side='left')
        self.search.trace_add('write',lambda *a:self._refresh_catalog())
        self.catalog = table(catalog,[('part','Número de parte',145),('description','Descripción',200),
                                      ('refs','OK / 10',65),('zones','Zonas',60),('state','Estado',190),
                                      ('version','Versión',65),('saved','Guardado',155)],height=9)
        self.catalog.bind('<Double-1>',lambda e:self.open_model())
        self.catalog.bind('<<TreeviewSelect>>',lambda e:self._catalog_thumbnail())
        self.catalog_photo = ttk.Label(catalog,text='Selecciona un modelo para ver su referencia.')
        self.catalog_photo.pack(anchor='w',pady=4)
        actions = ActionBar(catalog)
        actions.pack(fill='x')
        for text,action in [('Nuevo',self.new_model),('Abrir',self.open_model),('Renombrar',self.rename_model),
                            ('Duplicar',self.duplicate_model),('Archivar / restaurar',self.archive_model),
                            ('Exportar modelo',self.export_model),('Importar paquete',self.import_model)]:
            button(actions,text,action,tone='primary' if text in ('Nuevo','Abrir') else 'neutral')
        extra = ActionBar(catalog)
        extra.pack(fill='x')
        for text,action in [('Importar v3/v4',self.import_legacy),('Cambiar contraseña',self.change_password),
                            ('Errores del catálogo',self.show_catalog_errors)]:
            button(extra,text,action)
        bar = ttk.Frame(self.editor)
        bar.pack(fill='x',pady=(0,3))
        bar.columnconfigure(0,weight=1)
        self.model_title = ttk.Label(bar,text='Abre o crea un número de parte.',style='Accent.TLabel',wraplength=650)
        self.model_title.grid(row=0,column=0,sticky='w')
        self.dirty_label = ttk.Label(bar,text='',foreground='#ffca78')
        self.dirty_label.grid(row=0,column=1,sticky='e',padx=8)
        actions = ActionBar(self.editor)
        actions.pack(fill='x',pady=(0,5))
        self.save_button = button(actions,'Guardar configuración',self.save_model,tone='primary')
        button(actions,'Restaurar guardada',self.discard_changes)
        button(actions,'Versión anterior',self.restore_previous)
        button(actions,'Comprobar ajustes',self.try_settings)
        self.editor_workspace = ttk.Frame(self.editor)
        self.editor_workspace.pack(fill='both',expand=True)
        self.editor_workspace.columnconfigure(0,minsize=280)
        self.editor_workspace.columnconfigure(1,weight=1)
        self.editor_workspace.rowconfigure(0,weight=1)
        self.control_tabs = TabPanel(self.editor_workspace,width=280)
        self.control_tabs.grid(row=0,column=0,sticky='nsew',padx=(0,6))
        self.control_tabs.grid_propagate(False)
        panels = {}
        for title in ('Guía','Zonas','Cámara','Ajustes'):
            scroll = ScrollFrame(self.control_tabs)
            self.control_tabs.add(scroll,text=title)
            panels[title] = scroll.inner
        self._build_preparation_guide(panels['Guía'])
        self.fields = {}
        def field(parent,key,label,value,values=None):
            row = ttk.Frame(parent)
            row.pack(fill='x',padx=6,pady=(5,0))
            row.columnconfigure(1,weight=1)
            ttk.Label(row,text=label,wraplength=132).grid(row=0,column=0,sticky='w',padx=(0,6))
            var = tk.StringVar(value=str(value))
            self.fields[key] = var
            widget = ttk.Combobox(row,textvariable=var,values=values,state='readonly',width=8) if values else ttk.Entry(row,textvariable=var,width=9)
            widget.grid(row=0,column=1,sticky='ew')
            var.trace_add('write',self._mark_dirty)
        camera = panels['Cámara']
        ttk.Label(camera,text='DISPOSITIVO Y RESOLUCIÓN',style='Accent.TLabel').pack(anchor='w',padx=6,pady=6)
        field(camera,'camera.index','Cámara',0,list(range(11)))
        field(camera,'camera.width','Ancho (px)',1280)
        field(camera,'camera.height','Alto (px)',720)
        row = ActionBar(camera)
        row.pack(fill='x',padx=6,pady=6)
        button(row,'Abrir cámara',self.open_camera)
        button(row,'Resolución real',self.use_actual_resolution)
        button(row,'Detalles',self.camera_details)
        ttk.Label(camera,text='Ajustes opcionales · vacío conserva el valor de la cámara.',wraplength=238,style='Muted.TLabel').pack(fill='x',padx=6,pady=5)
        for key,label in [('exposure','Exposición'),('focus','Enfoque'),('white_balance','Balance de blanco')]:
            field(camera,'camera.'+key,label,'')
        settings = panels['Ajustes']
        ttk.Label(settings,text='IMAGEN Y CICLO',style='Accent.TLabel').pack(anchor='w',padx=6,pady=6)
        field(settings,'brightness','Brillo (-100 a 100)',0)
        field(settings,'contrast','Contraste (50–300)',100)
        field(settings,'tolerance','Tolerancia base (2000–15000)',5000)
        field(settings,'absence_seconds','Retiro de pieza (segundos)',.6)
        self.normalize_color = tk.BooleanVar(value=True)
        ttk.Checkbutton(settings,text='Normalizar color',variable=self.normalize_color,
                        command=self._mark_dirty).pack(anchor='w',padx=6,pady=(10,4))
        ttk.Label(settings,text='Desactívalo si el color es un criterio de defecto. Comprueba nuevamente después del cambio.',
                  wraplength=238,style='Muted.TLabel').pack(fill='x',padx=6)
        controls = panels['Zonas']
        field(controls,'zone_count','Zonas activas',3,[3,5])
        ttk.Label(controls,text='Menor % = mayor sensibilidad.\nGuarda y comprueba después de ajustar.',wraplength=238,
                  style='Muted.TLabel').pack(fill='x',padx=6,pady=6)
        self.zone_fields,self.zone_scales,self.zone_values = {},{},{}
        self.zone_inputs,self.zone_steps = {},{}
        self._updating_zone_scale = False
        for i in range(1,6):
            box = ctk.CTkFrame(controls,fg_color='#1b283a',corner_radius=6,border_width=1,border_color='#304057')
            box.pack(fill='x',padx=5,pady=(0,6))
            box.grid_columnconfigure(1,weight=1)
            ctk.CTkLabel(box,text=f'Z{i}',font=(UI_FONT,13,'bold'),text_color='#35d7b3',width=28).grid(row=0,column=0,padx=(6,4),pady=5)
            name,factor = tk.StringVar(value=f'Zona {i}'),tk.StringVar(value='100')
            ttk.Entry(box,textvariable=name,width=14).grid(row=0,column=1,columnspan=2,sticky='ew',padx=(0,6),pady=5)
            value = tk.StringVar(value='100 %')
            tuning = ctk.CTkFrame(box,fg_color='transparent',corner_radius=0)
            tuning.grid(row=1,column=0,columnspan=3,sticky='ew',padx=6)
            tuning.columnconfigure(1,weight=1)
            minus = ActionButton(tuning,'−',lambda key=str(i):self._step_zone(key,-1),width=28)
            minus.grid(row=0,column=0,padx=(0,3))
            scale = ZoneSlider(tuning,command=lambda v,key=str(i):self._zone_scale_moved(key,v))
            scale.grid(row=0,column=1,sticky='ew',padx=2)
            plus = ActionButton(tuning,'+',lambda key=str(i):self._step_zone(key,1),width=28)
            plus.grid(row=0,column=2,padx=3)
            numeric = ttk.Spinbox(tuning,from_=1,to=300,increment=1,width=4,justify='right',textvariable=factor)
            numeric.grid(row=0,column=3,padx=(0,2))
            ctk.CTkLabel(tuning,text='%',width=12,font=(UI_FONT,12)).grid(row=0,column=4)
            numeric.bind('<Return>',lambda event,key=str(i):self._commit_zone_factor(key))
            numeric.bind('<FocusOut>',lambda event,key=str(i):self._commit_zone_factor(key,quiet=True))
            self.zone_inputs[str(i)],self.zone_steps[str(i)] = numeric,(minus,plus)
            state = ttk.Label(box,text='Sin dibujar',style='Muted.TLabel')
            state.grid(row=2,column=0,columnspan=2,sticky='w',padx=8,pady=5)
            draw = ActionButton(box,'Dibujar',lambda idx=i:self.edit_zone(idx))
            draw.grid(row=2,column=2,padx=(0,6),pady=5)
            name.trace_add('write',self._mark_dirty)
            self.zone_fields[str(i)] = (name,factor,state)
            self.zone_scales[str(i)],self.zone_values[str(i)] = scale,value
            factor.trace_add('write',lambda *args,key=str(i):self._zone_factor_changed(key))
            scale.set(100)
        right = ttk.Frame(self.editor_workspace)
        right.grid(row=0,column=1,sticky='nsew')
        right.columnconfigure(0,weight=1)
        right.rowconfigure(0,weight=3,minsize=170)
        right.rowconfigure(1,weight=2,minsize=280)
        preview = ttk.Frame(right)
        preview.grid(row=0,column=0,sticky='nsew',pady=(0,5))
        preview.grid_propagate(False)
        preview.columnconfigure(0,weight=1)
        preview.rowconfigure(0,weight=1)
        self.admin_video = ImageCanvas(preview,width=600,height=120,video=True)
        self.admin_video.grid(row=0,column=0,sticky='nsew')
        row = ActionBar(preview)
        row.grid(row=1,column=0,sticky='ew',pady=(3,0))
        self.roi_button = button(row,'Dibujar región',self.edit_roi)
        button(row,'Reconectar cámara',self.open_camera)
        self.live_button = button(row,'Probar en vivo',self.toggle_live_test,tone='primary')
        status_row = ttk.Frame(preview)
        status_row.grid(row=2,column=0,sticky='ew',pady=(2,0))
        self.roi_label = ttk.Label(status_row,text='Sin región',style='Muted.TLabel')
        self.roi_label.pack(side='left',padx=(0,10))
        self.live_status = ttk.Label(status_row,text='Prueba detenida · no modifica producción',wraplength=500)
        self.live_status.pack(side='left',fill='x',expand=True)
        self.editor_tabs = TabPanel(right)
        self.editor_tabs.grid(row=1,column=0,sticky='nsew')
        self.editor_tabs.grid_propagate(False)
        self.photo_fields,self.photo_targets,self.photo_baselines = {},{},{}
        self._photo_loading = False
        self.sample_galleries = {}
        self.selected_sample_ids = {'OK':None,'NG':None}
        gallery = ttk.Frame(self.editor_tabs)
        self.reference_page = gallery
        self.editor_tabs.add(gallery,text='Fotos OK · referencia')
        self.gallery_info = ttk.Label(gallery,text='0 / 10 referencias OK',style='Accent.TLabel',wraplength=650)
        self.gallery_info.pack(fill='x',pady=3)
        row = ActionBar(gallery)
        row.pack(fill='x')
        self.undo_buttons,self.compare_buttons = {},{}
        self.capture_ok_button = button(row,'Capturar OK',self.capture_reference,tone='primary')
        button(row,'Repetir',self.retake_reference)
        button(row,'Importar',self.import_reference)
        button(row,'Eliminar',self.remove_reference,tone='danger')
        self.undo_buttons['reference'] = button(row,'Deshacer',self.undo_photo)
        self.compare_buttons['reference'] = button(row,'Comparar OK',lambda:self.compare_photo('reference'))
        split = ttk.Frame(gallery)
        split.pack(fill='both',expand=True)
        split.columnconfigure(0,weight=1)
        split.columnconfigure(1,minsize=250)
        split.rowconfigure(0,weight=1)
        ref_scroll = ScrollFrame(split)
        ref_scroll.grid(row=0,column=0,sticky='nsew')
        self.reference_scroller = ref_scroll
        grid = ref_scroll.inner
        grid.bind('<Configure>',self._layout_reference_grid,add='+')
        details = ttk.Frame(split,width=250)
        details.grid(row=0,column=1,sticky='nsew',padx=(5,0))
        details.pack_propagate(False)
        self.ref_buttons = []
        for i in range(10):
            b = ttk.Button(grid,text=f'OK {i+1}\nSin imagen',command=lambda idx=i:self.select_reference(idx))
            b.grid(row=i//2,column=i%2,sticky='nsew',padx=3,pady=3)
            b.bind('<Double-1>',lambda e,idx=i:self.view_reference(idx))
            self.ref_buttons.append(b)
        for i in range(2):
            grid.columnconfigure(i,weight=1,uniform='reference')
        self._build_photo_details(details,'reference')
        truth = 'NG'
        page = ttk.Frame(self.editor_tabs)
        self.editor_tabs.add(page,text='Fotos NG')
        ttk.Label(page,text='10 fotos NG · selecciona una miniatura para describir el defecto.',
                  wraplength=650).pack(fill='x',pady=3)
        row = ActionBar(page)
        row.pack(fill='x')
        capture = button(row,'Capturar NG',lambda:self.capture_sample('NG'),tone='warning')
        button(row,'Repetir',lambda:self.retake_sample('NG'))
        button(row,'Importar',lambda:self.import_sample('NG'))
        button(row,'Eliminar',lambda:self.remove_sample('NG'),tone='danger')
        self.undo_buttons['NG'] = button(row,'Deshacer',self.undo_photo)
        self.compare_buttons['NG'] = button(row,'Comparar OK',lambda:self.compare_photo('NG'))
        split = ttk.Frame(page)
        split.pack(fill='both',expand=True)
        split.columnconfigure(0,weight=1)
        split.columnconfigure(1,minsize=250)
        split.rowconfigure(0,weight=1)
        left = ttk.Frame(split)
        left.grid(row=0,column=0,sticky='nsew')
        thumbnails = ScrollFrame(left)
        nav = ttk.Frame(left)
        nav.pack(side='bottom',fill='x')
        previous = button(nav,'Anterior',lambda:self.sample_page('NG',-1))
        next_button = button(nav,'Siguiente',lambda:self.sample_page('NG',1))
        info = ttk.Label(nav,text='Sin fotos')
        info.pack(side='left',padx=3)
        thumbnails.pack(fill='both',expand=True)
        self.sample_galleries[truth] = dict(page=page,grid=thumbnails.inner,offset=0,images=[],scroller=thumbnails,
            info=info,previous=previous,next=next_button,capture=capture)
        thumbnails.inner.bind('<Configure>',lambda event:self._layout_sample_grid('NG'),add='+')
        details = ttk.Frame(split,width=250)
        details.grid(row=0,column=1,sticky='nsew',padx=(5,0))
        details.pack_propagate(False)
        self._build_photo_details(details,truth)
        validation_scroll = ScrollFrame(self.editor_tabs)
        validation = validation_scroll.inner
        self.editor_tabs.add(validation_scroll,text='Comprobación')
        ttk.Label(validation,text='Comprueba las mismas 10 OK + 10 NG; no se requieren más referencias.',
                  wraplength=650).pack(fill='x',pady=3)
        row = ActionBar(validation)
        row.pack(fill='x')
        button(row,'Comprobar 10 OK + 10 NG',self.run_validation,tone='primary')
        button(row,'Habilitar inspección',self.approve_model)
        row = ActionBar(validation)
        row.pack(fill='x')
        button(row,'Ver foto',self.view_sample)
        button(row,'Editar detalles',self.edit_sample)
        button(row,'Siguiente error',self.show_problem_photo)
        self.samples_table = table(validation,[('piece','Pieza',90),('truth','Real',50),('check','Resultado de foto',150),
                                  ('defect','Defecto',130),('note','Notas',150)],height=4)
        for state,color in self.samples_colors.items():
            self.samples_table.tag_configure(state,background=color,foreground='#ffffff')
        self.samples_table.bind('<Double-1>',lambda e:self.edit_sample())
        self.validation_info = ttk.Label(validation,text='Sin pruebas de esta versión.',wraplength=650,justify='left',padding=4)
        self.validation_info.pack(fill='x')
        ttk.Label(validation,text='Verde: correcto · Rojo: error · Ámbar: sin evaluar / resultado anterior.',
                  style='Muted.TLabel',wraplength=650).pack(fill='x',pady=3)
        ttk.Label(validation,text='Las OK se reutilizan para formar y comprobar la referencia. '
                  'Esta comprobación no mide el desempeño con piezas nuevas.',
                  wraplength=650,style='Muted.TLabel').pack(fill='x',pady=3)
        right.bind('<Configure>',self._resize_editor,add='+')
        self._update_undo_buttons()

    def _resize_editor(self,event):
        width = max(280,event.width-20)
        for widget in (self.gallery_info,self.validation_info):
            widget.configure(wraplength=width)
        self.live_status.configure(wraplength=max(220,width-145))
        self.model_title.configure(wraplength=max(300,self.editor.winfo_width()-210))

    def _build_photo_details(self,parent,key):
        box = ttk.LabelFrame(parent,text='Detalles de foto',padding=5)
        box.pack(fill='both',expand=True)
        actions = ActionBar(box)
        actions.pack(side='bottom',fill='x',pady=(3,0))
        button(actions,'Aplicar',lambda value=key:self.apply_photo_details(value))
        button(actions,'Ampliar',lambda value=key:self.view_photo(value))
        scroll = ScrollFrame(box)
        scroll.pack(fill='both',expand=True)
        content = scroll.inner
        variables = {name:tk.StringVar() for name in ('piece_id','defect_type','note')}
        title = ttk.Label(content,text='Selecciona una miniatura.',wraplength=210)
        title.grid(row=0,column=0,columnspan=2,sticky='w',pady=(0,3))
        ttk.Label(content,text='ID pieza').grid(row=1,column=0,sticky='w',padx=(0,4))
        piece = ttk.Entry(content,textvariable=variables['piece_id'],width=12,style='Compact.TEntry')
        piece.grid(row=1,column=1,sticky='ew',pady=2)
        ttk.Label(content,text='Defecto' if key == 'NG' else 'Detalle').grid(row=2,column=0,sticky='w',padx=(0,4))
        defect = ttk.Combobox(content,textvariable=variables['defect_type'],width=12,style='Compact.TCombobox',
                  values=['Faltante','Mal colocado','Invertido','Dañado','Incorrecto','Rayadura'] if key == 'NG' else ['Conforme'])
        defect.grid(row=2,column=1,sticky='ew',pady=2)
        ttk.Label(content,text='Notas').grid(row=3,column=0,sticky='w',padx=(0,4))
        note = ttk.Entry(content,textvariable=variables['note'],width=12,style='Compact.TEntry')
        note.grid(row=3,column=1,sticky='ew',pady=2)
        diagnosis = ttk.Label(content,text='',wraplength=210)
        diagnosis.grid(row=4,column=0,columnspan=2,sticky='w',pady=(3,0))
        content.columnconfigure(1,weight=1)
        self.photo_fields[key] = dict(variables=variables,title=title,widgets=[piece,defect,note],diagnosis=diagnosis,
                                      actions=actions,scroller=scroll)
        for var in variables.values():
            var.trace_add('write',lambda *args,value=key:self._photo_edited(value))
        self._fill_photo_details(key,None)

    def _layout_reference_grid(self,event=None):
        if hasattr(self,'ref_buttons'):
            self._layout_thumbnails(self.reference_scroller.inner,self.ref_buttons)

    def _layout_sample_grid(self,truth):
        grid = self.sample_galleries[truth]['grid']
        self._layout_thumbnails(grid,grid.winfo_children())

    @staticmethod
    def _layout_thumbnails(grid,widgets):
        cols = max(2,min(4,grid.winfo_width()//180))
        grid.photo_columns = cols
        for col in range(4):
            grid.columnconfigure(col,weight=1 if col<cols else 0,uniform='photos' if col<cols else '')
        for i,widget in enumerate(widgets):
            widget.grid_configure(row=i//cols,column=i%cols)

    def _build_preparation_guide(self,parent):
        self.guide_title = ttk.Label(parent,text='PREPARACIÓN · 0/6',style='Accent.TLabel')
        self.guide_title.pack(anchor='w',padx=6,pady=(6,4))
        self.guide_rows = []
        for i,title in enumerate(('Cámara conectada','Región general','Zonas de inspección','10 fotos OK','10 fotos NG','Comprobación')):
            row = ttk.Frame(parent,padding=(5,3))
            row.pack(fill='x',padx=3,pady=1)
            row.columnconfigure(0,weight=1)
            name = ttk.Label(row,text=f'{i+1}. {title}')
            name.grid(row=0,column=0,sticky='w')
            detail = ttk.Label(row,text='Pendiente',style='Muted.TLabel',wraplength=170)
            detail.grid(row=1,column=0,sticky='w')
            action = ActionButton(row,'Ir',lambda step=i:self.guide_to(step),width=40)
            action.grid(row=0,column=1,rowspan=2,padx=(4,0))
            self.guide_rows.append((name,detail,action))
        self.guide_next_button = ActionButton(parent,'Siguiente paso',self.guide_next,tone='primary')
        self.guide_next_button.pack(fill='x',padx=6,pady=6)
        self.guide_note = ttk.Label(parent,text='Abre o crea un número de parte.',wraplength=238,style='Muted.TLabel')
        self.guide_note.pack(fill='x',padx=6,pady=3)
        self.guide_steps = []

    def _update_preparation(self,info=None,stamp=None):
        if not self.admin_mode or not hasattr(self,'guide_rows'):
            return
        c = self.draft
        if c is None:
            return
        if info is None:
            _,stamp,info = self.camera.snapshot()
        camera_ok = bool(info.get('ready') and info.get('compatible') and stamp is not None and time.monotonic()-stamp<=1.)
        requested = getattr(self,'camera_requested',{})
        camera_ok = camera_ok and info.get('generation') == self.generation and all(requested.get(key)==c['camera'][key] for key in ('index','width','height'))
        camera_ok = camera_ok and (info.get('width'),info.get('height')) == (c['camera']['width'],c['camera']['height'])
        try:
            camera_ok = camera_ok and all(int(self.fields['camera.'+key].get()) == c['camera'][key]
                                          for key in ('index','width','height'))
        except ValueError:
            camera_ok = False
        try:
            total = int(self.fields['zone_count'].get())
        except ValueError:
            total = 0
        zones = sum(bool(c['zones'][str(i)]['rect']) for i in range(1,min(total,5)+1))
        refs = sum(bool(e and (self.store.model_dir(self.model['id'])/e['path']).is_file()) for e in c['references'])
        ng = sum((self.store.model_dir(self.model['id'])/e['path']).is_file() for e in ng_samples(c))
        summary = self.guide_summary
        checked = (not self.dirty and summary.get('reference_ok')==10 and summary.get('reference_total')==10
                   and summary.get('ng_photo_rejected')==10 and summary.get('ng_photos')==10
                   and not summary.get('reference_unevaluated') and not summary.get('ng_photo_unevaluated'))
        failures = sum(info['state'] in ('fail','unevaluated') for info in self.photo_report.values()) if not self.dirty else 0
        steps = [(camera_ok,'Conectada' if camera_ok else 'Revisar conexión / resolución'),
                 (bool(c['roi']),'Definida' if c['roi'] else 'Dibuja la pieza'),
                 (total in (3,5) and zones==total,f'{zones}/{total} zonas dibujadas'),
                 (refs==10,f'{refs}/10 fotos disponibles'),(ng==10,f'{ng}/10 fotos disponibles'),
                 (checked,'Correcta' if checked else ('Guarda y comprueba' if self.dirty else
                     (f'{failures} fotos por revisar' if failures else 'Pendiente de comprobar')))]
        signature = (tuple(steps),self.dirty,self.model['version'],bool(self.model.get('validation')),self.model['archived'])
        if signature == self.guide_signature:
            return
        self.guide_signature,self.guide_steps = signature,steps
        for i,((name,detail,_),(done,text)) in enumerate(zip(self.guide_rows,steps)):
            color = '#35d7b3' if done else ('#ff6b7b' if i==5 and failures else '#ffca78')
            name.configure(foreground=color)
            detail.configure(text=('Listo · ' if done else '')+text)
        completed = sum(done for done,_ in steps)
        self.guide_title.configure(text=f'PREPARACIÓN · {completed}/6')
        self.guide_next_button.configure(text='Ver comprobación' if completed==6 else 'Siguiente paso')
        self.guide_note.configure(text='Modelo archivado; restáuralo desde el catálogo.' if self.model['archived'] else (
            'Guarda los ajustes antes de comprobar.' if self.dirty else (
            'Habilita la versión en Comprobación.' if completed==6 and not self.model.get('validation') else
            ('Modelo habilitado para inspección.' if completed==6 else 'Pulsa Ir para resolver cada pendiente.'))))

    @admin_action
    def guide_to(self,step):
        self._need_model()
        self.admin_tabs.select(self.editor)
        if step == 0:
            self.control_tabs.select('Cámara')
        elif step == 1:
            self.edit_roi()
        elif step == 2:
            self.control_tabs.select('Zonas')
        elif step == 3:
            self.editor_tabs.select(self.reference_page)
        elif step == 4:
            self.editor_tabs.select(self.sample_galleries['NG']['page'])
        else:
            self.editor_tabs.select('Comprobación')

    @admin_action
    def guide_next(self):
        self._update_preparation()
        self.guide_to(next((i for i,(done,_) in enumerate(self.guide_steps) if not done),5))

    def _remember_photo_change(self,before,label):
        self.photo_undo.remember(before,self.draft,label)
        self._update_undo_buttons()

    def _update_undo_buttons(self):
        for widget in getattr(self,'undo_buttons',{}).values():
            widget.state(['!disabled'] if self.photo_undo.actions and self.task is None else ['disabled'])
        for key,widget in getattr(self,'compare_buttons',{}).items():
            widget.state(['!disabled'] if self.task is None and self._photo_entry(key) is not None else ['disabled'])

    @admin_action
    def undo_photo(self):
        current = self._sync()
        self.draft,label = self.photo_undo.undo(current)
        self.photo_targets.clear()
        self.photo_baselines.clear()
        self._mark_dirty()
        self._refresh_gallery()
        self._refresh_samples()
        self._update_undo_buttons()
        self.message.set('Deshecho: '+label+'. Guarda la configuración para conservar el cambio.')

    def _step_zone(self,key,delta):
        self._commit_zone_factor(key)
        value = int(self.zone_fields[key][1].get())
        self.zone_fields[key][1].set(str(max(1,min(300,value+delta))))

    def _commit_zone_factor(self,key,quiet=False):
        try:
            value = int(self.zone_fields[key][1].get())
            if not 1<=value<=300:
                raise ValueError()
        except ValueError:
            self.zone_inputs[key].state(['invalid'])
            if not quiet:
                raise ValueError('El umbral de cada zona debe ser un entero entre 1 y 300 %.') from None
            return False
        self.zone_inputs[key].state(['!invalid'])
        return True

    @admin_action
    def compare_photo(self,key):
        c = copy.deepcopy(self._sync())
        entry = self._photo_entry(key)
        if entry is None:
            raise ValueError('Selecciona primero una foto para compararla con las OK.')
        m = copy.deepcopy(self.model)
        m['config'] = c
        entry = dict(entry,truth='OK',kind='crop') if key=='reference' else copy.deepcopy(entry)
        label = 'Borrador actual' if self.dirty else f"Configuración guardada · v{m['version']}"
        def done(packet):
            self._show_photo_comparison(packet,label)
        self._submit(lambda:build_photo_comparison(self.store,m,entry),done,'Preparando comparación con el patrón OK…')

    def _show_photo_comparison(self,packet,configuration):
        dialog = ctk.CTkToplevel(self.root)
        dialog.title('Comparar con las OK · '+packet['entry'].get('piece_id','Foto'))
        dialog.configure(fg_color='#151e2b')
        scale = ctk.ScalingTracker.get_window_scaling(dialog)
        width,height = min(1120,self.root.winfo_screenwidth()-40),min(760,self.root.winfo_screenheight()-60)
        dialog.geometry(f'{int(width/scale)}x{int(height/scale)}')
        dialog.minsize(int(800/scale),int(600/scale))
        dialog.transient(self.root)
        dialog.grid_columnconfigure(0,weight=1)
        dialog.grid_rowconfigure(2,weight=1)
        result,entry = packet['result'],packet['entry']
        outcome = ('OK' if result['ok'] else 'NG') if result else 'SIN EVALUACIÓN'
        expected = entry.get('truth','')
        correct = result is not None and outcome==expected
        verdict = 'COINCIDE CON LA ETIQUETA' if correct else (
            f'{expected} PASA COMO {outcome}' if result else 'SIN EVALUACIÓN')
        title = f"{entry.get('piece_id','Foto')} · etiqueta {expected} · resultado {outcome} · {verdict}"
        verdict_label = ttk.Label(dialog,text=title,foreground='#35d7b3' if correct else '#ffca78',padding=8,wraplength=1000)
        verdict_label.grid(row=0,column=0,sticky='ew')
        choices = ActionBar(dialog)
        choices.grid(row=1,column=0,sticky='ew',padx=8)
        images = ttk.Frame(dialog)
        images.grid(row=2,column=0,sticky='nsew',padx=8,pady=3)
        images.columnconfigure((0,1),weight=1,uniform='comparison')
        images.rowconfigure(1,weight=1)
        ttk.Label(images,text=packet['reference_title'],padding=3).grid(row=0,column=0,sticky='w')
        ttk.Label(images,text=packet['sample_title'],padding=3).grid(row=0,column=1,sticky='w')
        left,right = ImageCanvas(images,empty_text='Sin referencia OK'),ImageCanvas(images,empty_text='Foto no disponible')
        left.grid(row=1,column=0,sticky='nsew',padx=(0,3))
        right.grid(row=1,column=1,sticky='nsew',padx=(3,0))
        def highlight(selected=None):
            for canvas,source in ((left,packet['reference']),(right,packet['sample'])):
                image = source.copy() if source is not None else None
                if image is not None:
                    for z in packet['zones']:
                        active = selected is None or selected==z['idx']
                        color = ((0,210,110) if z['ok'] else (60,60,245)) if active else (100,115,130)
                        x1,y1,x2,y2 = z['rect']
                        cv2.rectangle(image,(x1,y1),(x2,y2),color,3 if active else 1)
                        cv2.putText(image,f"Z{z['idx']}",(x1+2,y1+16),cv2.FONT_HERSHEY_SIMPLEX,.45,color,1)
                canvas.display(image)
        button(choices,'Todas las zonas',highlight)
        for zone in packet['zones']:
            button(choices,f"Z{zone['idx']}",lambda idx=zone['idx']:highlight(idx),tone='neutral' if zone['ok'] else 'danger')
        first_bad = next((z['idx'] for z in packet['zones'] if not z['ok']),None)
        highlight(first_bad)
        detail = ScrollFrame(dialog)
        detail.grid(row=3,column=0,sticky='ew',padx=8)
        detail.configure(height=154)
        detail.pack_propagate(False)
        metrics = ZoneReadout(detail.inner)
        metrics.display(result)
        metrics.pack(fill='x')
        note = configuration+' · Vista de diagnóstico; no guarda ni habilita el modelo.'
        if packet['note']:
            note += '\n'+packet['note']
        note_label = ttk.Label(dialog,text=note,wraplength=1000,padding=5,style='Muted.TLabel')
        note_label.grid(row=4,column=0,sticky='ew')
        def resize(event):
            if event.widget == dialog:
                verdict_label.configure(wraplength=max(300,event.width-24))
                note_label.configure(wraplength=max(300,event.width-24))
        dialog.bind('<Configure>',resize,add='+')
        actions = ActionBar(dialog)
        actions.grid(row=5,column=0,sticky='ew',padx=8,pady=(0,5))
        button(actions,'Cerrar comparación',dialog.destroy)
        dialog.bind('<Escape>',lambda event:dialog.destroy())
        dialog.comparison = packet
        dialog.comparison_canvases = (left,right)
        dialog.zone_metrics = metrics
        return dialog

    def _load_photo_report(self):
        self.photo_report = self.store.photo_results(self.model) if self.model else {}

    def _cache_photo_issue(self,sid,entry,error):
        previous = self.photo_report.get(sid,{})
        info = dict(previous) if previous.get('path') == entry['path'] else {}
        info.update(state='fail',label='FOTO ILEGIBLE',reason='No se puede leer esta foto: '+str(error),
                    path=entry['path'],match=None,local_read_error=True)
        self.photo_report[sid] = info

    def _photo_diagnostic(self,sid,entry):
        if entry is None:
            return dict(state='fail' if self.model else 'pending',label='SIN FOTO',reason='Falta capturar esta foto OK.')
        info = self.photo_report.get(sid)
        if info and info.get('path') == entry['path']:
            info = dict(info)
            if self.dirty and not info.get('local_read_error'):
                info.update(state='stale',label=info['label']+' (anterior)',
                            reason='Resultado anterior: guarda los cambios y vuelve a comprobar. '+info['reason'])
            return info
        return dict(state='pending',label='SIN COMPROBAR',reason='Esta foto no tiene una comprobación de la configuración actual.')

    @staticmethod
    def _photo_style(info):
        return 'Photo'+{'pass':'Pass','fail':'Fail','unevaluated':'Warning','stale':'Warning','pending':'Pending'}[info['state']]+'.TButton'

    def _decorate_photo(self,widget,sid,entry,text):
        info = self._photo_diagnostic(sid,entry)
        widget.photo_sid,widget.photo_entry,widget.photo_text = sid,entry,text
        widget.configure(style=self._photo_style(info),text=text+'\n'+info['label'])

    def _update_photo_indicators(self):
        widgets = list(self.ref_buttons)
        for gallery in self.sample_galleries.values():
            widgets += list(gallery['grid'].winfo_children())
        for widget in widgets:
            if hasattr(widget,'photo_sid'):
                self._decorate_photo(widget,widget.photo_sid,widget.photo_entry,widget.photo_text)
        if self.draft:
            for entry in reference_samples(self.draft)+ng_samples(self.draft):
                if self.samples_table.exists(entry['id']):
                    info = self._photo_diagnostic(entry['id'],entry)
                    self.samples_table.set(entry['id'],'check',info['label'])
                    self.samples_table.item(entry['id'],tags=('unevaluated' if info['state']=='stale' else info['state'],))
        for key,panel in self.photo_fields.items():
            entry = self._photo_entry(key)
            sid = f'reference:{self.gallery_slot}' if key == 'reference' else self.photo_targets.get(key)
            info = self._photo_diagnostic(sid,entry) if sid else dict(state='pending',label='',reason='')
            color = {'pass':'#35e0b5','fail':'#ff7b8d','unevaluated':'#ffcb78','stale':'#ffcb78','pending':'#9cabc1'}[info['state']]
            short = info['label']
            if info.get('match') is not None:
                short += f" · coincidencia {info['match']:.2f}"
            panel['diagnosis'].configure(text=short,foreground=color)

    def _run_report_idle(self):
        self.report_idle_id = None
        if not self.closed:
            self._update_photo_indicators()
            self._update_preparation()

    def show_problem_photo(self):
        self._need_model()
        problems = [sid for sid,info in self.photo_report.items() if info['state'] in ('fail','unevaluated')]
        problems.sort(key=lambda sid:(self.photo_report[sid]['state']!='fail',sid))
        if not problems:
            self.message.set('No hay fotos con error en la última comprobación de esta versión.')
            return
        previous = getattr(self,'last_problem_sid',None)
        pos = (problems.index(previous)+1)%len(problems) if previous in problems else 0
        sid = problems[pos]
        self.last_problem_sid = sid
        if sid.startswith('reference:'):
            index = int(sid.split(':')[1])
            self.select_reference(index)
            self.editor_tabs.select(self.reference_page)
            cols = getattr(self.reference_scroller.inner,'photo_columns',2)
            rows = math.ceil(10/cols)
            self.reference_scroller.inner.master.yview_moveto((index//cols)/rows)
        else:
            self._select_sample(sid)
            self.editor_tabs.select(self.sample_galleries['NG']['page'])
            self.sample_galleries['NG']['scroller'].inner.master.yview_moveto(0)
        self.message.set(f"Foto con problema {pos+1}/{len(problems)}: "+self.photo_report[sid]['reason'])

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
            panel['title'].configure(text=caption)
            self.compare_buttons[key].state(['!disabled'] if entry and self.task is None else ['disabled'])
            if hasattr(self,'ref_buttons'):
                self._update_photo_indicators()
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
        sid = f'reference:{self.gallery_slot}' if key == 'reference' else entry['id']
        info = self._photo_diagnostic(sid,entry)
        if info.get('row_id') and not info.get('local_read_error'):
            self._show_event(self._event_by_id(info['row_id']))
            return
        title = f"{entry.get('piece_id','Referencia OK')} · {entry.get('defect_type','')} · {entry.get('note','')} · {entry.get('captured_at','')}"
        try:
            image = self.store.image(self.model['id'],entry)
        except (ValueError,OSError,cv2.error):
            image = None
        self._show_image(image,title,info['reason'])

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
            except (OSError,ValueError,cv2.error) as error:
                self._cache_photo_issue(entry['id'],entry,error)
            b = ttk.Button(gallery['grid'],text=text,image=photo,compound='top',
                           command=lambda sid=entry['id']:self._select_sample(sid))
            b.grid(row=i//2,column=i%2,sticky='nsew',padx=3,pady=3)
            b.bind('<Double-1>',lambda event,key=truth:self.view_photo(key))
            self._decorate_photo(b,entry['id'],entry,text)
            for event in ('<MouseWheel>','<Button-4>','<Button-5>'):
                b.bind(event,gallery['scroller'].scroll_callback)
        for i in range(2):
            gallery['grid'].columnconfigure(i,weight=1)
        self._layout_sample_grid(truth)
        pages = max(1,(count+7)//8)
        gallery['info'].configure(text=f'{count}/10 fotos {truth} · {offset//8+1}/{pages}')
        gallery['previous'].state(['!disabled'] if offset else ['disabled'])
        gallery['next'].state(['!disabled'] if offset+8 < count else ['disabled'])

    def _callback_error(self, exc, value, tb):
        traceback.print_exception(exc,value,tb)
        messagebox.showerror('No se completó la operación',str(value),parent=self.root)

    def _zone_scale_moved(self,key,value):
        if self._updating_zone_scale:
            return
        factor = max(1,min(300,int(round(float(value)))))
        self._updating_zone_scale = True
        try:
            self.zone_fields[key][1].set(str(factor))
            self.zone_inputs[key].state(['!invalid'])
            self.zone_values[key].set(f'{factor} %')
        finally:
            self._updating_zone_scale = False
        self._mark_dirty()

    def _zone_factor_changed(self,key):
        if self._updating_zone_scale:
            return
        if not self._commit_zone_factor(key,quiet=True):
            self._mark_dirty()
            return
        factor = int(self.zone_fields[key][1].get())
        self._updating_zone_scale = True
        try:
            self.zone_scales[key].set(factor)
            self.zone_values[key].set(f'{factor} %')
        finally:
            self._updating_zone_scale = False
        self._mark_dirty()

    def _update_zone_activity(self):
        try:
            total = int(self.fields['zone_count'].get())
        except ValueError:
            return
        for key,scale in self.zone_scales.items():
            state = ['!disabled'] if int(key) <= total else ['disabled']
            scale.state(state)
            self.zone_inputs[key].state(state)
            for widget in self.zone_steps[key]:
                widget.state(state)

    def _submit(self, work, done, text):
        if self.task is not None:
            raise RuntimeError('Hay una operación en curso; espera a que termine.')
        self.stop_live_test()
        self.message.set(text)
        self.task_locks_admin = True
        self.task = (self.executor.submit(work),done)
        self._set_admin_editable(False)

    def _set_admin_editable(self, enabled):
        def walk(parent):
            for w in parent.winfo_children():
                if isinstance(w, (ActionButton, ZoneSlider, ttk.Button, ttk.Entry, ttk.Combobox, ttk.Spinbox, ttk.Checkbutton,ttk.Scale)):
                    w.state(['!disabled'] if enabled else ['disabled'])
                walk(w)
        walk(self.admin)
        if enabled:
            self._update_zone_activity()
            for key,panel in self.photo_fields.items():
                if self.photo_targets.get(key) is None:
                    for w in panel['widgets']:
                        w.state(['disabled'])
            self._update_undo_buttons()

    def _need_model(self):
        if self.model is None or self.draft is None:
            raise ValueError('Abre o crea primero un número de parte.')

    def _mark_dirty(self,*args):
        if self.loading or not self.admin_mode or self.model is None:
            return
        self.dirty = True
        self.dirty_label.configure(text='Cambios sin guardar')
        self.stop_live_test('Ajustes cambiados: vuelve a iniciar la prueba en vivo.')
        if self.report_idle_id is None:
            self.report_idle_id = self.root.after_idle(self._run_report_idle)
        if hasattr(self,'zone_scales'):
            self._update_zone_activity()

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
            self.photo_undo.clear()
            self._update_undo_buttons()
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
        self.stop_live_test()
        self.photo_undo.clear()
        self.guide_signature = None
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
            self._load_photo_report()
            self._refresh_gallery()
            self._refresh_samples()
            self._refresh_validation()
            self._update_zone_activity()
        finally:
            self.loading = False
        self._configure_preview(c['camera'])
        self._update_undo_buttons()
        self._update_preparation()

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
            self.stop_live_test()
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
        self.access_button.configure(text='Salir de Administración')
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
        self.stop_live_test()
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
        self.photo_undo.clear()
        self._update_undo_buttons()
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
                except (ValueError,OSError,cv2.error) as error:
                    self._cache_photo_issue(f'reference:{i}',entry,error)
            b.configure(image=photo,text=text,compound='top')
            self._decorate_photo(b,f'reference:{i}',entry,text)
        self.gallery_info.configure(text=f'{count} / 10 referencias legibles · seleccionada: {self.gallery_slot+1} · alineación pendiente de comprobar')
        self.roi_label.configure(text='Región definida' if self.draft['roi'] else 'Sin región')
        self._fill_photo_details('reference',self.draft['references'][self.gallery_slot],self.gallery_slot)

    def select_reference(self,index):
        self._flush_photo_details()
        self.gallery_slot = index
        self._refresh_gallery()

    def _show_image(self,image,title,details=''):
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.geometry('960x670')
        if details:
            ttk.Label(dialog,text=details,wraplength=900,padding=10).pack(fill='x')
        canvas = ImageCanvas(dialog,empty_text='Imagen no disponible')
        canvas.pack(fill='both',expand=True)
        canvas.display(image)
        ttk.Button(dialog,text='Cerrar',command=dialog.destroy).pack(pady=8)
        return dialog

    def view_reference(self,index):
        self._flush_photo_details('reference')
        info = self._photo_diagnostic(f'reference:{index}',self.draft['references'][index])
        if info.get('row_id') and not info.get('local_read_error'):
            return self._show_event(self._event_by_id(info['row_id']))
        try:
            image = self._reference_image(index)
        except (ValueError,OSError,cv2.error):
            image = None
        self._show_image(image,f'Referencia OK {index+1}',info['reason'])

    def _replace_reference(self,image,*,base_frame=None):
        c = self._sync()
        if c['roi'] is None:
            raise ValueError('Dibuja primero la región general de la pieza con Dibujar región.')
        x1,y1,x2,y2 = c['roi']
        if image.shape[:2] == (c['camera']['height'],c['camera']['width']):
            base_frame = image
            image = image[y1:y2,x1:x2].copy()
        if image.shape[:2] != (y2-y1,x2-x1):
            raise ValueError(f'La imagen debe ser un cuadro completo o un recorte de {x2-x1} × {y2-y1}.')
        if c['references'][self.gallery_slot] and not messagebox.askyesno('Reemplazar referencia',f'¿Reemplazar la referencia OK {self.gallery_slot+1} del borrador?',parent=self.root):
            return
        before = copy.deepcopy(c)
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
        self._remember_photo_change(before,f'captura / reemplazo de OK {selected+1}')
        self._refresh_gallery()
        self.message.set(f'Referencia OK {selected+1} preparada. Guarda cuando termines los cambios.')
        return entry

    def _capture_ok_image(self,image):
        c = self._sync()
        if c['roi'] is None:
            raise ValueError('Dibuja primero la región general de la pieza.')
        empty = next((i for i,e in enumerate(c['references']) if not e),None)
        if empty is None:
            raise ValueError('Las 10 referencias OK están completas. Usa Repetir para reemplazar una.')
        self.preview_hold_until = 0
        if c['references'][self.gallery_slot]:
            self.gallery_slot = empty
        return self._replace_reference(image)

    @admin_action
    def capture_reference(self):
        c = self._sync()
        return self._capture_ok_image(self._fresh_frame(c))

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
            return self._capture_ok_image(load_image(path))

    @admin_action
    def remove_reference(self):
        self._need_model()
        self._flush_photo_details()
        if not self.draft['references'][self.gallery_slot]:
            return
        if messagebox.askyesno('Eliminar referencia','¿Quitar esta referencia del borrador? La versión guardada sigue disponible.',parent=self.root):
            before = copy.deepcopy(self.draft)
            self.draft['references'][self.gallery_slot] = None
            self._remember_photo_change(before,f'eliminación de OK {self.gallery_slot+1}')
            self._mark_dirty()
            self._refresh_gallery()

    def _next_piece_id(self,truth):
        existing = {s['piece_id'].casefold() for s in self.draft['samples']}
        index = 1
        while f'{truth}-{index:02d}'.casefold() in existing:
            index += 1
        return f'{truth}-{index:02d}'

    def _add_sample(self,image,truth=None):
        c = self._sync()
        truth = truth or 'NG'
        if truth == 'OK':
            return self._capture_ok_image(image)
        if truth != 'NG':
            raise ValueError('La sección de captura debe indicar OK o NG.')
        if sum(s['truth'] == 'NG' for s in c['samples']) >= 10:
            raise ValueError('Las 10 fotos NG están completas. Usa Repetir para reemplazar una.')
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
            raise ValueError('Esta imagen ya está guardada como OK. Captura una foto NG.')
        e.update(id=uuid.uuid4().hex,piece_id=piece,truth=truth,kind=kind,note=note,
                 defect_type='Conforme' if truth == 'OK' else '',captured_at=now(),identity_confirmed=False)
        before = copy.deepcopy(c)
        c['samples'].append(e)
        validate_config(c)
        self.draft = c
        self._remember_photo_change(before,'captura de '+piece)
        self.selected_sample_ids[truth] = e['id']
        count = sum(s['truth'] == truth for s in c['samples'])
        self.sample_galleries[truth]['offset'] = ((count-1)//8)*8
        self._mark_dirty()
        self._refresh_samples()
        self.editor_tabs.select(self.sample_galleries[truth]['page'])
        self.message.set('Foto NG capturada. Puedes editar el tipo de defecto y las notas junto a su miniatura.')
        return e

    @admin_action
    def capture_sample(self,truth=None):
        if truth == 'OK':
            return self.capture_reference()
        c = self._sync()
        self.preview_hold_until = 0
        return self._add_sample(self._fresh_frame(c),truth or 'NG')

    @admin_action
    def import_sample(self,truth=None):
        if truth == 'OK':
            return self.import_reference()
        self._need_model()
        path = filedialog.askopenfilename(parent=self.root,title=f'Importar muestra {truth or "OK o NG"}',
                    filetypes=[('Imágenes','*.png *.jpg *.jpeg *.bmp *.tif *.tiff')])
        if path:
            return self._add_sample(load_image(path),truth)

    @admin_action
    def retake_sample(self,truth):
        if truth == 'OK':
            return self.retake_reference()
        c = self._sync()
        entry = self._selected_sample(truth)
        image = self._fresh_frame(c)
        asset = self.store.put_image(self.model['id'],image)
        if any(s['id'] != entry['id'] and s['path'] == asset['path'] for s in c['samples']):
            raise ValueError('Esta imagen ya pertenece a otra muestra.')
        if any(e and e['path'] == asset['path'] for e in c['references']+[c['base_image']]):
            raise ValueError('Esta imagen corresponde a una OK. Repite la captura con una pieza NG.')
        if not messagebox.askyesno('Repetir fotografía',f"¿Reemplazar la foto de {entry['piece_id']} conservando sus detalles?",parent=self.root):
            return
        before = copy.deepcopy(c)
        entry.update(asset,kind='full',captured_at=now())
        self._remember_photo_change(before,'reemplazo de '+entry['piece_id'])
        self._mark_dirty()
        self._refresh_samples()
        self.message.set('Fotografía repetida; se conservaron su identificador, tipo y notas en el borrador.')

    def _refresh_samples(self):
        selection = self.samples_table.selection()
        self.samples_table.delete(*self.samples_table.get_children())
        if self.draft:
            for e in reference_samples(self.draft)+ng_samples(self.draft):
                piece = e['piece_id']
                info = self._photo_diagnostic(e['id'],e)
                state = 'unevaluated' if info['state']=='stale' else info['state']
                self.samples_table.insert('','end',iid=e['id'],tags=(state,),
                    values=(piece,e['truth'],info['label'],e.get('defect_type',''),e['note']))
        if selection and self.samples_table.exists(selection[0]):
            self.samples_table.selection_set(selection[0])
        for truth in self.sample_galleries:
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
        selection = self.samples_table.selection()
        if selection and selection[0].startswith('reference:'):
            return self.view_reference(int(selection[0].split(':')[1]))
        e = self._selected_sample()
        self._show_image(self.store.image(self.model['id'],e),
                         f"{e['piece_id']} · real {e['truth']} · {e.get('defect_type','')} · {e.get('note','')}")

    @admin_action
    def edit_sample(self):
        selection = self.samples_table.selection()
        if selection and selection[0].startswith('reference:'):
            self.select_reference(int(selection[0].split(':')[1]))
            self.editor_tabs.select(self.reference_page)
            return
        e = self._selected_sample()
        self._select_sample(e['id'])
        self.editor_tabs.select(self.sample_galleries[e['truth']]['page'])
        self.photo_fields[e['truth']]['widgets'][0].focus_set()

    @admin_action
    def remove_sample(self,truth=None):
        self._flush_photo_details()
        e = self._selected_sample(truth)
        if messagebox.askyesno('Eliminar muestra','¿Quitar esta muestra del borrador?',parent=self.root):
            before = copy.deepcopy(self.draft)
            self.draft['samples'] = [s for s in self.draft['samples'] if s['id'] != e['id']]
            self._remember_photo_change(before,'eliminación de '+e['piece_id'])
            self._mark_dirty()
            self._refresh_samples()

    def _refresh_validation(self):
        if not self.model:
            return
        s = self.store.validation_summary(self.model)
        self.guide_summary = s
        self._update_preparation()
        c = self.model['config']
        ok_count = sum(entry is not None for entry in c['references'])
        ng_count = len(ng_samples(c))
        self.validation_info.configure(text=(
            f"Versión {self.model['version']} · guardadas: {ok_count}/10 OK y {ng_count}/10 NG\n"
            f"OK de referencia comprobadas: {s['reference_total']}/10 · aceptadas: {s['reference_ok']} · rechazadas: {s['reference_ng']}\n"
            f"NG comprobadas: {s['ng_photos']}/10 · detectadas: {s['ng_photo_rejected']} · aceptadas: {s['ng_photo_accepted']}\n"
            f"Sin evaluación: {s['reference_unevaluated']+s['ng_photo_unevaluated']} · Estado: {self.store.status(self.model)}\n"
            'Las OK son las referencias; este resultado no es una validación independiente.'))

    def stop_live_test(self,reason='Prueba en vivo detenida · no modifica producción'):
        self.live_running = False
        self.live_generation += 1
        self.live_cycle = None
        self.live_output = None
        if hasattr(self,'live_button'):
            self.live_button.configure(text='Probar en vivo')
            self.live_status.configure(text=reason,foreground='#9cabc1')

    @admin_action
    def toggle_live_test(self):
        if self.live_running:
            self.stop_live_test()
            return
        c = copy.deepcopy(self._sync())
        self._fresh_frame(c)
        m = copy.deepcopy(self.model)
        m['config'] = c
        def done(engine):
            self.live_cycle = InspectionCycle(engine,require_clear=False)
            self.live_running = True
            self.live_generation += 1
            self.live_last_stamp = 0.
            self.live_button.configure(text='Detener prueba')
            self.live_status.configure(text='PRUEBA EN VIVO · ajustes actuales · sin conteo de producción',foreground='#ffcb78')
            self.message.set('Prueba de inspección activa en Administración. No requiere habilitar el modelo ni aprobar las NG.')
        self._submit(lambda:VisionEngine(self.store,m),done,'Preparando prueba en vivo con las 10 OK; las NG no bloquean esta prueba…')

    def _consume_live_test(self):
        if self.live_future is None:
            return
        future,generation = self.live_future
        if not future.done():
            return
        self.live_future = None
        try:
            out = future.result()
        except Exception as error:
            if self.live_running and generation == self.live_generation:
                self.stop_live_test('Prueba detenida: '+str(error))
            return
        if not self.live_running or generation != self.live_generation:
            return
        self.live_output = out
        self.live_output_stamp = self.live_last_stamp
        result = out['result']
        label = 'SIN EVALUACIÓN' if not result else ('OK' if result['ok'] else 'NG')
        zones = '' if not result else ' · '.join(f"{z['name']}: {'OK' if z['ok'] else 'DIFERENCIA'}" for z in result['zonas'])
        color = '#ffcb78' if not result else ('#35e0b5' if result['ok'] else '#ff7b8d')
        self.live_status.configure(text=f"PRUEBA · {label} · {out['state']} · {zones}",foreground=color)

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
        if len(ng_samples(m['config'])) != 10:
            raise ValueError('Guarda 10 fotos OK y 10 fotos NG. Las mismas OK se usan como referencia.')
        def done(rows):
            self._load_photo_report()
            self._refresh_gallery()
            self._refresh_samples()
            self._refresh_validation()
            if any(info['state'] in ('fail','unevaluated') for info in self.photo_report.values()):
                self.last_problem_sid = None
                self.show_problem_photo()
            else:
                self.message.set(f'{len(rows)} fotos comprobadas: sus resultados están marcados en verde en las galerías.')
        self._submit(lambda:check_saved_photos(self.store,m),done,'Comprobando las 10 OK de referencia y las 10 NG…')

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
        self.result_label.configure(text='SIN EVALUACIÓN',style='Neutral.Result.TLabel',foreground='#a6b6cb')
        self.zone_results.configure(text='')
        self.zone_readout.display(None)
        self.phase_label.configure(text='PAUSADA',text_color='#a6b6cb')
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
        self.stop_live_test()
        was_running = self.running or getattr(self,'pending_start',False)
        self.running = self.pending_start = False
        self.production_preview = None
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
        self.phase_label.configure(text='PREPARANDO INSPECCIÓN',text_color='#ffca78')
        self._context_enabled(False)
        self.start_button.configure(text='Cancelar inicio')
        if self.engine and self.engine.model['id'] == m['id'] and self.engine.model['version'] == m['version']:
            self.message.set('Esperando imagen de cámara para continuar…')
            return
        def done(engine):
            self.engine = engine
            self.cycle = InspectionCycle(engine,require_clear=True)
            self.message.set('Modelo comprobado. Esperando cámara; retira la pieza para armar el primer ciclo.')
        self._submit(lambda:self._prepare_production_engine(m),done,'Cargando modelo y comprobando 10 referencias OK…')

    def _prepare_production_engine(self,model):
        # La prueba administrativa tolera NG inválidas; producción conserva la integridad completa.
        self.store.integrity(model,True)
        return VisionEngine(self.store,model)

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

    def _video_due(self,canvas,token):
        previous = self.video_paints.get(canvas)
        return previous is None or (previous[1] != token and time.monotonic()-previous[0]>=.05)

    def _paint_video(self,canvas,image,token,force=False):
        if image is None:
            self.video_paints.pop(canvas,None)
            canvas.display(None)
            return
        if force or self._video_due(canvas,token):
            # snapshot() y el motor entregan buffers propios que se tratan como inmutables.
            canvas.display(image,copy_image=False)
            self.video_paints[canvas] = (time.monotonic(),token)

    def _alert_ng(self):
        try:
            import winsound
            threading.Thread(target=winsound.Beep,args=(2500,200),daemon=True).start()
        except ImportError:
            self.root.bell()

    def _display_output(self,out):
        if not self.running:
            self._refresh_counts()
            self._refresh_recent()
            return
        stamp = out.get('frame_stamp',time.monotonic())
        self.production_preview = (stamp,out['annotated'])
        if out.get('record_pending') or time.monotonic()-stamp<=.2:
            self._paint_video(self.video,out['annotated'],('result',stamp),force=bool(out.get('record_pending')))
        detail = out['state']+f" · coincidencia {out['match']:.2f}"
        if self.cycle_status.get() != detail:
            self.cycle_status.set(detail)
        result = out['result']
        if result:
            text = 'OK' if result['ok'] else 'NG'
            if self.result_label.cget('text') != text:
                self.result_label.configure(text=text,style='Result.TLabel',foreground='#35e0b5' if result['ok'] else '#ff6b7b')
            self.zone_readout.display(result)
        else:
            if self.result_label.cget('text') != 'SIN EVALUACIÓN':
                self.result_label.configure(text='SIN EVALUACIÓN',style='Neutral.Result.TLabel',foreground='#a6b6cb')
            self.zone_readout.display(None)
        phase,color = production_phase(out['state'])
        if self.phase_label.cget('text') != phase:
            self.phase_label.configure(text=phase,text_color=color)
        if phase == 'ESPERANDO PIEZA' and self.message.get().startswith('Inspección registrada'):
            self.message.set('Inspección activa · esperando la siguiente pieza.')
        if out.get('record_pending'):
            self.message.set('Resultado emitido · guardando registro de inspección…')
            if result and not result['ok']:
                self._alert_ng()
        if out.get('record'):
            self.last_event = out['record']
            self._refresh_counts()
            self._refresh_recent()
            self.message.set('Inspección registrada · retira la pieza para continuar.')

    def _poll(self):
        if self.closed:
            return
        try:
            self._consume_live_test()
            while not self.production_progress.empty():
                self._display_output(self.production_progress.get_nowait())
            if self.task is not None:
                future,done = self.task
                if future.done():
                    self.task = None
                    if self.task_locks_admin:
                        self.task_locks_admin = False
                        self._set_admin_editable(True)
                    try:
                        value = future.result()
                        done(value)
                    except Exception as e:
                        traceback.print_exc()
                        self.pause()
                        self.stop_live_test('No se pudo iniciar la operación. Revisa las fotos y la configuración.')
                        self.message.set('Operación no completada: '+str(e))
                        messagebox.showerror('Operación no completada',str(e),parent=self.root)
            if isinstance(self.camera,Camera):
                frame,stamp,info = self.camera.snapshot(self.preview_stamp,self.preview_frame)
                self.preview_frame,self.preview_stamp = frame,stamp
            else:
                frame,stamp,info = self.camera.snapshot()
            self.camera_status.set(info['status'])
            if self.admin_mode and time.monotonic() >= getattr(self,'guide_next_refresh',0):
                self.guide_next_refresh = time.monotonic()+.5
                self._update_preparation(info,stamp)
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
                if self.live_running:
                    self.stop_live_test('Prueba detenida: cámara sin imagen reciente.')
                if self.running:
                    self.pause()
                    if self.cycle:
                        self.executor.submit(self.cycle.disconnected)
                    self.message.set('Inspección detenida: no hay imagen reciente. Revisa cámara y vuelve a iniciar.')
                    self.phase_label.configure(text='SIN CÁMARA',text_color='#ff6b7b')
                self._paint_video(self.video,None,None)
                self._paint_video(self.admin_video,None,None)
            elif self.admin_mode:
                if self.live_running:
                    if self.live_future is None and self.task is None and stamp != self.live_last_stamp:
                        self.live_last_stamp = stamp
                        cycle = self.live_cycle
                        self.live_future = (self.executor.submit(cycle.process,frame,stamp),self.live_generation)
                    recent = self.live_output and time.monotonic()-self.live_output_stamp <= .2
                    image = self.live_output['annotated'] if recent else frame
                    token = ('live',self.live_output_stamp) if recent else stamp
                    self._paint_video(self.admin_video,image,token)
                elif time.monotonic() >= getattr(self,'preview_hold_until',0) and self._video_due(self.admin_video,stamp):
                    try:
                        preview = cv2.convertScaleAbs(frame,alpha=float(self.fields['contrast'].get())/100.,beta=float(self.fields['brightness'].get()))
                    except ValueError:
                        preview = frame.copy()
                    if self.draft and self.draft['roi']:
                        x1,y1,x2,y2 = self.draft['roi']
                        cv2.rectangle(preview,(x1,y1),(x2,y2),(255,220,40),2)
                    self._paint_video(self.admin_video,preview,stamp)
            elif self.running:
                if self.task is None and stamp != self.last_stamp:
                    self.last_stamp = stamp
                    cycle,m = self.cycle,copy.deepcopy(self.active_model)
                    context = copy.deepcopy(self.production_context)
                    def work(frame=frame,stamp=stamp,cycle=cycle,m=m,context=context):
                        out = cycle.process(frame,stamp)
                        out['frame_stamp'] = stamp
                        if out['event']:
                            self.production_progress.put(dict(out,record_pending=True))
                            out['record'] = self.store.record(m,out['event'],image=frame,annotated=out['annotated'],**context)
                        return out
                    self.task_locks_admin = False
                    self.task = (self.executor.submit(work),self._display_output)
                recent = self.production_preview and time.monotonic()-self.production_preview[0] <= .2
                image = self.production_preview[1] if recent else frame
                token = ('result',self.production_preview[0]) if recent else stamp
                self._paint_video(self.video,image,token)
            else:
                self._paint_video(self.video,frame,stamp)
        except Exception as e:
            self.pause()
            self.message.set('Error de operación: '+str(e))
            traceback.print_exc()
        finally:
            if not self.closed:
                self.poll_id = self.root.after(15,self._poll)

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
        if row['kind'] in ('reference_check','validation') and row['truth'] in ('OK','NG'):
            info = describe_photo_result(dict(truth=row['truth']),row)
            text += '\n'+info['label']+' · '+info['reason']
        if result:
            text += f"\nResultado: {row['outcome']} · coincidencia: {result['match']:.3f}"
            for z in result['zonas']:
                text += f"\n{z.get('name', 'Zona '+str(z['idx']))}: {'OK' if z['ok'] else 'Diferencia detectada'} · defecto mayor {z['pix']} / límite {z['lim']}"
        else:
            text += '\nSIN EVALUACIÓN · '+row['note']
        ttk.Label(dialog,text=text,padding=10,wraplength=960,justify='left').pack(fill='x')
        canvas = ImageCanvas(dialog,empty_text='Imagen no disponible')
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
        self.stop_live_test()
        self.running = self.pending_start = False
        self.root.after_cancel(self.poll_id)
        if self.report_idle_id is not None:
            self.root.after_cancel(self.report_idle_id)
        self.camera.close()
        self.executor.shutdown(wait=False,cancel_futures=True)
        # Incluye los temporizadores de dibujo y escala de CustomTkinter.
        for pending in self.root.tk.call('after','info'):
            self.root.after_cancel(pending)
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
    parser.add_argument('--version',action='version',version='Poka-Yoke 5.5.1')
    args = parser.parse_args()
    try:
        import tkinter as tk
        from tkinter import messagebox
    except ImportError as e:
        print(f'Falta una dependencia: {e}\nInstala con: python -m pip install numpy opencv-python Pillow customtkinter',file=sys.stderr)
        return 1
    try:
        ctk.set_appearance_mode('dark')
        root = ctk.CTk()
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

