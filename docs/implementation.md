# Documentação Técnica — Bitcoin ATM

> Para o guia completo de instalação, consulte o [README](../README.md).

---

## Arquitetura

```
src/
├── main.py          — ponto de entrada; inicia Qt, dispara flush da fila em background
├── atm_gui.py       — interface gráfica (PyQt5); toda I/O bloqueante roda em QThreadPool
├── atm_core.py      — lógica de negócio: BTCPay API, impressão ESC/POS, fila offline
├── btc_address.py   — validação com checksum (Base58Check + Bech32/Bech32m + BOLT11)
└── utils.py         — helpers: QR code, validação de endereço, is_online()
```

---

## Threading

Todas as chamadas de rede e USB rodam em workers `QRunnable` gerenciados por `QThreadPool`, fora da thread de eventos do Qt. Isso evita que a GUI congele durante:

- consultas de cotação ao BTCPay Server (`get_btc_rate` — até 10 s offline)
- envio de pagamentos (`send_onchain_payment` / `send_lightning_payment` — até 30 s)
- impressão de recibo (`print_receipt` — USB)
- processamento da fila offline na inicialização (`process_offline_queue`)

O padrão usado é `_Worker(_WorkerSignals)` com sinais `result`, `error` e `finished`. Referências ao worker são mantidas em `self._workers` até o sinal `finished` para evitar coleta de lixo prematura.

---

## Segurança financeira

### Classificação de exceções

| Exceção | Significado | Ação |
|---|---|---|
| `PaymentNotBroadcast` | Certamente NÃO foi transmitido (sem conexão, 4xx) | Seguro enfileirar |
| `PaymentUncertain` | Resultado ambíguo (timeout, 5xx) | **Não reenfileirar** — operador confere manualmente |

Essa distinção evita gasto duplo: se há dúvida sobre se o Bitcoin foi enviado, a transação é descartada da fila com um log de erro e o operador deve verificar a carteira no BTCPay Server.

### Precisão decimal

Conversões BRL→BTC usam `decimal.Decimal` com `ROUND_DOWN` para nunca enviar mais do que o cliente pagou:

```python
def brl_to_btc(amount_brl, rate):
    return (Decimal(str(amount_brl)) / Decimal(str(rate))).quantize(
        Decimal('0.00000001'), rounding=ROUND_DOWN)
```

### Token da API

O token BTCPay é armazenado **criptografado** com Fernet (chave AES-128-CBC) em `config.ini`. A chave fica em `/etc/atm/key` com permissão `600`. Sem a chave, o token cifrado é inútil.

---

## Validação de endereços Bitcoin

`btc_address.py` implementa validação com checksum em Python puro (sem dependências externas):

| Tipo | Exemplos | Algoritmo |
|---|---|---|
| Legacy P2PKH/P2SH | `1...`, `3...`, `m...`, `n...`, `2...` | Base58Check (double-SHA256) |
| SegWit v0 (P2WPKH/P2WSH) | `bc1q...`, `tb1q...` | Bech32 (BIP173) |
| Taproot / SegWit v1+ | `bc1p...`, `tb1p...` | Bech32m (BIP350) |
| Lightning BOLT11 | `lnbc...`, `lntb...`, `lnbcrt...` | Bech32 sem limite de 90 chars |

Redes aceitas: mainnet (`bc`, `1`, `3`), testnet/signet (`tb`, `m`, `n`), regtest (`bcrt`, `2`).

---

## API BTCPay Server (Greenfield v1)

### Cotação

```
GET /api/v1/stores/{storeId}/rates?currencyPair=BTC_BRL
```

### Pagamento on-chain

```
POST /api/v1/stores/{storeId}/payment-methods/BTC-CHAIN/wallet/transactions
```

O método (`BTC-CHAIN`) pode ser sobrescrito via `onchain_payment_method` no `config.ini` para instâncias antigas. O campo `feerate` é omitido intencionalmente para usar a estimativa automática do BTCPay (enviar `null` causa erro 422).

### Pagamento Lightning

```
POST /api/v1/stores/{storeId}/lightning/BTC/invoices/pay
```

Status `200` = completo, `202` = pendente (ambos tratados como enviado), `Failed` = não enviado → seguro reenfileirar.

---

## Fila offline

Transações que falharam com `PaymentNotBroadcast` são salvas em `/var/atm/offline_queue.json`. Na próxima inicialização, se `is_online()` retornar `True`, `process_offline_queue()` é executado em background (via `QThreadPool`) sem bloquear a GUI.

`is_online()` verifica conectividade fazendo `HEAD` no host do BTCPay configurado — não usa servidores externos (compatível com ambientes Tor).

---

## Configuração (`config.ini`)

| Campo | Seção | Descrição |
|---|---|---|
| `host` | `btcpay` | URL completa do BTCPay Server |
| `store_id` | `btcpay` | ID da loja no BTCPay |
| `api_token` | `btcpay` | Token Fernet-criptografado |
| `currency` | `btcpay` | Moeda fiduciária (ex.: `BRL`) |
| `crypto_code` | `btcpay` | Código da cripto (ex.: `BTC`) |
| `onchain_payment_method` | `btcpay` | (Opcional) Sobrescreve `BTC-CHAIN` |
| `serial_port` | `hardware` | Porta serial do noteiro (ex.: `/dev/ttyUSB0`) |
| `baud_rate` | `hardware` | Baud rate serial (ex.: `9600`) |
| `printer_usb` | `hardware` | ID USB da impressora `vendor:product` (ex.: `0416:5011`) |
| `chat_id` | `telegram` | ID do chat para alertas |

---

## Logs

O ATM registra em `/var/log/btc_atm.log`. Se não tiver permissão de escrita (ex.: em desenvolvimento), cai automaticamente para `stderr`. Para seguir os logs em tempo real:

```bash
tail -f /var/log/btc_atm.log
# ou, se rodando como serviço systemd:
sudo journalctl -u bitcoin-atm -f
```
