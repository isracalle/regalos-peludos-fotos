# Robot de fotos · Regalos Peludos

Hace fotos nuevas de los productos sin tocar nada a mano:
- **Cada día** descarga las fotos de los productos de regalospeludos.shop → `tienda/<producto>/`
- **Bajo pedido** (archivo en `pedidos/`) descarga fotos del proveedor → `salida/<id>/`

Para cada foto deja: `foto_N.jpg` (original), `recorte_N.png` (sin fondo) y `escena_<fondo>_N.jpg` (1080x1920, producto sobre un fondo nuevo, listo para vídeo).
Lo usa la "Fábrica de anuncios". No publica nada.
