import qrcode
import requests
from PIL import Image

def generate_qr_code(data):
    qr = qrcode.QRCode(version=1, box_size=6, border=1)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill='black', back_color='white')
    img = img.resize((300, 300), Image.Resampling.LANCZOS)
    img.save("/tmp/qr.png")
    return "/tmp/qr.png"

def is_valid_bitcoin_address(address):
    return isinstance(address, str) and (address.startswith('1') or address.startswith('3') or address.startswith('bc1'))

def is_valid_lightning_invoice(invoice):
    return isinstance(invoice, str) and invoice.startswith('ln')

def is_online():
    try:
        requests.get("https://google.com", timeout=5)
        return True
    except requests.ConnectionError:
        return False
