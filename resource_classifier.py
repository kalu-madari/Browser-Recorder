import mimetypes
from urllib.parse import urlparse
import os

def determine_extension(url: str, content_type: str, resource_type: str) -> str:
    if content_type:
        mime = content_type.split(';')[0].strip().lower()
        if mime in ['application/javascript', 'text/javascript', 'application/x-javascript']:
            return '.js'
        if mime in ['application/json', 'application/ld+json']:
            return '.json'
        if mime == 'text/html':
            return '.html'
        if mime == 'text/css':
            return '.css'
        
        mime_ext_map = {
            'image/png': '.png',
            'image/jpeg': '.jpeg',
            'image/webp': '.webp',
            'image/gif': '.gif',
            'image/svg+xml': '.svg',
            'image/avif': '.avif',
            'image/bmp': '.bmp',
            'image/x-icon': '.ico',
            'font/woff2': '.woff2',
            'font/woff': '.woff',
            'font/ttf': '.ttf',
            'font/otf': '.otf',
            'font/collection': '.ttc',
            'application/font-woff': '.woff',
            'application/vnd.ms-fontobject': '.eot',
            'video/mp4': '.mp4',
            'video/webm': '.webm',
            'audio/mpeg': '.mp3',
            'audio/ogg': '.ogg',
            'application/wasm': '.wasm',
            'application/manifest+json': '.webmanifest',
            'application/xml': '.xml',
            'text/xml': '.xml',
            'application/pdf': '.pdf'
        }
        if mime in mime_ext_map:
            return mime_ext_map[mime]
            
        ext = mimetypes.guess_extension(mime)
        if ext:
            if ext == '.jpe':
                return '.jpg'
            return ext
            
    parsed = urlparse(url)
    path = parsed.path
    ext = os.path.splitext(path)[1].lower()
    
    if ext and 1 < len(ext) <= 10 and ext.startswith("."):
        clean_ext = ext.split('?')[0].split('#')[0]
        if clean_ext:
            return clean_ext
            
    if resource_type == 'document': return '.html'
    if resource_type == 'script': return '.js'
    if resource_type == 'stylesheet': return '.css'
    if resource_type == 'image': return '.png'
    if resource_type == 'font': return '.woff2'
    if resource_type == 'media': return '.mp4'
    if resource_type == 'manifest': return '.webmanifest'
    if resource_type == 'fetch' or resource_type == 'xhr': return '.json' # Rough fallback
    
    return '.bin'
