#!/usr/bin/env python3
"""Robot de fotos de Regalos Peludos.

1. tienda: descarga todas las fotos de los productos de regalospeludos.shop
   -> tienda/<handle>/foto_N.jpg  +  recorte_N.png (sin fondo)
2. pedidos/*.json: fotos de proveedores u otras URLs
   {"id": "manta-navidad", "producto": "Manta personalizada",
    "imagenes": ["https://..."], "escenas": ["salon", "navidad"]}
   -> salida/<id>/recorte_N.png y escena_<escena>_N.jpg
En ambos casos crea escenas nuevas (fondo distinto) en 1080x1920 listas para vídeo.
"""
import io, json, os, random, sys, time, glob, hashlib, urllib.parse
import requests
from PIL import Image, ImageDraw, ImageFilter, ImageEnhance

TIENDA = os.environ.get("TIENDA", "https://regalospeludos.shop")
W, H = 1080, 1920
UA = {"User-Agent": "Mozilla/5.0 (RegalosPeludosBot)"}

ESCENAS = {
    "salon":   ("cozy modern living room, soft sofa with blankets, warm lamp light, plants, "
                "blurred background, shallow depth of field, empty wooden table in foreground", (238, 226, 210), (196, 168, 140)),
    "navidad": ("christmas living room, decorated christmas tree with warm fairy lights bokeh, "
                "gift boxes, fireplace, blurred background, empty wooden table in foreground", (120, 30, 40), (30, 70, 50)),
    "estudio": ("minimal pastel studio backdrop, soft shadows, product photography background, empty podium", (250, 236, 228), (236, 205, 190)),
    "jardin":  ("sunny garden terrace, green plants, golden hour bokeh, blurred, empty wooden table in foreground", (210, 230, 190), (120, 160, 90)),
    "cama":    ("cozy bedroom, white bed linen, morning sunlight, blurred background, empty bed in foreground", (245, 240, 232), (210, 200, 185)),
    "halloween": ("cozy halloween room, carved pumpkins, warm candle light bokeh, blurred, empty wooden table", (60, 30, 20), (230, 120, 30)),
}
DEFAULT_ESCENAS = ["salon", "navidad", "estudio"]

LOG = []
def log(*a):
    msg = " ".join(str(x) for x in a); print(msg); LOG.append(msg)

def get(url, timeout=60, intentos=3):
    for i in range(intentos):
        try:
            r = requests.get(url, headers=UA, timeout=timeout)
            if r.status_code == 200:
                return r.content
            print("  HTTP", r.status_code, url[:90])
        except Exception as e:
            print("  error", e, url[:90])
        time.sleep(3 * (i + 1))
    return None

_sesiones = {}
def sesion(nombre):
    if nombre not in _sesiones:
        from rembg import new_session
        _sesiones[nombre] = new_session(nombre)
    return _sesiones[nombre]

def fondo_por_bordes(rgb):
    """Fondo claro y liso conectado con los bordes de la foto (mockups sobre blanco)."""
    import numpy as np
    from scipy import ndimage
    a = np.asarray(rgb).astype(float)
    g = a.mean(axis=2)
    gs = ndimage.gaussian_filter(g, 1.0)
    grad = np.hypot(ndimage.sobel(gs, 0), ndimage.sobel(gs, 1)) / 8
    sat = a.max(axis=2) - a.min(axis=2)
    cand = (g >= 205) & (sat <= 18) & (grad < 6)
    lab, _ = ndimage.label(cand)
    borde = set(np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))) - {0}
    bg = np.isin(lab, list(borde))
    return ndimage.binary_closing(bg, iterations=1, border_value=1)

def recortar(img_bytes):
    import numpy as np
    from scipy import ndimage
    from rembg import remove
    orig = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    orig.thumbnail((1280, 1280))
    buf = io.BytesIO(); orig.save(buf, "PNG"); png = buf.getvalue()
    union = np.zeros(orig.size[::-1], bool)
    for modelo in ("isnet-general-use", "u2net"):
        try:
            o = Image.open(io.BytesIO(remove(png, session=sesion(modelo)))).convert("RGBA").resize(orig.size)
            union |= ndimage.binary_fill_holes(np.asarray(o.getchannel("A")) > 60)
        except Exception as e:
            log("  modelo", modelo, "falla:", e)
    fg = union.copy()
    bg = fondo_por_bordes(orig)
    if bg.mean() > 0.15:  # foto sobre fondo blanco: completa partes blancas del producto
        cerca = ndimage.binary_dilation(union, iterations=max(10, orig.size[0] // 9))
        fg |= ndimage.binary_opening(ndimage.binary_fill_holes(~bg), iterations=2) & cerca
    fg = ndimage.binary_fill_holes(ndimage.binary_opening(fg, iterations=2))
    # quedarse con las piezas grandes
    lab, n = ndimage.label(fg)
    if n > 1:
        tam = ndimage.sum(fg, lab, range(1, n + 1))
        fg = np.isin(lab, [i + 1 for i, t in enumerate(tam) if t >= tam.max() * 0.04])
    alfa = Image.fromarray((fg * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.0))
    im = orig.convert("RGBA"); im.putalpha(alfa)
    bbox = im.getchannel("A").point(lambda a: 255 if a > 20 else 0).getbbox()
    return im.crop(bbox) if bbox else im

def fondo_ia(escena, seed):
    prompt = ESCENAS[escena][0] + ", photorealistic, vertical, no people, no text, no logos"
    url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt) +
           f"?width={W}&height={H + 200}&seed={seed}&nologo=true&model=flux")
    global IA_FALLOS
    if IA_FALLOS >= 6:
        return None
    data = get(url, timeout=90, intentos=1)
    try:
        im = Image.open(io.BytesIO(data)).convert("RGB")
        if im.size[0] < 500:
            raise ValueError("imagen pequeña")
        log("  fondo IA ok", escena)
        im = im.resize((W, int(W * im.size[1] / im.size[0])), Image.LANCZOS)
        return im.crop((0, 0, W, H))  # recorta abajo (marca de agua del generador)
    except Exception as e:
        IA_FALLOS += 1
        log("  fondo IA falla:", escena, repr(e)[:120], (data or b"")[:120])
        return None
IA_FALLOS = 0

VARIANTES = 5
def fondo(escena, seed):
    """Banco de fondos reutilizables en fondos/: menos peticiones a la IA, más rápido."""
    k = seed % VARIANTES
    ruta = f"fondos/{escena}_{k}.jpg"
    if os.path.exists(ruta):
        return Image.open(ruta).convert("RGB"), True
    im = fondo_ia(escena, 1000 + k) or (time.sleep(15) or fondo_ia(escena, 2000 + k))
    if im:
        os.makedirs("fondos", exist_ok=True); im.save(ruta, quality=88)
        return im, True
    return fondo_dibujado(escena, seed), False

def fondo_dibujado(escena, seed):
    """Plan B sin internet: degradado + bokeh desenfocado."""
    rnd = random.Random(seed)
    c1, c2 = ESCENAS[escena][1], ESCENAS[escena][2]
    im = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(im)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(c1[i] * (1 - t) + c2[i] * t) for i in range(3)))
    bok = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bok)
    luz = (255, 214, 150) if escena in ("navidad", "halloween", "salon") else (255, 255, 255)
    for _ in range(22):
        r = rnd.randint(18, 60); x = rnd.randint(-30, W + 30); y = rnd.randint(int(H * .05), int(H * .6))
        bd.ellipse([x - r, y - r, x + r, y + r], fill=luz + (rnd.randint(30, 80),))
    im = Image.alpha_composite(im.convert("RGBA"), bok.filter(ImageFilter.GaussianBlur(9))).convert("RGB")
    # luz central suave (viñeta)
    vin = Image.new("L", (W, H), 0)
    ImageDraw.Draw(vin).ellipse([-W * .3, H * .1, W * 1.3, H * .95], fill=255)
    vin = vin.filter(ImageFilter.GaussianBlur(220))
    oscuro = ImageEnhance.Brightness(im).enhance(0.72)
    im = Image.composite(im, oscuro, vin)
    # "mesa" en primer plano
    mesa = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(mesa).rectangle([0, int(H * .68), W, H], fill=tuple(int(v * .75) for v in c2) + (235,))
    return Image.alpha_composite(im.convert("RGBA"), mesa.filter(ImageFilter.GaussianBlur(6))).convert("RGB")

def componer(recorte, fondo, escala=0.72):
    fondo = fondo.copy().convert("RGBA")
    # desenfoque suave del fondo para que destaque el producto
    fondo = fondo.filter(ImageFilter.GaussianBlur(2.5))
    rw, rh = recorte.size
    s = min(W * escala / rw, H * 0.5 / rh)
    p = recorte.resize((max(1, int(rw * s)), max(1, int(rh * s))), Image.LANCZOS)
    x = (W - p.size[0]) // 2
    y = int(H * 0.78) - p.size[1]  # apoyado sobre la "mesa", por encima de la zona de textos inferior
    # sombra
    sh = Image.new("RGBA", fondo.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).ellipse([x + p.size[0] * .08, y + p.size[1] - 30, x + p.size[0] * .92, y + p.size[1] + 40], fill=(0, 0, 0, 120))
    fondo = Image.alpha_composite(fondo, sh.filter(ImageFilter.GaussianBlur(28)))
    fondo.alpha_composite(p, (x, y))
    out = fondo.convert("RGB")
    return ImageEnhance.Contrast(out).enhance(1.04)

def procesar_imagen(data, carpeta, n, escenas, base_seed, rehacer_dibujados=False):
    """Devuelve True si todas las escenas tienen fondo realista (IA)."""
    todo_ia = True
    os.makedirs(carpeta, exist_ok=True)
    Image.open(io.BytesIO(data)).convert("RGB").save(f"{carpeta}/foto_{n}.jpg", quality=90)
    try:
        rec = recortar(data)
    except Exception as e:
        log("  sin recorte:", e); return True
    rec.save(f"{carpeta}/recorte_{n}.png")
    for esc in escenas:
        if esc not in ESCENAS:
            continue
        seed = base_seed + n * 7 + list(ESCENAS).index(esc)
        destino = f"{carpeta}/escena_{esc}_{n}.jpg"
        if os.path.exists(destino):
            continue
        fnd, ia = fondo(esc, seed)
        componer(rec, fnd).save(destino, quality=90)
        if not ia:
            todo_ia = False
        print("  ->", destino, "(IA)" if ia else "(dibujado)")
    return todo_ia

def sync_tienda(max_fotos=3):
    data = get(f"{TIENDA}/products.json?limit=250")
    if not data:
        print("No se pudo leer la tienda"); return
    estado_path = "tienda/estado.json"
    estado = json.load(open(estado_path)) if os.path.exists(estado_path) else {}
    for p in json.loads(data)["products"]:
        h = p["handle"]
        for n, img in enumerate(p.get("images", [])[:max_fotos], 1):
            clave = f"{h}/{n}"
            firma = img["src"].split("?")[0]
            if estado.get(clave) == firma:
                continue
            log("Tienda:", clave)
            b = get(img["src"])
            if not b:
                continue
            carpeta = f"tienda/{h}"
            for f in glob.glob(f"{carpeta}/*_{n}.*"):
                os.remove(f)
            ok = procesar_imagen(b, carpeta, n, DEFAULT_ESCENAS, int(hashlib.md5(h.encode()).hexdigest()[:6], 16))
            estado[clave] = firma if ok else firma + "|dibujado"  # se rehace en la próxima pasada
        json.dump(estado, open(estado_path, "w"), indent=1, ensure_ascii=False)
    # índice legible
    with open("tienda/INDICE.md", "w") as f:
        f.write("# Fotos de la tienda\n\n")
        for d in sorted(glob.glob("tienda/*/")):
            f.write(f"- {d}: {len(glob.glob(d + 'escena_*'))} escenas, {len(glob.glob(d + 'recorte_*'))} recortes\n")

def pedidos():
    for path in sorted(glob.glob("pedidos/*.json")):
        job = json.load(open(path))
        jid = job.get("id") or os.path.splitext(os.path.basename(path))[0]
        carpeta = f"salida/{jid}"
        if os.path.exists(f"{carpeta}/HECHO"):
            continue
        print("Pedido:", jid)
        escenas = job.get("escenas") or DEFAULT_ESCENAS
        ok = 0
        for n, url in enumerate(job.get("imagenes", [])[:6], 1):
            b = get(url)
            if b:
                procesar_imagen(b, carpeta, n, escenas, int(hashlib.md5(jid.encode()).hexdigest()[:6], 16)); ok += 1
        os.makedirs(carpeta, exist_ok=True)
        open(f"{carpeta}/HECHO", "w").write(f"{ok} imágenes procesadas\n")

if __name__ == "__main__":
    modo = sys.argv[1] if len(sys.argv) > 1 else "todo"
    if modo in ("todo", "tienda"):
        sync_tienda()
    if modo == "rehacer":  # vuelve a procesar todas las fotos de la tienda
        if os.path.exists("tienda/estado.json"): os.remove("tienda/estado.json")
        sync_tienda()
    if modo in ("todo", "pedidos"):
        pedidos()
    os.makedirs("salida", exist_ok=True)
    open("salida/ultimo_log.txt", "w").write("\n".join(LOG[-200:]) + "\n")
