import configparser
import contextlib
import ipaddress
import json
import logging
import os
import socket
import threading
import time
from decimal import Decimal, ROUND_DOWN
from urllib.parse import urlparse

import requests
import serial
import telegram_send
from cryptography.fernet import Fernet
from escpos.printer import Usb

from btc_address import (DEFAULT_NETWORK, NETWORKS, decode_bolt11_amount_msats,
                         validate_lightning_address)

_LOG_PATH = '/var/log/btc_atm.log'
_LOG_FORMAT = '%(asctime)s %(levelname)s %(message)s'
try:
    logging.basicConfig(filename=_LOG_PATH, level=logging.INFO, format=_LOG_FORMAT)
except (PermissionError, FileNotFoundError, OSError):
    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)
    logging.warning("Could not write to %s; logging to stderr instead.", _LOG_PATH)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), '..', 'config.ini')
_QUEUE_PATH = '/var/atm/offline_queue.json'
_FAILED_QUEUE_PATH = '/var/atm/failed_queue.json'
_KEY_PATH = '/etc/atm/key'

_SATOSHI = Decimal('0.00000001')
_LNURL_TIMEOUT = 15  # segundos para cada chamada HTTP de resolução LNURL-pay
_LNURL_MAX_BYTES = 65536  # teto do corpo de resposta LNURL (anti-DoS de memória)

# Serializa todo acesso à fila offline. GUI (enqueue) e o worker de background
# (process_offline_queue) rodam no mesmo processo em threads diferentes; sem
# este lock, o read-modify-write concorrente perderia transações ou reprocessaria
# pagamentos (gasto duplo).
_QUEUE_LOCK = threading.RLock()

# Idade máxima (s) da cotação salva numa transação enfileirada. Acima disso,
# busca-se uma cotação fresca ao liquidar, para não usar um preço obsoleto.
_QUEUE_RATE_MAX_AGE = 120

# Quantas tentativas de ENVIO uma transação enfileirada faz antes de ir para a
# fila de descarte. Sem este teto, uma transação que falha de forma
# determinística (endereço que o BTCPay rejeita com 4xx) voltava para a fila a
# cada ciclo, para sempre: ninguém era avisado e o cliente nunca era
# reembolsado.
#
# Só conta tentativa de fato feita. Uma queda do BTCPay não queima o contador:
# is_online() barra o processamento quando o host não responde, e a falta de
# cotação reenfileira sem tentar enviar. Com o ciclo de 5 min, o teto equivale
# a ~50 minutos de rejeições seguidas com o servidor no ar.
_MAX_QUEUE_ATTEMPTS = 10


class PaymentNotBroadcast(Exception):
    """O pagamento com certeza NÃO foi transmitido (sem conexão, ou rejeitado
    pelo servidor com 4xx). É seguro reenfileirar para tentar de novo."""


class PaymentUncertain(Exception):
    """Resultado ambíguo (timeout, 5xx): o Bitcoin PODE já ter sido enviado.
    NÃO reenfileirar automaticamente — o operador deve conferir a carteira
    antes de qualquer reenvio, para evitar gasto duplo."""


@contextlib.contextmanager
def _not_broadcast_on_error(context):
    """Classifica como PaymentNotBroadcast qualquer falha ocorrida ANTES do
    POST de pagamento.

    Ler o config.ini, ler /etc/atm/key e descriptografar o token acontecem
    antes de qualquer byte ir para a rede. Se falharem, o Bitcoin com certeza
    não saiu e a transação pode ser enfileirada com segurança.

    Sem isto, uma chave ausente ou rotacionada escapava como
    FileNotFoundError/InvalidToken — e tanto a GUI quanto a fila tratam
    exceção desconhecida como resultado AMBÍGUO, que por segurança não é
    reenfileirado. O cliente pagava em dinheiro e ficava sem o Bitcoin e sem
    registro, por um erro de configuração que nem chegou a tentar pagar.

    Exceções já classificadas passam intactas: rebaixar um PaymentUncertain
    para PaymentNotBroadcast reintroduziria o risco de gasto duplo."""
    try:
        yield
    except (PaymentNotBroadcast, PaymentUncertain):
        raise
    except Exception as e:
        raise PaymentNotBroadcast(f"{context}: {e}") from e


def brl_to_btc(amount_brl, rate):
    """Converte BRL->BTC com precisão decimal, arredondando para baixo
    (ROUND_DOWN) para nunca enviar mais do que o cliente pagou."""
    return (Decimal(str(amount_brl)) / Decimal(str(rate))).quantize(
        _SATOSHI, rounding=ROUND_DOWN)


def _post_payment(url, payload):
    """POST de pagamento com classificação do resultado para segurança
    financeira. Levanta PaymentNotBroadcast (seguro reenfileirar) ou
    PaymentUncertain (não reenfileirar) conforme o tipo de falha."""
    # Montar o header lê a config e descriptografa o token — ainda antes da
    # rede, portanto uma falha aqui é comprovadamente não-transmitida.
    with _not_broadcast_on_error("falha ao montar as credenciais do BTCPay"):
        headers = _btcpay_headers()
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
    except requests.ConnectionError as e:
        raise PaymentNotBroadcast(f"sem conexão: {e}") from e
    except requests.Timeout as e:
        raise PaymentUncertain(f"timeout — pode ter sido enviado: {e}") from e
    except requests.RequestException as e:
        raise PaymentUncertain(f"erro de rede: {e}") from e
    if 200 <= resp.status_code < 300:
        return resp
    if 400 <= resp.status_code < 500:
        # Erro de validação/rejeição → não foi transmitido.
        raise PaymentNotBroadcast(f"rejeitado {resp.status_code}: {resp.text[:200]}")
    # 5xx → ambíguo, pode ter transmitido antes de falhar.
    raise PaymentUncertain(f"erro do servidor {resp.status_code}: {resp.text[:200]}")


def _safe_json_field(resp, field, default='desconhecido'):
    """Lê um campo do corpo SEM nunca levantar. Após um 2xx o dinheiro pode
    já ter saído; um erro de parsing não pode propagar (senão a GUI
    reenfileiraria e haveria gasto duplo)."""
    try:
        return resp.json().get(field) or default
    except ValueError:
        return default


def _load_config():
    cfg = configparser.ConfigParser()
    cfg.read(_CONFIG_PATH)
    return cfg


_DEFAULT_MAX_TX_BRL = 1000


def get_max_transaction_brl():
    """Teto por transação em BRL ([atm] max_transaction_brl no config.ini).
    A aceitação de cédulas para quando o total inserido atinge este valor.
    Com config ausente/inválida, usa um padrão conservador em vez de ficar
    sem limite."""
    try:
        value = int(_load_config()['atm']['max_transaction_brl'])
        if value > 0:
            return value
        logging.warning("max_transaction_brl inválido (%s); usando %s",
                        value, _DEFAULT_MAX_TX_BRL)
    except Exception:
        logging.info("max_transaction_brl não configurado; usando %s",
                     _DEFAULT_MAX_TX_BRL)
    return _DEFAULT_MAX_TX_BRL


def get_network():
    """Rede Bitcoin em que este ATM opera ([btcpay] network no config.ini):
    'mainnet', 'testnet' (cobre signet) ou 'regtest'.

    Define quais endereços e invoices a GUI aceita. Config ausente ou valor
    desconhecido caem em mainnet — recusar um endereço de teste é o lado
    seguro para errar; o contrário aceitaria dinheiro de verdade rumo a um
    destino que o BTCPay vai rejeitar."""
    try:
        value = _load_config()['btcpay']['network'].strip().lower()
        if value in NETWORKS:
            return value
        logging.warning("network inválida (%s); usando %s. Válidas: %s",
                        value, DEFAULT_NETWORK, ', '.join(NETWORKS))
    except Exception:
        logging.info("network não configurada; usando %s", DEFAULT_NETWORK)
    return DEFAULT_NETWORK


def _get_api_token():
    cfg = _load_config()
    encrypted = cfg['btcpay']['api_token']
    with open(_KEY_PATH, 'rb') as f:
        key = f.read()
    return Fernet(key).decrypt(encrypted.encode()).decode()


def _btcpay_headers():
    return {
        'Authorization': f'token {_get_api_token()}',
        'Content-Type': 'application/json',
    }


def init_note_reader():
    cfg = _load_config()
    port = cfg['hardware']['serial_port']
    baud = int(cfg['hardware']['baud_rate'])
    return serial.Serial(port, baud, timeout=0)


def get_btc_rate():
    cfg = _load_config()
    host = cfg['btcpay']['host'].rstrip('/')
    store_id = cfg['btcpay']['store_id']
    currency = cfg['btcpay'].get('currency', 'BRL')
    pair = f'BTC_{currency}'
    try:
        url = f"{host}/api/v1/stores/{store_id}/rates?currencyPair={pair}"
        resp = requests.get(url, headers=_btcpay_headers(), timeout=10)
        resp.raise_for_status()
        for entry in resp.json():
            if entry.get('currencyPair') == pair:
                # Cada elemento pode trazer 'errors' quando a cotação falhou.
                if entry.get('errors'):
                    logging.error("Rate errors for %s: %s", pair, entry['errors'])
                    return None
                return float(entry['rate'])
    except Exception as e:
        logging.error("Failed to get BTC rate: %s", e)
    return None


def send_onchain_payment(amount_brl, address, rate):
    with _not_broadcast_on_error("falha ao preparar o pagamento on-chain"):
        cfg = _load_config()
        host = cfg['btcpay']['host'].rstrip('/')
        store_id = cfg['btcpay']['store_id']
        crypto = cfg['btcpay'].get('crypto_code', 'BTC')
        # Path correto do Greenfield atual: payment-methods/{BTC-CHAIN}/wallet/...
        # (o antigo /on-chain/{cryptoCode}/transactions retorna 404). Permite
        # sobrescrever via config para instâncias mais antigas.
        pm = cfg['btcpay'].get('onchain_payment_method', f'{crypto}-CHAIN')
        amount_btc = brl_to_btc(amount_brl, rate)
        url = f"{host}/api/v1/stores/{store_id}/payment-methods/{pm}/wallet/transactions"
        payload = {
            'destinations': [
                {'destination': address, 'amount': str(amount_btc), 'subtractFromAmount': False}
            ],
            'noChange': False,
            # 'feerate' é omitido de propósito: enviar null pode causar 422.
            # Ausência → estimativa automática de taxa pelo BTCPay.
        }
    resp = _post_payment(url, payload)
    # 2xx = transmitido. Daqui em diante o parsing nunca pode levantar.
    txid = _safe_json_field(resp, 'transactionHash')
    logging.info("On-chain payment: %s BTC to %s txid=%s", amount_btc, address, txid)
    _send_telegram(amount_brl, amount_btc, address, txid, 'onchain')
    return txid


def _resolve_lightning_address(address, amount_btc):
    """Resolve um Lightning Address (user@domain) em uma invoice BOLT11 para
    o valor exato (LUD-16 / LNURL-pay), em duas chamadas HTTP:

      1. GET https://domain/.well-known/lnurlp/user  → metadados (callback,
         minSendable/maxSendable em msat)
      2. GET <callback>?amount=<msats>               → { "pr": "lntb..." }

    Toda a resolução acontece ANTES de qualquer chamada de pagamento ao
    BTCPay, então qualquer falha aqui é PaymentNotBroadcast (nada foi enviado
    → seguro reenfileirar). Verifica também se a invoice retornada tem
    exatamente o valor solicitado, protegendo o cliente contra um endpoint
    que devolva um valor diferente."""
    user, domain = address.split('@', 1)
    lnurl_url = f"https://{domain}/.well-known/lnurlp/{user}"

    # Valor solicitado em millisatoshis (amount_btc já está em múltiplos de sat).
    sats = int((amount_btc / _SATOSHI).to_integral_value())
    msats = sats * 1000
    if msats <= 0:
        raise PaymentNotBroadcast(f"valor muito baixo para Lightning: {amount_btc} BTC")

    meta = _lnurl_get_json(lnurl_url, f"resolver {address}")
    if meta.get('tag') != 'payRequest' or 'callback' not in meta:
        raise PaymentNotBroadcast(f"{address} não é um endereço LNURL-pay válido")
    try:
        min_s = int(meta.get('minSendable', 0))
        max_s = int(meta.get('maxSendable', 0))
    except (TypeError, ValueError) as e:
        raise PaymentNotBroadcast(f"limites LNURL inválidos para {address}: {e}") from e
    if not (min_s <= msats <= max_s):
        raise PaymentNotBroadcast(
            f"valor {msats} msat fora do permitido [{min_s}, {max_s}] por {address}")

    callback = meta['callback']
    sep = '&' if '?' in callback else '?'
    data = _lnurl_get_json(f"{callback}{sep}amount={msats}", f"obter invoice de {address}")
    if data.get('status') == 'ERROR':
        raise PaymentNotBroadcast(f"LNURL recusou: {data.get('reason', data)}")
    invoice = data.get('pr')
    if not invoice:
        raise PaymentNotBroadcast(f"LNURL não retornou invoice para {address}: {data}")

    # Segurança: a invoice precisa ter exatamente o valor solicitado.
    got = decode_bolt11_amount_msats(invoice)
    if got != msats:
        raise PaymentNotBroadcast(
            f"invoice de {address} com valor inesperado "
            f"(esperado {msats} msat, obtido {got})")
    logging.info("Lightning address %s resolvido para invoice de %s msat", address, msats)
    return invoice


def _assert_safe_lnurl_url(url):
    """Defesa anti-SSRF: o domínio e a URL de callback do LNURL vêm do cliente
    (e de um servidor que ele controla). Sem esta checagem, um cliente hostil
    poderia fazer o ATM emitir requisições a serviços internos (localhost, RPC
    do bitcoind, metadados de nuvem em 169.254.169.254 etc.).

    Exige https (LUD-16), resolve o host e recusa qualquer IP
    privado/loopback/link-local/reservado. Resta um TOCTOU teórico (DNS
    rebinding entre esta resolução e a do requests); o bloqueio de redirects em
    _lnurl_get_json reduz a superfície restante.

    Não há exceção para .onion: o ATM não fala Tor, então um destino desses não
    seria alcançável de qualquer forma, e dispensá-lo da checagem serviria
    apenas para driblar a proteção — bastaria um resolvedor local devolver
    127.0.0.1 para um nome terminado em .onion."""
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        raise PaymentNotBroadcast(f"URL LNURL inválida: {url}")
    if parsed.scheme != 'https':
        raise PaymentNotBroadcast(f"esquema LNURL não permitido: {parsed.scheme}")
    port = parsed.port or 443
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise PaymentNotBroadcast(f"não foi possível resolver {host}: {e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise PaymentNotBroadcast(
                f"host LNURL {host} aponta para endereço interno ({ip}) — bloqueado")


def _lnurl_get_json(url, context):
    """GET HTTP que retorna JSON, classificando falhas como PaymentNotBroadcast
    (a resolução ocorre antes do pagamento, então nada foi transmitido).
    Bloqueia alvos internos (anti-SSRF), não segue redirects (um redirect para
    um host interno driblaria a checagem) e limita o tamanho do corpo."""
    _assert_safe_lnurl_url(url)
    try:
        resp = requests.get(url, timeout=_LNURL_TIMEOUT,
                            allow_redirects=False, stream=True)
    except requests.RequestException as e:
        raise PaymentNotBroadcast(f"falha de rede ao {context}: {e}") from e
    try:
        if resp.status_code in (301, 302, 303, 307, 308):
            raise PaymentNotBroadcast(f"redirect não permitido ao {context}")
        if resp.status_code != 200:
            raise PaymentNotBroadcast(f"falha ao {context}: HTTP {resp.status_code}")
        raw = resp.raw.read(_LNURL_MAX_BYTES + 1, decode_content=True)
        if len(raw) > _LNURL_MAX_BYTES:
            raise PaymentNotBroadcast(f"resposta LNURL grande demais ao {context}")
    finally:
        resp.close()
    try:
        return json.loads(raw)
    except ValueError as e:
        raise PaymentNotBroadcast(f"resposta inválida ao {context}: {e}") from e


def send_lightning_payment(amount_brl, destination, rate):
    with _not_broadcast_on_error("falha ao preparar o pagamento Lightning"):
        cfg = _load_config()
        host = cfg['btcpay']['host'].rstrip('/')
        store_id = cfg['btcpay']['store_id']
        crypto = cfg['btcpay'].get('crypto_code', 'BTC')
        amount_btc = brl_to_btc(amount_brl, rate)
        # Lightning Address (user@domain) é resolvido para uma invoice BOLT11 com
        # o valor exato; uma invoice BOLT11 já pronta é usada diretamente.
        if validate_lightning_address(destination):
            invoice = _resolve_lightning_address(destination, amount_btc)
        else:
            invoice = destination
        url = f"{host}/api/v1/stores/{store_id}/lightning/{crypto}/invoices/pay"
        payload = {'BOLT11': invoice}
    resp = _post_payment(url, payload)
    # 200 = Complete, 202 = Pending. Lê o status sem deixar o parsing levantar.
    data = {}
    try:
        data = resp.json()
    except ValueError:
        pass
    status = data.get('status', 'Unknown')
    payment_hash = data.get('paymentHash') or invoice[:20]
    if status == 'Failed':
        # Pagamento Lightning é atômico: 'Failed' = nenhum fundo saiu →
        # seguro reenfileirar.
        raise PaymentNotBroadcast(f"pagamento Lightning falhou: {data or resp.text[:200]}")
    # 'Pending' é tratado como enviado: a invoice é de uso único, então um
    # reenvio do mesmo BOLT11 não causa pagamento duplicado.
    logging.info("Lightning payment: %s BTC status=%s hash=%s", amount_btc, status, payment_hash)
    # Registra o destino original (endereço ou invoice) para o operador.
    _send_telegram(amount_brl, amount_btc, destination, payment_hash, 'lightning')
    return payment_hash


def print_receipt(amount_brl, amount_btc, destination, txid):
    cfg = _load_config()
    usb_id = cfg['hardware']['printer_usb']
    vendor_id, product_id = (int(x, 16) for x in usb_id.split(':'))
    try:
        printer = Usb(vendor_id, product_id)
        printer.text("=== Bitcoin ATM ===\n")
        printer.text(f"Valor: R$ {amount_brl:.2f}\n")
        printer.text(f"BTC: {amount_btc:.8f}\n")
        printer.text(f"Destino: {destination[:30]}...\n")
        printer.text(f"TxID: {txid[:30]}...\n")
        printer.text(f"Data: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        printer.cut()
    except Exception as e:
        logging.error("Failed to print receipt: %s", e)


def enqueue_transaction(amount_brl, destination, payment_type, rate):
    with _QUEUE_LOCK:
        queue = _load_queue()
        queue.append({
            'amount_brl': amount_brl,
            'destination': destination,
            'payment_type': payment_type,
            'rate': rate,
            'timestamp': time.time(),
        })
        _save_queue(queue)
    logging.info("Transaction enqueued: %s BRL to %s (%s)", amount_brl, destination, payment_type)


def _fresh_rate_for(tx):
    """Só reaproveita a cotação salva se ela for recente; caso contrário busca
    uma fresca. Impede liquidar horas/dias depois com um preço obsoleto (que,
    se menor que o atual, faria o ATM enviar BTC a mais)."""
    stored = tx.get('rate')
    age = time.time() - tx.get('timestamp', 0)
    if stored and age <= _QUEUE_RATE_MAX_AGE:
        return stored
    return get_btc_rate()


def process_offline_queue():
    """Processa a fila offline com garantia de NO-MÁXIMO-UMA-VEZ por transação.

    Cada transação é REMOVIDA da fila persistida ANTES da tentativa de envio.
    Assim, um crash/queda de energia durante o envio não faz o pagamento ser
    retransmitido no próximo processamento — preferimos, no pior caso, perder
    o registro (reconciliável pelos logs) a arriscar um gasto duplo. Só uma
    falha comprovadamente-não-transmitida (PaymentNotBroadcast) recoloca a
    transação na fila — no FIM, e sem ser retentada nesta mesma passagem."""
    # Processa no máximo os itens presentes no início. Sem este limite, uma tx
    # que falha de forma determinística (ex.: endereço inválido → 4xx →
    # PaymentNotBroadcast) seria reenfileirada e repescada imediatamente, num
    # laço infinito martelando o BTCPay. Itens reenfileirados esperam o
    # próximo ciclo de retry.
    with _QUEUE_LOCK:
        pending = len(_load_queue())
    for _ in range(pending):
        with _QUEUE_LOCK:
            queue = _load_queue()
            if not queue:
                return
            tx = queue.pop(0)
            _save_queue(queue)  # a partir daqui, tx não está mais persistida

        try:
            rate = _fresh_rate_for(tx)
            if not rate:
                # Sem cotação: nada foi enviado → seguro reenfileirar. Para de
                # processar (as demais provavelmente falhariam igual); tenta de
                # novo no próximo ciclo.
                _requeue(tx)
                logging.warning("Sem cotação para processar a fila; adiada.")
                return
            if tx['payment_type'] == 'onchain':
                txid = send_onchain_payment(tx['amount_brl'], tx['destination'], rate)
            else:
                txid = send_lightning_payment(tx['amount_brl'], tx['destination'], rate)
            # Daqui em diante o dinheiro saiu: NADA pode reenfileirar esta tx.
            amount_btc = brl_to_btc(tx['amount_brl'], rate)
            print_receipt(tx['amount_brl'], amount_btc, tx['destination'], txid)
            logging.info("Offline queue tx processed: %s", txid)
        except PaymentNotBroadcast as e:
            # Comprovadamente não transmitido → seguro recolocar na fila, até
            # o teto de tentativas.
            tx['attempts'] = tx.get('attempts', 0) + 1
            if tx['attempts'] >= _MAX_QUEUE_ATTEMPTS:
                _dead_letter(tx, e)
            else:
                logging.warning("Queued tx not broadcast (tentativa %s/%s), "
                                "re-queued: %s", tx['attempts'],
                                _MAX_QUEUE_ATTEMPTS, e)
                _requeue(tx)
        except PaymentUncertain as e:
            # Ambíguo (já removida da fila): NÃO reenfileira, para não arriscar
            # gasto duplo. Operador confere a carteira.
            logging.error("Queued tx UNCERTAIN, dropped to avoid double-spend: %s", e)
        except Exception as e:
            # Erro inesperado após um possível envio (já removida): não
            # reenfileira. Registra para reconciliação manual.
            logging.error("Queued tx failed post-removal, NOT re-queued: %s", e)


def _requeue(tx):
    with _QUEUE_LOCK:
        queue = _load_queue()
        queue.append(tx)
        _save_queue(queue)


def _dead_letter(tx, error):
    """Tira da fila principal uma transação que esgotou as tentativas e a
    guarda em _FAILED_QUEUE_PATH.

    Não é descarte: é dinheiro parado esperando uma pessoa. A transação sai do
    laço de retry (que ficaria martelando o BTCPay indefinidamente) mas o
    registro é preservado com o motivo, para o operador reembolsar o cliente
    ou corrigir o destino e reprocessar manualmente."""
    tx['failed_at'] = time.time()
    tx['last_error'] = str(error)
    with _QUEUE_LOCK:
        failed = _load_queue(_FAILED_QUEUE_PATH)
        failed.append(tx)
        _save_queue(failed, _FAILED_QUEUE_PATH)
    logging.critical(
        "TRANSAÇÃO DESISTIDA após %s tentativas: R$%s para %s (%s). Movida "
        "para %s. AÇÃO MANUAL NECESSÁRIA (reembolso ou reenvio). Último erro: %s",
        tx['attempts'], tx.get('amount_brl'), tx.get('destination'),
        tx.get('payment_type'), _FAILED_QUEUE_PATH, error)


def _load_queue(path=None):
    # O caminho é lido em tempo de chamada (e não como default do parâmetro)
    # para que continue valendo se o módulo for reconfigurado.
    path = path or _QUEUE_PATH
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []


def _save_queue(queue, path=None):
    """Grava a fila de forma atômica (arquivo temporário + os.replace). Uma
    queda de energia no meio da escrita não pode corromper o JSON e fazer
    _load_queue descartar TODAS as pendências silenciosamente."""
    path = path or _QUEUE_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, 'w') as f:
        json.dump(queue, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _send_telegram(amount_brl, amount_btc, destination, txid, payment_type):
    try:
        msg = (
            f"[Bitcoin ATM] Nova transação\n"
            f"Tipo: {payment_type}\n"
            f"Valor: R$ {amount_brl:.2f}\n"
            f"BTC: {amount_btc:.8f}\n"
            f"Destino: {destination[:30]}...\n"
            f"TxID: {txid}"
        )
        telegram_send.send(messages=[msg])
    except Exception as e:
        logging.warning("Telegram notification failed: %s", e)
