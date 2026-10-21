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

_session = None
def rembg_session():
    global _session
    if _session is None:
        from rembg import new_session
        _session = new_session("isnet-general-use")
    return _session

def get(url, timeout=60):
    for i in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=timeout)
            if r.status_code == 200:
                return r.content
            print("  HTTP", r.status_code, url[:90])
        except Exception as e:
            print("  error", e, url[:90])
        time.sleep(3 * (i + 1))
    return None

def recortar(img_bytes):
    from rembg import remove
    out = remove(img_bytes, session=rembg_session())
    im = Image.open(io.BytesIO(out)).convert("RGBA")
    bbox = im.getchannel("A").point(lambda a: 255 if a > 20 else 0).getbbox()
    return im.crop(bbox) if bbox else im

def fondo_ia(escena, seed):
    prompt = ESCENAS[escena][0] + ", photorealistic, vertical, no people, no text, no logos"
    url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt) +
           f"?width={W}&height={H}&seed={seed}&nologo=true&model=flux")
    data = get(url, timeout=180)
    if not data:
        return None
    try:
        im = Image.open(io.BytesIO(data)).convert("RGB")
        if im.size[0] < 500:
            return None
        return im.resize((W, H), Image.LANCZOS)
    except Exception:
        return None

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
    for _ in range(45):
        r = rnd.randint(25, 110); x = rnd.randint(-50, W + 50); y = rnd.randint(0, int(H * .6))
        bd.ellipse([x - r, y - r, x + r, y + r], fill=luz + (rnd.randint(25, 70),))
    im = Image.alpha_composite(im.convert("RGBA"), bok.filter(ImageFilter.GaussianBlur(18))).convert("RGB")
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
    y = int(H * 0.74) - p.size[1]  # apoyado sobre la "mesa", por encima de la zona de textos inferior
    # sombra
    sh = Image.new("RGBA", fondo.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).ellipse([x + p.size[0] * .08, y + p.size[1] - 30, x + p.size[0] * .92, y + p.size[1] + 40], fill=(0, 0, 0, 120))
    fondo = Image.alpha_composite(fondo, sh.filter(ImageFilter.GaussianBlur(28)))
    fondo.alpha_composite(p, (x, y))
    out = fondo.convert("RGB")
    return ImageEnhance.Contrast(out).enhance(1.04)

def procesar_imagen(data, carpeta, n, escenas, base_seed):
    os.makedirs(carpeta, exist_ok=True)
    Image.open(io.BytesIO(data)).convert("RGB").save(f"{carpeta}/foto_{n}.jpg", quality=90)
    try:
        rec = recortar(data)
    except Exception as e:
        print("  sin recorte:", e); return
    rec.save(f"{carpeta}/recorte_{n}.png")
    for esc in escenas:
        if esc not in ESCENAS:
            continue
        seed = base_seed + n * 7 + list(ESCENAS).index(esc)
        destino = f"{carpeta}/escena_{esc}_{n}.jpg"
        if os.path.exists(destino):
            continue
        fondo = fondo_ia(esc, seed) or fondo_dibujado(esc, seed)
        componer(rec, fondo).save(destino, quality=90)
        print("  ->", destino)

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
            print("Tienda:", clave)
            b = get(img["src"])
            if not b:
                continue
            carpeta = f"tienda/{h}"
            for f in glob.glob(f"{carpeta}/*_{n}.*"):
                os.remove(f)
            procesar_imagen(b, carpeta, n, DEFAULT_ESCENAS, int(hashlib.md5(h.encode()).hexdigest()[:6], 16))
            estado[clave] = firma
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
    if modo in ("todo", "pedidos"):
        pedidos()
