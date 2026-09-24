
import io
import math
import os
import re
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from PIL import Image, ImageOps, ImageTk, ImageFilter, ImageDraw
import numpy as np

try:
    from rembg import remove as rembg_remove
    REMBG_AVAILABLE = True
except Exception:
    REMBG_AVAILABLE = False
    rembg_remove = None

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DND_AVAILABLE = True
except Exception:
    DND_AVAILABLE = False

SUPPORTED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff'}


def normalize_drop_paths(raw: str):
    if not raw:
        return []
    try:
        root = tk._default_root
        if root is not None:
            return list(root.tk.splitlist(raw))
    except Exception:
        pass
    result, buf, in_brace = [], '', False
    for ch in raw:
        if ch == '{':
            in_brace = True
            buf = ''
        elif ch == '}':
            in_brace = False
            if buf:
                result.append(buf)
                buf = ''
        elif ch == ' ' and not in_brace:
            if buf:
                result.append(buf)
                buf = ''
        else:
            buf += ch
    if buf:
        result.append(buf)
    return result


def sanitize_filename(name: str, fallback='file'):
    name = re.sub(r'[\\/:*?"<>|]+', '_', (name or '').strip())
    name = re.sub(r'\s+', '_', name)
    name = name.strip('._')
    return name[:120] or fallback


DEFAULT_PRODUCT_CODE_QUERY_KEYS = ['goodsNo', 'nvMid', 'productNo', 'product_no', 'productId', 'product_id', 'sku', 'style', 'styleCode', 'itemNo', 'itmNo', 'id']
DEFAULT_PRODUCT_CODE_META_KEYS = ['sku', 'product:retailer_item_id', 'og:sku', 'twitter:data1']
DEFAULT_PRODUCT_CODE_REGEXES = [
    r'\"(?:goodsNo|productNo|product_no|product_id|productId|sku|styleCode|style|articleNo|articleno|modelNo|model_no|itemNo)\"\s*[:=]\s*\"?([A-Za-z0-9\-_]{4,40})\"?',
    r'\b(?:품번|상품코드|style code|style|sku|article no|model no)\b\s*[:：]?\s*([A-Za-z0-9\-_]{4,40})',
    r'\b([A-Z]{1,5}[0-9]{3,8}(?:-[A-Z0-9]{1,6})?)\b',
]


class ImageProcessor:
    def __init__(self, use_rembg=True):
        self.use_rembg = use_rembg and REMBG_AVAILABLE

    @staticmethod
    def _alpha_bbox(im_rgba: Image.Image):
        if im_rgba.mode != 'RGBA':
            im_rgba = im_rgba.convert('RGBA')
        return im_rgba.getchannel('A').getbbox()

    @staticmethod
    def trim_transparent_or_white(im_rgba: Image.Image, threshold=245, padding=4):
        """Crop by alpha first. If alpha is still full-frame, use border-background cleanup."""
        if im_rgba.mode != 'RGBA':
            im_rgba = im_rgba.convert('RGBA')
        bbox = im_rgba.getchannel('A').getbbox()
        if not bbox:
            return im_rgba
        l, t, r, b = bbox
        l = max(0, l - padding)
        t = max(0, t - padding)
        r = min(im_rgba.width, r + padding)
        b = min(im_rgba.height, b + padding)
        return im_rgba.crop((l, t, r, b))

    @staticmethod
    def cleanup_edge_background(im_rgba: Image.Image, color_tolerance=10, min_alpha=8):
        """Remove only border-connected pixels closely matching the studio background."""
        if im_rgba.mode != 'RGBA':
            im_rgba = im_rgba.convert('RGBA')
        arr = np.array(im_rgba, dtype=np.uint8)
        if arr.ndim != 3 or arr.shape[2] != 4:
            return im_rgba
        h, w = arr.shape[:2]
        if h < 4 or w < 4:
            return im_rgba

        rgb = arr[:, :, :3].astype(np.float32)
        alpha = arr[:, :, 3].astype(np.uint8)

        bw = max(1, min(18, min(h, w) // 16))
        strips = [
            rgb[:bw, :, :].reshape(-1, 3),
            rgb[-bw:, :, :].reshape(-1, 3),
            rgb[:, :bw, :].reshape(-1, 3),
            rgb[:, -bw:, :].reshape(-1, 3),
        ]
        border = np.concatenate(strips, axis=0)
        bg = np.median(border, axis=0).astype(np.float32)
        # Gray product fabric is not background merely because it is neutral.
        dist = np.max(np.abs(rgb - bg), axis=2)
        tolerance = min(12.0, max(0.0, float(color_tolerance)))
        candidate = (dist <= tolerance) & (alpha >= min_alpha)

        from collections import deque
        visited = np.zeros((h, w), dtype=np.uint8)
        q = deque()

        def enqueue(x, y):
            if 0 <= x < w and 0 <= y < h and not visited[y, x] and candidate[y, x]:
                visited[y, x] = 1
                q.append((x, y))

        for x in range(w):
            enqueue(x, 0)
            enqueue(x, h - 1)
        for y in range(h):
            enqueue(0, y)
            enqueue(w - 1, y)

        while q:
            x, y = q.popleft()
            for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1), (x - 1, y - 1), (x + 1, y - 1), (x - 1, y + 1), (x + 1, y + 1)):
                if 0 <= nx < w and 0 <= ny < h and not visited[ny, nx] and candidate[ny, nx]:
                    visited[ny, nx] = 1
                    q.append((nx, ny))

        # Never dilate the removal mask into straps, seams, or pale fabric.
        arr[:, :, 3][visited.astype(bool)] = 0
        return Image.fromarray(arr, mode='RGBA')

    @staticmethod
    def has_uniform_background(im):
        rgb = np.asarray(im.convert('RGB'), dtype=np.float32)
        border = np.concatenate((rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]))
        bg = np.median(border, axis=0)
        close = np.max(np.abs(border - bg), axis=1) <= 8
        return bool(close.mean() >= 0.75 and bg.min() >= 150
                    and bg.max() - bg.min() <= 12)

    @staticmethod
    def remove_bg(im_rgba: Image.Image):
        if not REMBG_AVAILABLE:
            return im_rgba
        buf = io.BytesIO()
        im_rgba.save(buf, format='PNG')
        data = buf.getvalue()
        try:
            out_bytes = rembg_remove(
                data,
                alpha_matting=True,
                alpha_matting_foreground_threshold=240,
                alpha_matting_background_threshold=10,
                alpha_matting_erode_size=8,
                post_process_mask=True,
            )
        except Exception:
            out_bytes = rembg_remove(data)
        return Image.open(io.BytesIO(out_bytes)).convert('RGBA')

    @staticmethod
    def add_soft_shadow(base_rgba: Image.Image, shadow_alpha=55, blur_radius=14, offset=(0, 12)):
        alpha = base_rgba.getchannel('A')
        if alpha.getbbox() is None:
            return base_rgba
        shadow_mask = alpha.filter(ImageFilter.GaussianBlur(blur_radius))
        shadow = Image.new('RGBA', base_rgba.size, (0, 0, 0, 0))
        shadow_color = Image.new('RGBA', base_rgba.size, (0, 0, 0, shadow_alpha))
        shadow.paste(shadow_color, offset, shadow_mask)
        out = Image.new('RGBA', base_rgba.size, (255, 255, 255, 0))
        out.alpha_composite(shadow)
        out.alpha_composite(base_rgba)
        return out

    def process_rgba(self, src_path, remove_bg=True, trim_white=True, threshold=245):
        with Image.open(src_path) as im:
            im = ImageOps.exif_transpose(im).convert('RGBA')

        if remove_bg:
            if self.has_uniform_background(im):
                # Flat studio backgrounds need no semantic mask: preserve pale
                # handles and all non-background pixels from the original.
                im = self.cleanup_edge_background(im)
            elif self.use_rembg:
                # Always give the model the intact original, with no gray erasure.
                im = self.remove_bg(im)
            else:
                im = self.cleanup_edge_background(im)

        if trim_white:
            im = self.trim_transparent_or_white(im, threshold=threshold, padding=4)
        return im

    def make_square(self, im_rgba, size=1600, fill_ratio=0.92, add_shadow=False, bg_color=(255, 255, 255)):
        """Normalize every subject so its longest visible side occupies the same target ratio."""
        if im_rgba.mode != 'RGBA':
            im_rgba = im_rgba.convert('RGBA')
        bbox = im_rgba.getchannel('A').getbbox()
        if bbox:
            im_rgba = im_rgba.crop(bbox)

        canvas = Image.new('RGBA', (size, size), bg_color + (255,))
        subject = im_rgba.copy()
        target = max(1, int(size * fill_ratio))
        # Because subject is cropped to alpha bbox first, this makes visible size consistent.
        scale = min(target / max(1, subject.width), target / max(1, subject.height))
        new_w = max(1, int(round(subject.width * scale)))
        new_h = max(1, int(round(subject.height * scale)))
        subject = subject.resize((new_w, new_h), Image.Resampling.LANCZOS)

        layer = Image.new('RGBA', (size, size), (255, 255, 255, 0))
        px = (size - subject.width) // 2
        py = (size - subject.height) // 2
        layer.alpha_composite(subject, (px, py))
        if add_shadow:
            layer = self.add_soft_shadow(layer, blur_radius=max(8, size // 110), offset=(0, max(6, size // 120)))
        canvas.alpha_composite(layer)
        return canvas.convert('RGB')

    def process_and_save(self, src_path, square_sizes=(1000, 1600), remove_bg=True, trim_white=True, threshold=245, fill_ratio=0.92, add_shadow=False, save_base_dir=None, preloaded_rgba=None, filename_override=None):
        rgba = preloaded_rgba if preloaded_rgba is not None else self.process_rgba(src_path, remove_bg=remove_bg, trim_white=trim_white, threshold=threshold)
        src = Path(src_path)
        if save_base_dir is None:
            save_base_dir = str(src.parent)
        stem = sanitize_filename(filename_override or src.stem)
        outputs = {}
        for size in square_sizes:
            out_im = self.make_square(rgba, size=size, fill_ratio=fill_ratio, add_shadow=add_shadow)
            out_dir = Path(save_base_dir) / f'square_{size}'
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f'{stem}_{size}.png'
            out_im.save(out_path, format='PNG', optimize=True)
            outputs[size] = str(out_path)
        return outputs


class BrushEditor:
    def __init__(self, parent, original_rgba, initial_rgba, on_save):
        self.parent = parent
        self.original_rgba = original_rgba.copy()
        self.edit_rgba = initial_rgba.copy()
        self.on_save = on_save
        self.top = tk.Toplevel(parent)
        self.top.title('수동 브러시 보정')
        self.top.geometry('1180x820')
        self.top.transient(parent)
        self.mode_var = tk.StringVar(value='erase')
        self.brush_size_var = tk.IntVar(value=24)
        self.zoom_factor = 1.0
        self.scale = 1.0
        self.fit_scale = 1.0
        self.offset = (0, 0)
        self.pan_x = 0
        self.pan_y = 0
        self.display_photo = None
        self.last_xy = None
        self.undo_stack = []
        self.redo_stack = []
        self.max_undo = 20
        self.zoom_text_var = tk.StringVar(value='100%')
        self.is_panning = False
        self.pan_anchor = None
        self._build_ui()
        self.top.bind('<Control-z>', lambda e: self.undo())
        self.top.bind('<Control-y>', lambda e: self.redo())
        self.top.bind('<MouseWheel>', self.on_mousewheel)
        self.canvas.bind('<MouseWheel>', self.on_mousewheel)
        self.canvas.bind('<Button-4>', lambda e: self.zoom_in())
        self.canvas.bind('<Button-5>', lambda e: self.zoom_out())
        self.canvas.bind('<Button-3>', self.on_pan_start)
        self.canvas.bind('<B3-Motion>', self.on_pan_drag)
        self.canvas.bind('<ButtonRelease-3>', self.on_pan_end)
        self.canvas.bind('<Button-2>', self.on_pan_start)
        self.canvas.bind('<B2-Motion>', self.on_pan_drag)
        self.canvas.bind('<ButtonRelease-2>', self.on_pan_end)
        self.render()

    def _build_ui(self):
        wrap = ttk.Frame(self.top, padding=10)
        wrap.pack(fill='both', expand=True)
        topbar = ttk.Frame(wrap)
        topbar.pack(fill='x', pady=(0, 8))
        ttk.Radiobutton(topbar, text='지우기', variable=self.mode_var, value='erase').pack(side='left')
        ttk.Radiobutton(topbar, text='복원', variable=self.mode_var, value='restore').pack(side='left', padx=(8, 0))
        ttk.Label(topbar, text='브러시 크기').pack(side='left', padx=(18, 6))
        ttk.Scale(topbar, from_=4, to=120, variable=self.brush_size_var, orient='horizontal', length=180).pack(side='left')
        ttk.Button(topbar, text='Undo', command=self.undo).pack(side='left', padx=(14, 0))
        ttk.Button(topbar, text='Redo', command=self.redo).pack(side='left', padx=(4, 0))
        ttk.Button(topbar, text='축소', command=self.zoom_out).pack(side='left', padx=(14, 0))
        ttk.Button(topbar, text='확대', command=self.zoom_in).pack(side='left', padx=(4, 0))
        ttk.Button(topbar, text='맞춤', command=self.zoom_fit).pack(side='left', padx=(4, 0))
        ttk.Button(topbar, text='원점', command=self.pan_reset).pack(side='left', padx=(4, 0))
        ttk.Label(topbar, textvariable=self.zoom_text_var).pack(side='left', padx=(8, 0))
        ttk.Button(topbar, text='원본 누끼 상태로 되돌리기', command=self.reset_to_initial).pack(side='left', padx=(18, 0))
        ttk.Button(topbar, text='저장', command=self.save).pack(side='right')
        self.canvas = tk.Canvas(wrap, bg='#cfcfcf', cursor='crosshair')
        self.canvas.pack(fill='both', expand=True)
        self.canvas.bind('<Configure>', lambda e: self.render())
        self.canvas.bind('<Button-1>', self.on_down)
        self.canvas.bind('<B1-Motion>', self.on_drag)
        self.canvas.bind('<ButtonRelease-1>', self.on_up)
        ttk.Label(wrap, text='왼쪽 드래그: 브러시 보정 · 오른쪽/가운데 드래그: 화면 이동(pan) · 마우스 휠: 확대/축소 · Ctrl+Z/Ctrl+Y', foreground='#555555').pack(anchor='w', pady=(6, 0))

    def push_undo(self):
        self.undo_stack.append(self.edit_rgba.copy())
        if len(self.undo_stack) > self.max_undo:
            self.undo_stack.pop(0)
        self.redo_stack.clear()

    def undo(self):
        if not self.undo_stack:
            return
        self.redo_stack.append(self.edit_rgba.copy())
        self.edit_rgba = self.undo_stack.pop()
        self.render()

    def redo(self):
        if not self.redo_stack:
            return
        self.undo_stack.append(self.edit_rgba.copy())
        if len(self.undo_stack) > self.max_undo:
            self.undo_stack.pop(0)
        self.edit_rgba = self.redo_stack.pop()
        self.render()

    def zoom_in(self):
        self.zoom_factor = min(12.0, self.zoom_factor * 1.2)
        self.render()

    def zoom_out(self):
        self.zoom_factor = max(0.2, self.zoom_factor / 1.2)
        self.render()

    def zoom_fit(self):
        self.zoom_factor = 1.0
        self.pan_reset()
        self.render()

    def pan_reset(self):
        self.pan_x = 0
        self.pan_y = 0
        self.render()

    def on_mousewheel(self, event):
        delta = getattr(event, 'delta', 0)
        if delta > 0:
            self.zoom_in()
        elif delta < 0:
            self.zoom_out()

    def reset_to_initial(self):
        self.push_undo()
        self.edit_rgba = self.original_rgba.copy()
        self.render()

    def _preview_image(self):
        bg = Image.new('RGBA', self.edit_rgba.size, (255, 255, 255, 255))
        bg.alpha_composite(self.edit_rgba)
        return bg.convert('RGB')

    def render(self):
        cw = max(200, self.canvas.winfo_width())
        ch = max(200, self.canvas.winfo_height())
        prev = self._preview_image()
        self.fit_scale = min((cw - 20) / prev.width, (ch - 20) / prev.height)
        self.fit_scale = max(0.02, self.fit_scale)
        scale = max(0.02, self.fit_scale * self.zoom_factor)
        new_w = max(1, int(prev.width * scale))
        new_h = max(1, int(prev.height * scale))
        resized = prev.resize((new_w, new_h), Image.Resampling.LANCZOS)
        self.display_photo = ImageTk.PhotoImage(resized)
        self.canvas.delete('all')
        base_ox = (cw - new_w) // 2
        base_oy = (ch - new_h) // 2
        ox = base_ox + int(self.pan_x)
        oy = base_oy + int(self.pan_y)
        self.canvas.create_image(ox, oy, image=self.display_photo, anchor='nw')
        self.scale = scale
        self.offset = (ox, oy)
        self.zoom_text_var.set(f'{int(scale * 100)}%')

    def _canvas_to_image(self, x, y):
        ox, oy = self.offset
        return int((x - ox) / self.scale), int((y - oy) / self.scale)

    def apply_brush_line(self, p1, p2):
        ix1, iy1 = p1
        ix2, iy2 = p2
        radius = max(2, int(self.brush_size_var.get()))
        if self.mode_var.get() == 'erase':
            alpha = self.edit_rgba.getchannel('A')
            draw = ImageDraw.Draw(alpha)
            draw.line((ix1, iy1, ix2, iy2), fill=0, width=radius)
            draw.ellipse((ix2 - radius//2, iy2 - radius//2, ix2 + radius//2, iy2 + radius//2), fill=0)
            self.edit_rgba.putalpha(alpha)
        else:
            mask = Image.new('L', self.edit_rgba.size, 0)
            mdraw = ImageDraw.Draw(mask)
            mdraw.line((ix1, iy1, ix2, iy2), fill=255, width=radius)
            mdraw.ellipse((ix2 - radius//2, iy2 - radius//2, ix2 + radius//2, iy2 + radius//2), fill=255)
            self.edit_rgba = Image.composite(self.original_rgba, self.edit_rgba, mask)

    def on_down(self, event):
        if self.is_panning:
            return
        self.push_undo()
        self.last_xy = self._canvas_to_image(event.x, event.y)
        self.apply_brush_line(self.last_xy, self.last_xy)
        self.render()

    def on_drag(self, event):
        if self.last_xy is None or self.is_panning:
            return
        cur = self._canvas_to_image(event.x, event.y)
        self.apply_brush_line(self.last_xy, cur)
        self.last_xy = cur
        self.render()

    def on_up(self, event):
        self.last_xy = None

    def on_pan_start(self, event):
        self.is_panning = True
        self.pan_anchor = (event.x, event.y, self.pan_x, self.pan_y)
        self.canvas.configure(cursor='fleur')

    def on_pan_drag(self, event):
        if not self.pan_anchor:
            return
        sx, sy, base_x, base_y = self.pan_anchor
        self.pan_x = base_x + (event.x - sx)
        self.pan_y = base_y + (event.y - sy)
        self.render()

    def on_pan_end(self, event):
        self.is_panning = False
        self.pan_anchor = None
        self.canvas.configure(cursor='crosshair')

    def save(self):
        self.on_save(self.edit_rgba)
        self.top.destroy()


class PreviewPopup:

    def __init__(self, parent, title, before_im, after_im, on_save=None, on_add=None, on_brush=None):
        self.top = tk.Toplevel(parent)
        self.top.title(title)
        self.top.geometry('1100x680')
        self.top.transient(parent)
        self.before_im = before_im.copy()
        self.after_im = after_im.copy()
        self.before_photo = None
        self.after_photo = None
        self.on_save = on_save
        self.on_add = on_add
        self.on_brush = on_brush
        wrap = ttk.Frame(self.top, padding=10)
        wrap.pack(fill='both', expand=True)
        head = ttk.Frame(wrap)
        head.pack(fill='x', pady=(0, 8))
        if on_brush:
            ttk.Button(head, text='브러시 보정 열기', command=self.on_brush).pack(side='left')
        if on_add:
            ttk.Button(head, text='결과를 목록에 추가', command=self.on_add).pack(side='right')
        if on_save:
            ttk.Button(head, text='결과 저장', command=self.on_save).pack(side='right', padx=(0, 6))
        body = ttk.Frame(wrap)
        body.pack(fill='both', expand=True)
        left = ttk.LabelFrame(body, text='원본', padding=8)
        left.pack(side='left', fill='both', expand=True, padx=(0, 6))
        right = ttk.LabelFrame(body, text='처리 결과', padding=8)
        right.pack(side='left', fill='both', expand=True)
        self.before_label = tk.Label(left, bg='#dddddd')
        self.before_label.pack(fill='both', expand=True)
        self.after_label = tk.Label(right, bg='#dddddd')
        self.after_label.pack(fill='both', expand=True)
        self.before_label.bind('<Configure>', lambda e: self.render())
        self.after_label.bind('<Configure>', lambda e: self.render())
        self.render()

    def _fit(self, im, w, h):
        prev = im.copy()
        prev.thumbnail((max(50, w - 10), max(50, h - 10)), Image.Resampling.LANCZOS)
        return ImageTk.PhotoImage(prev)

    def render(self):
        bw = max(120, self.before_label.winfo_width())
        bh = max(120, self.before_label.winfo_height())
        aw = max(120, self.after_label.winfo_width())
        ah = max(120, self.after_label.winfo_height())
        self.before_photo = self._fit(self.before_im, bw, bh)
        self.after_photo = self._fit(self.after_im, aw, ah)
        self.before_label.config(image=self.before_photo)
        self.after_label.config(image=self.after_photo)


class App:
    def __init__(self, root):
        self.root = root
        self.root.title('상품 이미지 누끼 · 리사이즈 · 콜라주 ver 1.13')
        self.root.geometry('1400x920')
        self.root.minsize(1220, 800)
        self.processor = ImageProcessor(use_rembg=True)
        self.image_paths = []
        self.imported_images = {}
        self.preview_photo = None
        self.preview_job = None
        self.collage_offsets = {}
        self.collage_scales = {}
        self.collage_selected_index = None
        self.collage_drag_start = None
        self.collage_resize_start = None
        self.collage_interaction_mode = None
        self.preview_scale = 1.0
        self.preview_origin = (0, 0)
        self.preview_cell_rects = []
        self.preview_image_rects = []
        self.preview_resize_handles = []
        self.last_manual_processed_path = None
        self.last_manual_rgba = None
        self.last_manual_src_path = None
        self.canvas_w_var = tk.StringVar(value='1600')
        self.canvas_h_var = tk.StringVar(value='1600')
        self.margin_var = tk.StringVar(value='35')
        self.gap_var = tk.StringVar(value='24')
        self.jpg_quality_var = tk.StringVar(value='95')
        self.status_var = tk.StringVar(value='준비됨')
        self.count_var = tk.StringVar(value='현재 이미지: 0개')
        self.output_root_var = tk.StringVar(value=str(Path.cwd() / 'image_output'))
        self.remove_bg_var = tk.BooleanVar(value=True)
        self.trim_white_var = tk.BooleanVar(value=True)
        self.threshold_var = tk.StringVar(value='245')
        self.fill_ratio_var = tk.StringVar(value='0.92')
        self.add_shadow_var = tk.BooleanVar(value=False)
        self.normalize_collage_var = tk.BooleanVar(value=True)
        self.normalize_selected_size_var = tk.StringVar(value='1600')
        self.auto_process_import_var = tk.BooleanVar(value=True)
        self.auto_process_size_var = tk.StringVar(value='1600')
        self.auto_process_remove_bg_var = tk.BooleanVar(value=True)
        self._build_ui()
        self._bind_shortcuts()

    # UI BUILDERS
    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill='both', expand=True)
        top = ttk.LabelFrame(outer, text='콜라주 설정', padding=10)
        top.pack(fill='x', pady=(0, 8))
        ttk.Label(top, text='가로(px)').grid(row=0, column=0, sticky='w')
        ttk.Entry(top, textvariable=self.canvas_w_var, width=9).grid(row=0, column=1, padx=(6, 16))
        ttk.Label(top, text='세로(px)').grid(row=0, column=2, sticky='w')
        ttk.Entry(top, textvariable=self.canvas_h_var, width=9).grid(row=0, column=3, padx=(6, 16))
        ttk.Label(top, text='바깥 여백(px)').grid(row=0, column=4, sticky='w')
        ttk.Entry(top, textvariable=self.margin_var, width=8).grid(row=0, column=5, padx=(6, 16))
        ttk.Label(top, text='이미지 간격(px)').grid(row=0, column=6, sticky='w')
        ttk.Entry(top, textvariable=self.gap_var, width=8).grid(row=0, column=7, padx=(6, 16))
        ttk.Label(top, text='JPG 품질').grid(row=0, column=8, sticky='w')
        ttk.Entry(top, textvariable=self.jpg_quality_var, width=6).grid(row=0, column=9, padx=(6, 16))
        ttk.Button(top, text='1600×1600 기본값', command=self.reset_defaults).grid(row=0, column=10, padx=(6, 0))

        main = ttk.Frame(outer)
        main.pack(fill='both', expand=True)
        left = ttk.Notebook(main)
        self.main_notebook = left
        left.pack(side='left', fill='y', padx=(0, 8))
        tab_list = ttk.Frame(left)
        tab_manual = ttk.Frame(left)
        left.add(tab_list, text='이미지 목록')
        left.add(tab_manual, text='배경제거 보정')
        self._build_list_tab(tab_list)
        self._build_manual_tab(tab_manual)
        right = ttk.LabelFrame(main, text='콜라주 미리보기', padding=8)
        right.pack(side='left', fill='both', expand=True)
        bar = ttk.Frame(right)
        bar.pack(fill='x', pady=(0, 6))
        ttk.Button(bar, text='미리보기 새로고침', command=self.refresh_preview).pack(side='left')
        ttk.Button(bar, text='콜라주 저장', command=self.save_collage_dialog).pack(side='left', padx=6)
        resets = ttk.Frame(right)
        resets.pack(fill='x', pady=(0, 6))
        ttk.Button(resets, text='선택 위치 초기화', command=self.reset_selected_collage_position).pack(side='left')
        ttk.Button(resets, text='선택 크기 초기화', command=self.reset_selected_collage_scale).pack(side='left', padx=4)
        self.preview_frame = tk.Frame(right, bg='#dddddd', relief='sunken', bd=1)
        self.preview_frame.pack(fill='both', expand=True)
        self.preview_canvas = tk.Canvas(self.preview_frame, bg='#dddddd', highlightthickness=0, width=300, height=300)
        self.preview_canvas.pack(fill='both', expand=True)
        self.preview_canvas.bind('<Configure>', lambda e: self.refresh_preview(deferred=True))
        self.preview_canvas.bind('<ButtonPress-1>', self.on_collage_mouse_down)
        self.preview_canvas.bind('<B1-Motion>', self.on_collage_mouse_drag)
        self.preview_canvas.bind('<ButtonRelease-1>', self.on_collage_mouse_up)
        self.preview_canvas.bind('<MouseWheel>', self.on_collage_mouse_wheel)
        self.preview_canvas.bind('<Button-4>', lambda e: self.on_collage_mouse_wheel(e, 1))
        self.preview_canvas.bind('<Button-5>', lambda e: self.on_collage_mouse_wheel(e, -1))
        bottom = ttk.Frame(outer)
        bottom.pack(fill='x', pady=(8, 0))
        ttk.Label(bottom, textvariable=self.status_var).pack(side='left')
        ttk.Label(bottom, textvariable=self.count_var).pack(side='right')

    def _build_list_tab(self, parent):
        frame = ttk.LabelFrame(parent, text='콜라주에 사용할 이미지 목록', padding=8)
        frame.pack(fill='both', expand=True, padx=8, pady=8)
        tools = ttk.Frame(frame)
        tools.pack(fill='x', pady=(0, 6))
        ttk.Button(tools, text='이미지 추가', command=self.add_images).pack(side='left')
        ttk.Button(tools, text='폴더 추가', command=self.add_folder).pack(side='left', padx=4)
        ttk.Button(tools, text='선택 이미지 보정', command=self.preview_selected_processing).pack(side='left', padx=(14,4))
        ttk.Button(tools, text='선택 이미지 리사이즈', command=self.resize_selected_images).pack(side='left', padx=4)
        self.listbox = tk.Listbox(frame, width=50, height=28, selectmode=tk.EXTENDED, activestyle='dotbox')
        self.listbox.pack(fill='both', expand=True)
        self.listbox.bind('<<ListboxSelect>>', lambda e: self.sync_manual_selected_label())
        if DND_AVAILABLE:
            self.listbox.drop_target_register(DND_FILES)
            self.listbox.dnd_bind('<<Drop>>', self.on_drop)
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=6)
        ttk.Button(row, text='▲ 위로', command=lambda: self.move_selected(-1)).pack(side='left')
        ttk.Button(row, text='▼ 아래로', command=lambda: self.move_selected(1)).pack(side='left', padx=4)
        ttk.Button(row, text='선택 삭제', command=self.remove_selected).pack(side='left', padx=(12, 4))
        ttk.Button(row, text='전체 삭제', command=self.clear_all).pack(side='left')
        hint = '드래그앤드롭 지원' if DND_AVAILABLE else '드래그앤드롭: tkinterdnd2 설치 시 지원'
        ttk.Label(frame, text=hint, foreground='#666666').pack(anchor='w')
        norm = ttk.Frame(frame)
        norm.pack(fill='x', pady=(6,0))
        ttk.Checkbutton(norm, text='콜라주에서 상품 크기 자동 통일', variable=self.normalize_collage_var, command=self.refresh_preview).pack(side='left')
        ttk.Label(norm, text='선택 리사이즈 크기').pack(side='left', padx=(16,6))
        ttk.Combobox(norm, textvariable=self.normalize_selected_size_var, values=['1000','1600'], width=8, state='readonly').pack(side='left')

        autoimp = ttk.LabelFrame(frame, text='이미지 추가 시 자동 처리', padding=8)
        autoimp.pack(fill='x', pady=(8,0))
        ttk.Label(autoimp, text='자동 크기 통일: 항상 적용', foreground='#006400').pack(side='left')
        ttk.Checkbutton(autoimp, text='자동 누끼 제거', variable=self.auto_process_remove_bg_var).pack(side='left', padx=(14,0))
        ttk.Label(autoimp, text='자동 처리 크기').pack(side='left', padx=(16,6))
        ttk.Combobox(autoimp, textvariable=self.auto_process_size_var, values=['1000','1600'], width=8, state='readonly').pack(side='left')
        ttk.Label(autoimp, text='※ 실패한 이미지는 배경제거 보정 탭에서 수동 보정 가능', foreground='#555555').pack(side='left', padx=(16,0))


    def _build_manual_tab(self, parent):
        frame = ttk.LabelFrame(parent, text='배경제거 / 브러시 보정', padding=8)
        frame.pack(fill='both', expand=True, padx=8, pady=8)
        output = ttk.LabelFrame(frame, text='보정 결과 저장 폴더', padding=6)
        output.pack(fill='x', pady=(0, 8))
        ttk.Entry(output, textvariable=self.output_root_var, width=42).pack(side='left', fill='x', expand=True)
        ttk.Button(output, text='폴더 선택', command=self.choose_output_root).pack(side='left', padx=4)
        self.manual_selected_label = ttk.Label(frame, text='현재 선택: 없음')
        self.manual_selected_label.pack(anchor='w', pady=(0, 8))

        opts = ttk.LabelFrame(frame, text='보정 옵션', padding=8)
        opts.pack(fill='x', pady=(0, 8))
        ttk.Checkbutton(opts, text='배경제거(누끼)', variable=self.remove_bg_var).grid(row=0, column=0, sticky='w')
        ttk.Checkbutton(opts, text='밝은 여백 제거', variable=self.trim_white_var).grid(row=0, column=1, sticky='w', padx=(12,0))
        ttk.Checkbutton(opts, text='자동 그림자 추가', variable=self.add_shadow_var).grid(row=0, column=2, sticky='w', padx=(12,0))
        ttk.Label(opts, text='여백 기준').grid(row=1, column=0, sticky='e', pady=(8,0))
        ttk.Entry(opts, textvariable=self.threshold_var, width=7).grid(row=1, column=1, sticky='w', padx=(6,16), pady=(8,0))
        ttk.Label(opts, text='상품 채움 비율').grid(row=1, column=2, sticky='e', pady=(8,0))
        ttk.Entry(opts, textvariable=self.fill_ratio_var, width=7).grid(row=1, column=3, sticky='w', padx=(6,16), pady=(8,0))
        self.manual_size_var = tk.StringVar(value='1600')
        ttk.Label(opts, text='저장 크기').grid(row=1, column=4, sticky='e', pady=(8,0))
        ttk.Combobox(opts, textvariable=self.manual_size_var, values=['1000','1600'], width=8, state='readonly').grid(row=1, column=5, sticky='w', pady=(8,0))

        actions = ttk.LabelFrame(frame, text='보정 작업', padding=8)
        actions.pack(fill='x', pady=(0,8))
        ttk.Button(actions, text='1. 자동 누끼 미리보기', command=self.preview_selected_processing, width=24).grid(row=0,column=0,padx=4,pady=4)
        ttk.Button(actions, text='2. 브러시 보정 열기', command=self.open_brush_for_selected, width=24).grid(row=0,column=1,padx=4,pady=4)
        ttk.Button(actions, text='3. 재처리 저장', command=self.manual_reprocess_selected, width=24).grid(row=1,column=0,padx=4,pady=4)
        ttk.Button(actions, text='4. 결과 목록에 추가', command=self.add_last_manual_to_list, width=24).grid(row=1,column=1,padx=4,pady=4)
        ttk.Button(actions, text='선택 이미지 리사이즈', command=self.resize_selected_images, width=24).grid(row=2,column=0,padx=4,pady=4)

        ttk.Label(frame, text='브러시 창: 왼쪽 드래그=지우기/복원, 오른쪽/가운데 드래그=화면 이동, 휠=확대/축소, Ctrl+Z=Undo, Ctrl+Y=Redo', foreground='#555555', wraplength=520, justify='left').pack(anchor='w')


    # BASIC ACTIONS


    def _bind_shortcuts(self):
        self.root.bind('<Delete>', lambda e: self.remove_selected())
        self.root.bind('<Control-o>', lambda e: self.add_images())
        self.root.bind('<Control-s>', lambda e: self.save_collage_dialog())

    def log(self, msg):
        self.status_var.set(msg)

    def choose_output_root(self):
        folder = filedialog.askdirectory(title='출력 루트 폴더 선택')
        if folder:
            self.output_root_var.set(folder)

    def reset_defaults(self):
        self.canvas_w_var.set('1600')
        self.canvas_h_var.set('1600')
        self.margin_var.set('35')
        self.gap_var.set('24')
        self.jpg_quality_var.set('95')
        self.threshold_var.set('245')
        self.fill_ratio_var.set('0.92')
        self.refresh_preview()

    def add_images(self):
        files = filedialog.askopenfilenames(title='이미지 선택', filetypes=[('이미지 파일', '*.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff'), ('모든 파일', '*.*')])
        self._append_paths(files)

    def add_folder(self):
        folder = filedialog.askdirectory(title='이미지 폴더 선택')
        if not folder:
            return
        files = [str(p) for p in sorted(Path(folder).iterdir()) if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS]
        self._append_paths(files)

    def on_drop(self, event):
        self._append_paths(normalize_drop_paths(event.data))

    def _append_paths(self, paths, preprocess=True):
        existing = set(self.image_paths)
        added = 0
        for p in paths:
            p = os.path.abspath(str(p))
            if not os.path.isfile(p):
                continue
            if Path(p).suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            if p in existing:
                continue
            if preprocess:
                try:
                    self.status_var.set(f'이미지 자동 처리 중: {os.path.basename(p)}')
                    self.root.update_idletasks()
                    rgba = self.processor.process_rgba(
                        p, remove_bg=self.auto_process_remove_bg_var.get(),
                        trim_white=True, threshold=int(self.threshold_var.get()))
                    size = int(self.auto_process_size_var.get())
                    self.imported_images[p] = self.processor.make_square(
                        rgba, size=size, fill_ratio=float(self.fill_ratio_var.get()),
                        add_shadow=False).convert('RGBA')
                except Exception as e:
                    self.log(f'자동 처리 실패 ({os.path.basename(p)}): {e} — 원본 사용')
            self.image_paths.append(p)
            existing.add(p)
            self.listbox.insert(tk.END, os.path.basename(p))
            added += 1
        self._update_count()
        if added:
            self.status_var.set(f'{added}개 이미지 추가')
            self.refresh_preview()
            self.sync_manual_selected_label()

    def remove_selected(self):
        sel = list(self.listbox.curselection())
        if not sel:
            return
        for idx in reversed(sel):
            self.listbox.delete(idx)
            self.imported_images.pop(self.image_paths[idx], None)
            del self.image_paths[idx]
        self.collage_offsets.clear()
        self.collage_scales.clear()
        self.collage_selected_index = None
        self._update_count()
        self.refresh_preview()
        self.sync_manual_selected_label()

    def clear_all(self):
        if not self.image_paths:
            return
        if not messagebox.askyesno('전체 삭제', '이미지 목록을 모두 비울까요?'):
            return
        self.image_paths = []
        self.imported_images.clear()
        self.collage_offsets.clear()
        self.collage_scales.clear()
        self.collage_selected_index = None
        self.listbox.delete(0, tk.END)
        self._update_count()
        try:
            self.preview_canvas.delete('all')
        except Exception:
            pass
        self.preview_photo = None
        self.refresh_preview()
        self.status_var.set('목록 비움')
        self.sync_manual_selected_label()

    def move_selected(self, direction):
        sel = list(self.listbox.curselection())
        if len(sel) != 1:
            messagebox.showinfo('순서 변경', '한 번에 하나만 선택해 주세요.')
            return
        idx = sel[0]
        new_idx = idx + direction
        if new_idx < 0 or new_idx >= len(self.image_paths):
            return
        self.image_paths[idx], self.image_paths[new_idx] = self.image_paths[new_idx], self.image_paths[idx]
        items = list(self.listbox.get(0, tk.END))
        items[idx], items[new_idx] = items[new_idx], items[idx]
        self.listbox.delete(0, tk.END)
        for item in items:
            self.listbox.insert(tk.END, item)
        self.listbox.selection_set(new_idx)
        self.listbox.activate(new_idx)
        self.refresh_preview()
        self.sync_manual_selected_label()

    def _update_count(self):
        self.count_var.set(f'현재 이미지: {len(self.image_paths)}개')

    def sync_manual_selected_label(self):
        sel = list(self.listbox.curselection())
        if len(sel) == 1 and 0 <= sel[0] < len(self.image_paths):
            self.manual_selected_label.config(text=f'현재 선택: {os.path.basename(self.image_paths[sel[0]])}')
        else:
            self.manual_selected_label.config(text='현재 선택: 없음')

    @staticmethod
    def _positive_int(value, label, minimum=1):
        try:
            v = int(value)
        except Exception:
            raise ValueError(f'{label}은(는) 정수여야 합니다.')
        if v < minimum:
            raise ValueError(f'{label}은(는) {minimum} 이상이어야 합니다.')
        return v

    @staticmethod
    def _float_range(value, label, minimum=0.0, maximum=1.0):
        try:
            v = float(value)
        except Exception:
            raise ValueError(f'{label}은(는) 숫자여야 합니다.')
        if not (minimum <= v <= maximum):
            raise ValueError(f'{label}은(는) {minimum}~{maximum} 범위여야 합니다.')
        return v

    def choose_grid(self, n, canvas_w, canvas_h):
        if n <= 0:
            return 0, 0
        best = None
        target_aspect = canvas_w / max(canvas_h, 1)
        for cols in range(1, n + 1):
            rows = math.ceil(n / cols)
            cell_aspect = (canvas_w / cols) / max(canvas_h / rows, 1e-9)
            empties = rows * cols - n
            score = abs(math.log(max(cell_aspect, 1e-9))) + empties * 0.05
            grid_aspect = cols / rows
            score += abs(math.log(max(grid_aspect / target_aspect, 1e-9))) * 0.15
            cand = (score, rows, cols)
            if best is None or cand < best:
                best = cand
        return best[1], best[2]

    def build_collage(self, preview_max=None):
        if not self.image_paths:
            raise ValueError('먼저 이미지를 추가하세요.')
        w = self._positive_int(self.canvas_w_var.get(), '가로', 100)
        h = self._positive_int(self.canvas_h_var.get(), '세로', 100)
        margin = self._positive_int(self.margin_var.get(), '바깥 여백', 0)
        gap = self._positive_int(self.gap_var.get(), '이미지 간격', 0)
        rows, cols = self.choose_grid(len(self.image_paths), w, h)
        usable_w = w - margin * 2 - gap * (cols - 1)
        usable_h = h - margin * 2 - gap * (rows - 1)
        if usable_w <= 0 or usable_h <= 0:
            raise ValueError('여백/간격이 너무 큽니다.')
        cell_w = usable_w / cols
        cell_h = usable_h / rows
        canvas = Image.new('RGB', (w, h), 'white')
        self._layout_cache = []
        for i, path in enumerate(self.image_paths):
            r = i // cols
            c = i % cols
            x0 = margin + c * (cell_w + gap)
            y0 = margin + r * (cell_h + gap)
            with Image.open(path) as im:
                im = ImageOps.exif_transpose(im).convert('RGBA')
                if path in self.imported_images:
                    im = self.imported_images[path].copy()
                if self.normalize_collage_var.get():
                    im = self._trim_for_collage(im)
                inner_pad = max(4, int(min(cell_w, cell_h) * 0.03))
                target_w = max(1, int(cell_w) - inner_pad * 2)
                target_h = max(1, int(cell_h) - inner_pad * 2)
                base_im = im.copy()
                base_im.thumbnail((target_w, target_h), Image.Resampling.LANCZOS)
                scale_factor = float(self.collage_scales.get(i, 1.0))
                scale_factor = max(0.25, min(3.0, scale_factor))
                if abs(scale_factor - 1.0) > 0.001:
                    new_w = max(1, int(base_im.width * scale_factor))
                    new_h = max(1, int(base_im.height * scale_factor))
                    im = base_im.resize((new_w, new_h), Image.Resampling.LANCZOS)
                else:
                    im = base_im
                offx, offy = self.collage_offsets.get(i, (0, 0))
                px = int(x0 + (cell_w - im.width) / 2 + offx)
                py = int(y0 + (cell_h - im.height) / 2 + offy)
                white = Image.new('RGBA', im.size, (255,255,255,255))
                white.alpha_composite(im)
                canvas.paste(white.convert('RGB'), (px, py))
                self._layout_cache.append({
                    'index': i, 'cell_rect': (x0, y0, x0 + cell_w, y0 + cell_h),
                    'image_rect': (px, py, px + im.width, py + im.height),
                    'base_size': (base_im.width, base_im.height), 'final_size': (im.width, im.height),
                    'scale_factor': scale_factor,
                })
        if preview_max:
            prev = canvas.copy()
            prev.thumbnail(preview_max, Image.Resampling.LANCZOS)
            return prev
        return canvas

    def refresh_preview(self, deferred=False):
        if self.preview_job:
            self.root.after_cancel(self.preview_job)
            self.preview_job = None
        if deferred:
            self.preview_job = self.root.after(180, self._refresh_preview_now)
        else:
            self._refresh_preview_now()

    def _refresh_preview_now(self):
        self.preview_job = None
        self.preview_cell_rects = []
        self.preview_image_rects = []
        self.preview_resize_handles = []
        cw = self.preview_canvas.winfo_width()
        ch = self.preview_canvas.winfo_height()
        if cw <= 1 or ch <= 1:
            self.preview_job = self.root.after(100, self._refresh_preview_now)
            return
        if not self.image_paths:
            self.preview_photo = None
            try:
                self.preview_canvas.delete('all')
                self.preview_canvas.create_text(cw // 2, ch // 2, text='이미지 추가 또는 폴더 추가로\n사진을 넣어 주세요.', fill='#555555', justify='center')
            except Exception:
                pass
            return
        try:
            fw = max(1, cw - 18)
            fh = max(1, ch - 18)
            full_w = self._positive_int(self.canvas_w_var.get(), '가로', 100)
            full_h = self._positive_int(self.canvas_h_var.get(), '세로', 100)
            preview = self.build_collage(preview_max=(fw, fh))
            self.preview_photo = ImageTk.PhotoImage(preview, master=self.preview_canvas)
            self.preview_canvas.delete('all')
            ox = (cw - preview.width) // 2
            oy = (ch - preview.height) // 2
            self.preview_canvas.create_image(ox, oy, image=self.preview_photo, anchor='nw')
            self.preview_scale = min(preview.width / full_w, preview.height / full_h)
            self.preview_origin = (ox, oy)

            # Build clickable rectangles in preview coordinates.
            self.preview_cell_rects = []
            self.preview_image_rects = []
            self.preview_resize_handles = []
            for item in getattr(self, '_layout_cache', []):
                i = item['index']
                x0, y0, x1, y1 = item['cell_rect']
                ix0, iy0, ix1, iy1 = item['image_rect']
                rx0 = ox + x0 * self.preview_scale
                ry0 = oy + y0 * self.preview_scale
                rx1 = ox + x1 * self.preview_scale
                ry1 = oy + y1 * self.preview_scale
                rix0 = ox + ix0 * self.preview_scale
                riy0 = oy + iy0 * self.preview_scale
                rix1 = ox + ix1 * self.preview_scale
                riy1 = oy + iy1 * self.preview_scale
                self.preview_cell_rects.append((rx0, ry0, rx1, ry1))
                self.preview_image_rects.append((rix0, riy0, rix1, riy1))
                hs = 10
                handle = (rix1 - hs, riy1 - hs, rix1 + hs, riy1 + hs)
                self.preview_resize_handles.append(handle)
                if self.collage_selected_index == i:
                    self.preview_canvas.create_rectangle(rx0, ry0, rx1, ry1, outline='#0088ff', width=2)
                    self.preview_canvas.create_rectangle(rix0, riy0, rix1, riy1, outline='#ff6600', width=2)
                    self.preview_canvas.create_rectangle(*handle, fill='#ff6600', outline='white', width=1)
                    scale_txt = f"{self.collage_scales.get(i, 1.0):.2f}x"
                    self.preview_canvas.create_text(rx0+8, ry0+8, text=f'선택 {i+1} / {scale_txt}', anchor='nw', fill='#0088ff')
            self.status_var.set(f'미리보기 완료 · {len(self.image_paths)}개 · 드래그=이동 / 오른쪽 아래 핸들 드래그=크기 조절')
        except Exception as e:
            self.status_var.set(f'미리보기 오류: {e}')
            try:
                self.preview_canvas.delete('all')
                self.preview_canvas.create_text(250, 180, text=f'미리보기 오류\n{e}', fill='red', justify='center')
            except Exception:
                pass

    def _pick_collage_index(self, x, y):
        # Prefer actual image rects for selection.
        for i, rect in enumerate(self.preview_image_rects):
            x0, y0, x1, y1 = rect
            if x0 <= x <= x1 and y0 <= y <= y1:
                return i
        for i, rect in enumerate(self.preview_cell_rects):
            x0, y0, x1, y1 = rect
            if x0 <= x <= x1 and y0 <= y <= y1:
                return i
        return None

    def _pick_resize_handle(self, x, y):
        for i, rect in enumerate(self.preview_resize_handles):
            x0, y0, x1, y1 = rect
            if x0 <= x <= x1 and y0 <= y <= y1:
                return i
        return None

    def on_collage_mouse_down(self, event):
        resize_idx = self._pick_resize_handle(event.x, event.y)
        if resize_idx is not None:
            self.collage_selected_index = resize_idx
            self.collage_interaction_mode = 'resize'
            self.collage_resize_start = (event.x, event.y, float(self.collage_scales.get(resize_idx, 1.0)))
            self.preview_canvas.configure(cursor='bottom_right_corner')
            self.refresh_preview()
            return

        idx = self._pick_collage_index(event.x, event.y)
        self.collage_selected_index = idx
        if idx is not None:
            self.collage_interaction_mode = 'move'
            self.collage_drag_start = (event.x, event.y, self.collage_offsets.get(idx, (0, 0)))
            self.preview_canvas.configure(cursor='fleur')
        else:
            self.collage_interaction_mode = None
        self.refresh_preview()

    def on_collage_mouse_drag(self, event):
        if self.collage_selected_index is None:
            return
        scale = max(0.0001, self.preview_scale)
        if self.collage_interaction_mode == 'move' and self.collage_drag_start:
            sx, sy, (base_x, base_y) = self.collage_drag_start
            dx = (event.x - sx) / scale
            dy = (event.y - sy) / scale
            self.collage_offsets[self.collage_selected_index] = (base_x + dx, base_y + dy)
            self._refresh_preview_now()
        elif self.collage_interaction_mode == 'resize' and self.collage_resize_start:
            sx, sy, base_scale = self.collage_resize_start
            delta = ((event.x - sx) + (event.y - sy)) / 220.0
            new_scale = max(0.25, min(3.0, base_scale + delta))
            self.collage_scales[self.collage_selected_index] = new_scale
            self._refresh_preview_now()

    def on_collage_mouse_up(self, event):
        self.collage_drag_start = None
        self.collage_resize_start = None
        self.collage_interaction_mode = None
        try:
            self.preview_canvas.configure(cursor='hand2')
        except Exception:
            pass

    def on_collage_mouse_wheel(self, event, linux_delta=None):
        idx = self.collage_selected_index
        hover_idx = self._pick_collage_index(getattr(event, 'x', -1), getattr(event, 'y', -1))
        if hover_idx is not None:
            idx = hover_idx
            self.collage_selected_index = hover_idx
        if idx is None:
            return
        delta = linux_delta
        if delta is None:
            raw = getattr(event, 'delta', 0)
            if raw > 0:
                delta = 1
            elif raw < 0:
                delta = -1
            else:
                delta = 0
        current = float(self.collage_scales.get(idx, 1.0))
        if delta > 0:
            new_scale = min(3.0, current * 1.06)
        elif delta < 0:
            new_scale = max(0.25, current / 1.06)
        else:
            return
        self.collage_scales[idx] = new_scale
        self.status_var.set(f'선택 이미지 {idx+1} 크기: {new_scale:.2f}x (마우스 휠 조절)')
        self._refresh_preview_now()

    def reset_selected_collage_position(self):
        if self.collage_selected_index is not None:
            self.collage_offsets[self.collage_selected_index] = (0, 0)
            self.refresh_preview()

    def reset_selected_collage_scale(self):
        if self.collage_selected_index is not None:
            self.collage_scales[self.collage_selected_index] = 1.0
            self.refresh_preview()

    def reset_all_collage_positions(self):
        self.collage_offsets.clear()
        self.collage_scales.clear()
        self.refresh_preview()

    def reset_all_collage_scales(self):
        self.collage_scales.clear()
        self.refresh_preview()

    def save_collage(self, path):
        quality = self._positive_int(self.jpg_quality_var.get(), 'JPG 품질', 1)
        quality = max(1, min(100, quality))
        collage = self.build_collage()
        ext = Path(path).suffix.lower()
        if ext == '.png':
            collage.save(path, format='PNG', optimize=True)
        else:
            if ext not in {'.jpg', '.jpeg'}:
                path += '.jpg'
            collage.save(path, format='JPEG', quality=quality, optimize=True, subsampling=0)
        return path

    def save_collage_dialog(self):
        if not self.image_paths:
            messagebox.showwarning('이미지 없음', '저장할 콜라주 이미지가 없습니다.')
            return
        path = filedialog.asksaveasfilename(title='콜라주 저장', defaultextension='.jpg', initialfile='product_collage_1600x1600.jpg', filetypes=[('JPEG 이미지', '*.jpg'), ('PNG 이미지', '*.png')])
        if not path:
            return
        try:
            saved = self.save_collage(path)
            self.status_var.set(f'저장 완료: {saved}')
            messagebox.showinfo('저장 완료', f'콜라주 저장 완료\n\n{saved}')
        except Exception as e:
            messagebox.showerror('저장 오류', str(e))

    def _current_manual_settings(self):
        threshold = self._positive_int(self.threshold_var.get(), '여백 제거 기준', 0)
        fill_ratio = self._float_range(self.fill_ratio_var.get(), '상품 채움 비율', 0.5, 0.98)
        size = self._positive_int(self.manual_size_var.get(), '저장 크기', 100)
        return threshold, fill_ratio, size

    def _trim_for_collage(self, im):
        im = im.convert('RGBA')
        # Composite on white for near-white detection
        rgb = Image.new('RGB', im.size, 'white')
        rgb.paste(im, mask=im.getchannel('A'))
        threshold = 245
        try:
            threshold = int(self.threshold_var.get())
        except Exception:
            pass
        gray = rgb.convert('L')
        mask = gray.point(lambda p: 255 if p < threshold else 0)
        bbox = mask.getbbox()
        if bbox:
            l,t,r,b = bbox
            pad = max(2, int(min(im.size)*0.005))
            l=max(0,l-pad); t=max(0,t-pad); r=min(im.width,r+pad); b=min(im.height,b+pad)
            return im.crop((l,t,r,b))
        return im

    def resize_selected_images(self):
        sel = list(self.listbox.curselection())
        if not sel:
            messagebox.showwarning('선택 필요','리사이즈할 이미지를 선택해 주세요.')
            return
        try:
            size = int(self.normalize_selected_size_var.get())
            fill_ratio = float(self.fill_ratio_var.get())
        except Exception:
            messagebox.showerror('입력 오류','리사이즈 크기 또는 상품 채움 비율을 확인해 주세요.')
            return
        out_root = Path(self.output_root_var.get().strip()) / 'manual_resized'
        out_root.mkdir(parents=True, exist_ok=True)
        new_paths=[]
        for idx in sel:
            p = self.image_paths[idx]
            with Image.open(p) as im:
                im = ImageOps.exif_transpose(im).convert('RGBA')
            if self.trim_white_var.get():
                im = self._trim_for_collage(im)
            result = self.processor.make_square(im, size=size, fill_ratio=fill_ratio, add_shadow=self.add_shadow_var.get())
            outp = out_root / f'{sanitize_filename(Path(p).stem)}_{size}.png'
            result.save(outp, format='PNG', optimize=True)
            new_paths.append(str(outp))
        self._append_paths(new_paths)
        messagebox.showinfo('리사이즈 완료', f'{len(new_paths)}개 이미지를 {size}x{size}로 생성하고 목록에 추가했습니다.')

    def open_brush_for_selected(self):
        sel = list(self.listbox.curselection())
        if len(sel) != 1:
            messagebox.showwarning('선택 필요','브러시 보정은 이미지 1개를 선택해 주세요.')
            return
        path = self.image_paths[sel[0]]
        try:
            threshold, fill_ratio, size = self._current_manual_settings()
        except Exception as e:
            messagebox.showerror('입력 오류', str(e)); return
        rgba = self.processor.process_rgba(path, remove_bg=self.remove_bg_var.get(), trim_white=self.trim_white_var.get(), threshold=threshold)
        self.last_manual_rgba = rgba.copy(); self.last_manual_src_path = path
        BrushEditor(self.root, rgba, rgba, lambda edited: self._on_brush_saved(edited, path, size, fill_ratio))

    def preview_selected_processing(self):
        sel = list(self.listbox.curselection())
        if len(sel) != 1:
            messagebox.showwarning('선택 필요', '이미지 목록에서 1개 이미지를 선택해 주세요.')
            return
        path = self.image_paths[sel[0]]
        try:
            threshold, fill_ratio, size = self._current_manual_settings()
        except Exception as e:
            messagebox.showerror('입력 오류', str(e))
            return
        with Image.open(path) as before:
            before = ImageOps.exif_transpose(before).convert('RGB')
        rgba = self.processor.process_rgba(path, remove_bg=self.remove_bg_var.get(), trim_white=self.trim_white_var.get(), threshold=threshold)
        result = self.processor.make_square(rgba, size=size, fill_ratio=fill_ratio, add_shadow=self.add_shadow_var.get())
        self.last_manual_rgba = rgba.copy()
        self.last_manual_src_path = path

        def save_cb():
            self._save_manual_result(rgba, path, size, fill_ratio)

        def add_cb():
            saved = self._save_manual_result(rgba, path, size, fill_ratio)
            self._append_paths([saved])

        def brush_cb():
            BrushEditor(self.root, rgba, rgba, lambda edited_rgba: self._on_brush_saved(edited_rgba, path, size, fill_ratio))

        PreviewPopup(self.root, '배경제거 결과 미리보기', before, result, on_save=save_cb, on_add=add_cb, on_brush=brush_cb)

    def _save_manual_result(self, rgba, src_path, size, fill_ratio):
        out_root = Path(self.output_root_var.get().strip())
        out_root.mkdir(parents=True, exist_ok=True)
        out_dir = out_root / 'manual_preview_saves'
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = sanitize_filename(Path(src_path).stem)
        out = out_dir / f'{stem}_{size}_manual.png'
        result = self.processor.make_square(rgba, size=size, fill_ratio=fill_ratio, add_shadow=self.add_shadow_var.get())
        result.save(out, format='PNG', optimize=True)
        self.last_manual_processed_path = str(out)
        return str(out)

    def _on_brush_saved(self, edited_rgba, src_path, size, fill_ratio):
        self.last_manual_rgba = edited_rgba.copy()
        saved = self._save_manual_result(edited_rgba, src_path, size, fill_ratio)
        self.last_manual_processed_path = saved
        messagebox.showinfo('브러시 보정 저장 완료', f'브러시 보정 결과를 저장했습니다.\n\n{saved}')

    def manual_reprocess_selected(self):
        sel = list(self.listbox.curselection())
        if len(sel) != 1:
            messagebox.showwarning('선택 필요', '이미지 목록에서 1개 이미지를 선택해 주세요.')
            return
        path = self.image_paths[sel[0]]
        try:
            threshold, fill_ratio, size = self._current_manual_settings()
        except Exception as e:
            messagebox.showerror('입력 오류', str(e))
            return
        out_root = Path(self.output_root_var.get().strip())
        out_root.mkdir(parents=True, exist_ok=True)
        save_dir = out_root / 'manual_reprocessed'
        save_dir.mkdir(parents=True, exist_ok=True)
        preloaded = self.last_manual_rgba if self.last_manual_src_path == path and self.last_manual_rgba is not None else None
        outputs = self.processor.process_and_save(path, square_sizes=(size,), remove_bg=self.remove_bg_var.get(), trim_white=self.trim_white_var.get(), threshold=threshold, fill_ratio=fill_ratio, add_shadow=self.add_shadow_var.get(), save_base_dir=str(save_dir), preloaded_rgba=preloaded)
        self.last_manual_processed_path = outputs[size]
        messagebox.showinfo('재처리 완료', f'수동 재처리 저장 완료\n\n{outputs[size]}')

    def add_last_manual_to_list(self):
        if not self.last_manual_processed_path or not os.path.exists(self.last_manual_processed_path):
            messagebox.showwarning('없음', '마지막 수동 재처리 결과가 없습니다.')
            return
        self._append_paths([self.last_manual_processed_path])


def main():
    if DND_AVAILABLE:
        root = TkinterDnD.Tk()
    else:
        root = tk.Tk()
    try:
        ttk.Style().theme_use('vista')
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
