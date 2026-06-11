"""Validação de endereços Bitcoin e invoices Lightning com checksum.

Implementação em Python puro (sem dependências externas) de:
- Base58Check  (endereços legados P2PKH/P2SH: 1.., 3.., m.., n.., 2..)
- Bech32 / Bech32m (SegWit: bc1.., tb1.., bcrt1..) conforme BIP173 e BIP350
- Verificação do checksum bech32 de invoices Lightning (BOLT11)

Aceita redes mainnet e testnet/signet/regtest (a POC roda em testnet).
A validação por checksum protege contra endereços digitados/escaneados
incorretamente — algo que a checagem por prefixo anterior não fazia.
"""

import hashlib

# --------------------------------------------------------------------------
# Base58Check (BIP- legado)
# --------------------------------------------------------------------------
_B58_ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
_B58_MAP = {c: i for i, c in enumerate(_B58_ALPHABET)}

# Version bytes aceitos: P2PKH/P2SH em mainnet e testnet.
_BASE58_VERSIONS = {0x00, 0x05, 0x6F, 0xC4}


def _b58check_decode(s):
    """Decodifica uma string Base58Check e valida o checksum (double-SHA256).
    Retorna o payload (version + hash) sem o checksum, ou None se inválido."""
    num = 0
    for ch in s:
        val = _B58_MAP.get(ch)
        if val is None:
            return None
        num = num * 58 + val
    body = num.to_bytes((num.bit_length() + 7) // 8, 'big') if num else b''
    # Zeros à esquerda são codificados como '1'.
    n_pad = len(s) - len(s.lstrip('1'))
    combined = b'\x00' * n_pad + body
    if len(combined) < 5:
        return None
    data, checksum = combined[:-4], combined[-4:]
    if hashlib.sha256(hashlib.sha256(data).digest()).digest()[:4] != checksum:
        return None
    return data


def _valid_base58_address(addr):
    data = _b58check_decode(addr)
    if data is None or len(data) != 21:
        return False
    return data[0] in _BASE58_VERSIONS


# --------------------------------------------------------------------------
# Bech32 / Bech32m (BIP173 / BIP350)
# --------------------------------------------------------------------------
_BECH32_CHARSET = 'qpzry9x8gf2tvdw0s3jn54khce6mua7l'
_BECH32_CONST = 1
_BECH32M_CONST = 0x2bc830a3
_SEGWIT_HRPS = {'bc', 'tb', 'bcrt'}


def _bech32_polymod(values):
    gen = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = ((chk & 0x1ffffff) << 5) ^ v
        for i in range(5):
            chk ^= gen[i] if ((b >> i) & 1) else 0
    return chk


def _bech32_hrp_expand(hrp):
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _bech32_spec(hrp, data):
    """Retorna 'bech32', 'bech32m' ou None conforme o checksum."""
    const = _bech32_polymod(_bech32_hrp_expand(hrp) + data)
    if const == _BECH32_CONST:
        return 'bech32'
    if const == _BECH32M_CONST:
        return 'bech32m'
    return None


def _bech32_decode(addr, max_length=90):
    """Decodifica bech32/bech32m e valida o checksum.
    Retorna (hrp, data_sem_checksum, spec) ou (None, None, None).
    max_length permite estender o limite para invoices Lightning (BOLT11),
    que removem o teto de 90 caracteres do BIP173."""
    if any(ord(c) < 33 or ord(c) > 126 for c in addr):
        return (None, None, None)
    if addr != addr.lower() and addr != addr.upper():
        return (None, None, None)  # caso misto é inválido
    addr = addr.lower()
    pos = addr.rfind('1')
    if pos < 1 or pos + 7 > len(addr) or len(addr) > max_length:
        return (None, None, None)
    hrp = addr[:pos]
    data = []
    for c in addr[pos + 1:]:
        idx = _BECH32_CHARSET.find(c)
        if idx == -1:
            return (None, None, None)
        data.append(idx)
    spec = _bech32_spec(hrp, data)
    if spec is None:
        return (None, None, None)
    return (hrp, data[:-6], spec)


def _convertbits(data, frombits, tobits, pad=True):
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    max_acc = (1 << (frombits + tobits - 1)) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            return None
        acc = ((acc << frombits) | value) & max_acc
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return ret


def _valid_segwit_address(addr):
    hrp, data, spec = _bech32_decode(addr)
    if hrp is None or hrp not in _SEGWIT_HRPS or not data:
        return False
    witver = data[0]
    if witver > 16:
        return False
    prog = _convertbits(data[1:], 5, 8, False)
    if prog is None or len(prog) < 2 or len(prog) > 40:
        return False
    if witver == 0 and len(prog) not in (20, 32):
        return False
    # BIP350: witness v0 usa bech32; v1+ (ex.: Taproot) usa bech32m.
    if witver == 0 and spec != 'bech32':
        return False
    if witver != 0 and spec != 'bech32m':
        return False
    return True


# --------------------------------------------------------------------------
# API pública
# --------------------------------------------------------------------------
def validate_bitcoin_address(address):
    """True se for um endereço Bitcoin válido (checksum verificado),
    em qualquer rede (mainnet/testnet/signet/regtest)."""
    if not isinstance(address, str):
        return False
    addr = address.strip()
    if not addr:
        return False
    return _valid_segwit_address(addr) or _valid_base58_address(addr)


def validate_lightning_invoice(invoice):
    """True se for uma invoice BOLT11 com prefixo conhecido e checksum
    bech32 válido. Não decodifica todos os campos BOLT11, mas o checksum
    já protege contra erros de digitação/leitura."""
    if not isinstance(invoice, str):
        return False
    inv = invoice.strip().lower()
    if not inv.startswith(('lnbc', 'lntb', 'lnbcrt')):
        return False
    # BOLT11 é bech32 (não bech32m) e remove o limite de 90 caracteres.
    hrp, data, spec = _bech32_decode(inv, max_length=2000)
    return spec == 'bech32' and hrp is not None and bool(data)
