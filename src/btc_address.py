"""Validação de endereços Bitcoin e invoices Lightning com checksum.

Implementação em Python puro (sem dependências externas) de:
- Base58Check  (endereços legados P2PKH/P2SH: 1.., 3.., m.., n.., 2..)
- Bech32 / Bech32m (SegWit: bc1.., tb1.., bcrt1..) conforme BIP173 e BIP350
- Verificação do checksum bech32 de invoices Lightning (BOLT11)
- Validação de Lightning Address (user@domain, LUD-16/LNURL-pay)
- Decodificação do valor (msats) codificado no HRP de uma invoice BOLT11

A rede (mainnet, testnet/signet ou regtest) vem da configuração e é exigida na
validação: um ATM de mainnet recusa um endereço de testnet escaneado por
engano, em vez de aceitar o dinheiro e só descobrir o problema quando o BTCPay
recusar o envio.

A validação por checksum protege contra endereços digitados/escaneados
incorretamente — algo que a checagem por prefixo anterior não fazia.
"""

import hashlib
import re
from decimal import Decimal

# Redes suportadas. 'testnet' cobre também signet, que usa os mesmos version
# bytes e o mesmo HRP ('tb'). O padrão é mainnet: se a configuração não disser
# a rede, recusar endereços de teste é o lado seguro para errar num caixa que
# opera dinheiro de verdade.
NETWORKS = ('mainnet', 'testnet', 'regtest')
DEFAULT_NETWORK = 'mainnet'

# --------------------------------------------------------------------------
# Base58Check (BIP- legado)
# --------------------------------------------------------------------------
_B58_ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
_B58_MAP = {c: i for i, c in enumerate(_B58_ALPHABET)}

# Version bytes por rede: P2PKH e P2SH. Testnet e signet compartilham os
# mesmos bytes; regtest também.
_BASE58_VERSIONS = {
    'mainnet': frozenset({0x00, 0x05}),
    'testnet': frozenset({0x6F, 0xC4}),
    'regtest': frozenset({0x6F, 0xC4}),
}


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


def _valid_base58_address(addr, versions):
    data = _b58check_decode(addr)
    if data is None or len(data) != 21:
        return False
    return data[0] in versions


# --------------------------------------------------------------------------
# Bech32 / Bech32m (BIP173 / BIP350)
# --------------------------------------------------------------------------
_BECH32_CHARSET = 'qpzry9x8gf2tvdw0s3jn54khce6mua7l'
_BECH32_CONST = 1
_BECH32M_CONST = 0x2bc830a3
# HRP SegWit por rede. Signet usa 'tb', como a testnet.
_SEGWIT_HRPS = {
    'mainnet': frozenset({'bc'}),
    'testnet': frozenset({'tb'}),
    'regtest': frozenset({'bcrt'}),
}


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


def _valid_segwit_address(addr, hrps):
    hrp, data, spec = _bech32_decode(addr)
    if hrp is None or hrp not in hrps or not data:
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
def validate_bitcoin_address(address, network=DEFAULT_NETWORK):
    """True se for um endereço Bitcoin válido (checksum verificado) DA REDE
    informada ('mainnet', 'testnet' — que cobre signet — ou 'regtest').

    A rede importa: sem amarrá-la, um ATM de mainnet aceitava um endereço de
    testnet escaneado por engano. O BTCPay recusaria o envio, mas só depois de
    o cliente ter posto o dinheiro na máquina.

    Uma rede desconhecida recusa tudo (falha fechada). Quem lê a configuração
    é atm_core.get_network(), que normaliza o valor e avisa no log."""
    if not isinstance(address, str):
        return False
    addr = address.strip()
    # Endereços Bitcoin reais têm no máximo ~62 chars. Um teto rígido evita que
    # uma colagem/leitura enorme dispare o decode Base58Check O(n^2) e congele a
    # thread da GUI (DoS do quiosque). O caminho bech32 já tem seu próprio teto.
    if not addr or len(addr) > 100:
        return False
    return (_valid_segwit_address(addr, _SEGWIT_HRPS.get(network, frozenset()))
            or _valid_base58_address(addr,
                                     _BASE58_VERSIONS.get(network, frozenset())))


def validate_lightning_invoice(invoice, network=DEFAULT_NETWORK):
    """True se for uma invoice BOLT11 DA REDE informada, com checksum bech32
    válido. Não decodifica todos os campos BOLT11, mas o checksum já protege
    contra erros de digitação/leitura."""
    if not isinstance(invoice, str):
        return False
    esperado = _BOLT11_NETWORK_PREFIX.get(network)
    if esperado is None:
        return False
    inv = invoice.strip().lower()
    # O prefixo mais longo tem de ser identificado primeiro: 'lnbcrt'
    # (regtest) começa com 'lnbc' (mainnet), então comparar por startswith
    # aceitaria uma invoice de regtest num ATM de mainnet.
    if next((p for p in _BOLT11_PREFIXES if inv.startswith(p)), None) != esperado:
        return False
    # BOLT11 é bech32 (não bech32m) e remove o limite de 90 caracteres.
    hrp, data, spec = _bech32_decode(inv, max_length=2000)
    return spec == 'bech32' and hrp is not None and bool(data)


# --------------------------------------------------------------------------
# Lightning Address (LUD-16 / LNURL-pay) — ex.: voce@walletofsatoshi.com
# --------------------------------------------------------------------------
# user: a-z0-9-_. (LUD-16); domain: rótulos alfanuméricos + TLD de letras.
# Validação de FORMATO apenas — a resolução real é feita em atm_core via HTTPS,
# onde destinos inalcançáveis (entre eles .onion, que o ATM não roteia) são
# recusados antes de qualquer pagamento.
_LN_ADDRESS_RE = re.compile(
    r'^[a-z0-9._-]+@([a-z0-9-]+\.)+[a-z]{2,}$', re.IGNORECASE)


def validate_lightning_address(address):
    """True se a string tiver o formato de um Lightning Address (user@domain).
    Não faz a resolução de rede — apenas valida o formato para a GUI decidir
    se habilita o botão de confirmação."""
    if not isinstance(address, str):
        return False
    addr = address.strip()
    if not addr or len(addr) > 512 or '@' not in addr or any(c.isspace() for c in addr):
        return False
    return bool(_LN_ADDRESS_RE.match(addr))


# --------------------------------------------------------------------------
# Decodificação do valor (msats) no HRP de uma invoice BOLT11
# --------------------------------------------------------------------------
# Multiplicadores BOLT11 (fração de BTC). Sem multiplicador → BTC inteiro.
_BOLT11_MULTIPLIERS = {
    'm': Decimal('0.001'),          # milli
    'u': Decimal('0.000001'),       # micro
    'n': Decimal('0.000000001'),    # nano
    'p': Decimal('0.000000000001'), # pico
}
# Ordem importa: 'lnbcrt' antes de 'lnbc' para casar o prefixo mais longo.
_BOLT11_PREFIXES = ('lnbcrt', 'lntb', 'lnbc')
_BOLT11_NETWORK_PREFIX = {
    'mainnet': 'lnbc',
    'testnet': 'lntb',
    'regtest': 'lnbcrt',
}
_MSATS_PER_BTC = Decimal(100_000_000_000)  # 1 BTC = 1e8 sat = 1e11 msat


def decode_bolt11_amount_msats(invoice):
    """Extrai o valor (em millisatoshis) codificado no HRP de uma invoice
    BOLT11. Retorna int (msats) ou None se a invoice não tiver valor
    (amountless), for inválida, ou o valor não for um número inteiro de msat.

    Usado como verificação de segurança ao pagar um Lightning Address: a
    invoice retornada precisa ter exatamente o valor solicitado."""
    if not isinstance(invoice, str):
        return None
    inv = invoice.strip().lower()
    sep = inv.rfind('1')
    if sep < 1:
        return None
    hrp = inv[:sep]
    prefix = next((p for p in _BOLT11_PREFIXES if hrp.startswith(p)), None)
    if prefix is None:
        return None
    amount_part = hrp[len(prefix):]
    if not amount_part:
        return None  # invoice sem valor (amountless)
    if amount_part[-1] in _BOLT11_MULTIPLIERS:
        digits, factor = amount_part[:-1], _BOLT11_MULTIPLIERS[amount_part[-1]]
    else:
        digits, factor = amount_part, Decimal(1)
    # isascii() além de isdigit(): str.isdigit() aceita dígitos Unicode (ex.:
    # '²', '٣') que Decimal() rejeita com InvalidOperation. Sem isso, uma
    # invoice maliciosa devolvida por um endpoint LNURL derrubaria a decodificação.
    if not (digits.isascii() and digits.isdigit()):
        return None
    msats = Decimal(digits) * factor * _MSATS_PER_BTC
    # Com multiplicador 'p', o valor precisa ser múltiplo de 10 pico-BTC para
    # resultar num número inteiro de msat; caso contrário é inválido.
    if msats != msats.to_integral_value():
        return None
    return int(msats)
