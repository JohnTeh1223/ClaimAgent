"""Local signature profiles and collision-free PDF signature placement."""
from __future__ import annotations

import io
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import fitz
from PIL import Image, ImageChops, ImageOps

PROFILE_ROOT = Path(__file__).resolve().parent.parent / 'others' / 'signatures'


@dataclass(frozen=True)
class SignatureAsset:
    png: bytes
    width: int
    height: int


def prepare_signature(path: str | Path) -> SignatureAsset:
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert('RGBA')
        if image.width * image.height > 25_000_000:
            raise ValueError('签名图片过大，请使用不超过 2500 万像素的图片。')
        # Preserve handwriting exactly; remove only the light background.
        white = Image.new('RGBA', image.size, 'white')
        gray = Image.alpha_composite(white, image).convert('L')
        ink = gray.point(lambda value: 255 if value < 210 else 0)
        bbox = ink.getbbox()
        if bbox is None:
            raise ValueError('图片中未检测到签名笔迹，请选择白底深色签名图片。')
        alpha = gray.point(lambda value: 0 if value >= 245 else min(255, (245 - value) * 6))
        image.putalpha(ImageChops.multiply(image.getchannel('A'), alpha))
        image = image.crop(bbox)
        stream = io.BytesIO()
        image.save(stream, format='PNG')
        return SignatureAsset(stream.getvalue(), image.width, image.height)


def read_profiles(root: Path = PROFILE_ROOT) -> dict:
    path = root / 'profiles.json'
    if not path.exists():
        return {'profiles': [], 'selected': '', 'enabled': False}
    return json.loads(path.read_text(encoding='utf-8'))


def atomic_write(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.claim-sign-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def save_profiles(data: dict, root: Path = PROFILE_ROOT) -> None:
    atomic_write(root / 'profiles.json', json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8'))


def import_signature(path: str | Path, name: str, root: Path = PROFILE_ROOT) -> dict:
    name = name.strip()
    if not name:
        raise ValueError('请输入使用者姓名。')
    asset = prepare_signature(path)
    data = read_profiles(root)
    profile = {'id': uuid4().hex, 'name': name, 'file': uuid4().hex + '.png'}
    atomic_write(root / profile['file'], asset.png)
    data['profiles'].append(profile)
    data.update(selected=profile['id'], enabled=True)
    save_profiles(data, root)
    return data


def signature_settings(data: dict, root: Path = PROFILE_ROOT) -> dict:
    profile = next((p for p in data['profiles'] if p['id'] == data.get('selected')), None)
    return {'enabled': bool(data.get('enabled')), 'name': profile['name'] if profile else '',
            'path': str(root / profile['file']) if profile else '', 'width': 130}


def selected_signature(config: dict) -> SignatureAsset | None:
    settings = config.get('signature', {})
    if not settings.get('enabled'):
        return None
    if not settings.get('path'):
        raise ValueError('已启用电子签名，请先上传并选择使用者签名。')
    # Uploaded assets already have their background removed.
    path = Path(settings['path'])
    with Image.open(path) as image:
        image.load()
        stream = io.BytesIO()
        image.convert('RGBA').save(stream, format='PNG')
        return SignatureAsset(stream.getvalue(), image.width, image.height)


def find_signature_rect(page, asset: SignatureAsset, width: float = 130):
    width = float(width)
    if not 40 <= width <= 200:
        raise ValueError('签名宽度须在 40 至 200 点之间。')
    height = max(18, width * asset.height / asset.width)
    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), colorspace=fitz.csGRAY, alpha=False)
    raster = Image.frombytes('L', (pix.width, pix.height), pix.samples)
    bounds = page.rect
    margin, padding = 16, 5
    notes = [fitz.Rect(w[:4]) * page.rotation_matrix for w in page.get_text('words')
             if str(w[4]).strip() in ('备注', '备')]
    notes = [r for r in notes if r.y0 > bounds.height * .5]
    regions = []
    if notes:
        note_y = max(r.y0 for r in notes)
        drawing_rects = [d['rect'] * page.rotation_matrix for d in page.get_drawings()]
        borders = [r.y1 for r in drawing_rects if r.y1 > note_y + height]
        regions.append((note_y, min(borders) if borders else bounds.height - margin))
    regions.append((margin, bounds.height - margin))
    for top, bottom in regions:
        y = bottom - height - padding
        while y >= top + padding:
            x = bounds.width - margin - width - padding
            while x >= margin + padding:
                rect = fitz.Rect(x, y, x + width, y + height)
                sample = raster.crop((int((x - padding) * 2), int((y - padding) * 2),
                                      int((rect.x1 + padding) * 2), int((rect.y1 + padding) * 2)))
                if not sum(sample.histogram()[:245]):
                    return rect
                x -= 8
            y -= 8
    raise ValueError('该页没有足够的空白区域放置签名，请调整发票版面后重试。')


def render_signed_pdf(source: str | Path, asset: SignatureAsset, width: float = 130) -> tuple[bytes, list]:
    placements = []
    with fitz.open(source) as doc:
        if doc.is_encrypted and not doc.authenticate(''):
            raise ValueError('无法给加密 PDF 添加签名。')
        for number, page in enumerate(doc, 1):
            try:
                rect = find_signature_rect(page, asset, width)
            except ValueError as exc:
                raise ValueError(f'{Path(source).name} 第 {number} 页：{exc}') from exc
            page.insert_image(rect * page.derotation_matrix, stream=asset.png,
                              rotate=page.rotation, keep_proportion=True, overlay=True)
            placements.append(rect)
        return doc.tobytes(garbage=3, deflate=True), placements
