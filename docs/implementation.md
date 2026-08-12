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
| `PaymentNotBroadcast` | Certamente NÃO foi transmitido (sem conexão, 4xx, erro de preparação) | Seguro enfileirar |
| `PaymentUncertain` | Resultado ambíguo (timeout, 5xx) | **Não reenfileirar** — operador confere manualmente |

Essa distinção evita gasto duplo: se há dúvida sobre se o Bitcoin foi enviado, a transação é descartada da fila com um log de erro e o operador deve verificar a carteira no BTCPay Server.

Tudo que roda antes do POST — ler o `config.ini`, ler `/etc/atm/key`, descriptografar o token, resolver um Lightning Address — está envolvido por `_not_broadcast_on_error`, que converte qualquer exceção inesperada em `PaymentNotBroadcast`. Sem isso, um erro de configuração (chave ausente ou rotacionada) escapava como `FileNotFoundError`/`InvalidToken`, era tratado como ambíguo e a transação do cliente não era nem enviada nem preservada. Exceções já classificadas passam intactas, para que um `PaymentUncertain` nunca seja rebaixado.

### Aceitação de cédulas

- **Whitelist de denominações**: só valores reais de cédulas BRL (2, 5, 10, 20,
  50, 100, 200) são creditados; ruído elétrico ou frames concatenados no buffer
  serial são descartados com log (`atm_gui._parse_note`).
- **Acúmulo**: múltiplas cédulas somam ao total (`atm_gui._credit_note`), até o
  momento de confirmar o pagamento — inclusive depois de escolher o método.
- **Teto por transação** (`max_transaction_brl`): atingido o teto, novas cédulas
  não são creditadas (logadas em nível crítico para reembolso manual) e a tela
  orienta o cliente a não inserir mais notas. O teto pode ser excedido por no
  máximo uma cédula, pois a nota já está dentro da máquina quando o valor é
  lido — a inibição do noteiro por hardware é o complemento recomendado em
  produção.

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
| Lightning Address | `voce@walletofsatoshi.com` | Validação de formato (LUD-16) |

Redes aceitas: mainnet (`bc`, `1`, `3`), testnet/signet (`tb`, `m`, `n`), regtest (`bcrt`, `2`).

No fluxo Lightning, o destino aceito é uma invoice BOLT11 **ou** um Lightning Address (`is_valid_lightning_destination` em `utils.py`). O endereço é validado apenas no formato pela GUI; a resolução de rede acontece no momento do pagamento.

---

## Lightning Address (LUD-16 / LNURL-pay)

Para não exigir que o cliente gere uma invoice manualmente, o ATM aceita um
endereço Lightning fixo (ex.: `voce@walletofsatoshi.com`) e o resolve em uma
invoice BOLT11 com o valor exato, em `atm_core._resolve_lightning_address`:

1. `GET https://{domain}/.well-known/lnurlp/{user}` → metadados com `callback`,
   `minSendable` e `maxSendable` (em msat). Valida `tag == "payRequest"`.
2. Confere se o valor solicitado (msats) está dentro de `[minSendable, maxSendable]`.
3. `GET {callback}?amount={msats}` → resposta com `pr` (a invoice BOLT11).
4. **Verificação anti-overpayment:** `decode_bolt11_amount_msats(pr)` decodifica o
   valor codificado no HRP da invoice e exige que seja **exatamente** o solicitado.
   Se divergir, levanta `PaymentNotBroadcast` e nada é enviado.
5. A invoice resultante é paga pelo caminho BOLT11 normal.

**Classificação de erros:** toda a resolução ocorre *antes* de qualquer chamada
de pagamento ao BTCPay, então qualquer falha (rede, HTTP ≠ 200, JSON inválido,
valor fora dos limites, valor divergente) é `PaymentNotBroadcast` — seguro
reenfileirar, pois nenhum fundo saiu. Domínios `.onion` usam `http` (via Tor);
clearnet exige `https`.

**Compatibilidade:** funciona com qualquer carteira que implemente LNURL-pay —
Wallet of Satoshi, Blink, Phoenix, etc. O endereço é fixo, então o cliente pode
manter um QR impresso.

**Limitação conhecida:** a verificação do `description_hash` da invoice contra o
`metadata` (LUD-06) não é feita; apenas o valor é verificado. A invoice na fila
offline é resolvida novamente no reprocessamento (gera uma invoice nova).

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
| `max_transaction_brl` | `atm` | Teto por transação em BRL (padrão 1000). A aceitação de cédulas para ao atingi-lo |
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
