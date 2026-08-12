# Testes

Rodar a suíte inteira, a partir da raiz do repositório:

```bash
python -m unittest discover -s tests
```

Um módulo só, ou um teste só:

```bash
python -m unittest discover -s tests -p test_notes.py
python -m unittest discover -s tests -k test_teto_recusa_cedula_seguinte
```

Nada além da biblioteca padrão é necessário. As dependências de hardware
(PyQt5, pyserial, escpos, telegram-send, qrcode) recebem stubs em
`support.py` **só quando não estão instaladas** — no Raspberry Pi, com o
`venv` completo, os módulos reais são usados. Nenhum teste abre porta serial,
impressora, janela ou conexão de rede.

## Falhas esperadas

A saída normal hoje é:

```
OK (expected failures=1)
```

Cada `@unittest.expectedFailure` documenta um bug conhecido, com a correção
prevista no docstring da classe:

| Testes | Achado |
|---|---|
| `test_offline_queue.TestRetentativaInfinita` | Transação que falha de forma determinística é retentada a cada 5 minutos para sempre, sem contador de tentativas nem fila de descarte. |

**Ao corrigir um desses bugs, remova o `@unittest.expectedFailure`.** Se
esquecer, o `unittest` reporta `UNEXPECTED SUCCESS` e o run termina com código
de saída 1 — a correção não passa despercebida.

Já corrigidos por este caminho:

- **Erro antes do POST** (config ilegível, chave Fernet ausente ou rotacionada)
  escapava como exceção crua e era tratado como resultado ambíguo, então a
  transação não era enfileirada nem reenfileirada e o dinheiro do cliente se
  perdia. Hoje é `PaymentNotBroadcast` — ver
  `test_payments.TestErroAntesDoBroadcast` e
  `test_gui_resultado.TestErroDeConfiguracaoNaGui`.
- **Quadro de cédula partido entre duas leituras** era descartado pelas duas
  metades: a nota estava dentro da máquina e nada era creditado. Hoje o resto
  aguarda a leitura seguinte e o fluxo ressincroniza byte a byte — ver
  `test_notes.TestBufferResidual`, incluindo dois testes de fuzz que cobrem
  cortes de pedaço arbitrários nas duas direções (nunca perder cédula em fluxo
  limpo, nunca criar dinheiro com ruído).
- **Desfechos que exigem uma pessoa** não deixavam registro: cédula inserida
  durante o envio, resultado incerto e falha de enfileiramento produziam no
  máximo uma caixa de diálogo, que some no cliente seguinte. Hoje os três
  registram valor, destino e método em nível `CRITICAL` — ver
  `test_notes.TestNotaDuranteOPagamento` e
  `test_gui_resultado.TestRegistroParaReconciliacao`, que também fixa o outro
  lado: desfecho normal NÃO gera `CRITICAL`, para o nível não virar ruído.

## Convenção

Testes que fixam uma decisão em aberto (e não um acerto) dizem isso no
docstring — por exemplo `TestRedeNaoAmarradaAConfig`, que registra que
endereços de testnet são aceitos num ATM de mainnet porque a validação não
conhece a rede configurada. Ao amarrar a rede ao `config.ini`, esse teste
muda de propósito, não por acidente.

Vetores de endereço e invoice são **gerados** por codificadores de referência
em `support.py` (Base58Check e bech32, escritos a partir do BIP-173), nunca
digitados de memória: um checksum errado faz o teste "provar" uma rejeição que
o código não deveria estar fazendo.
