# Bitcoin ATM

> **⚠️ EM DESENVOLVIMENTO — NÃO USAR EM PRODUÇÃO**

Um caixa eletrônico de Bitcoin (ATM) que aceita cédulas em reais (BRL) e envia Bitcoin para a carteira do cliente — via transação on-chain ou Lightning Network. Inclui interface gráfica touchscreen, impressão de recibo, fila offline e alertas via Telegram.

---

## Índice

1. [Como funciona](#como-funciona)
2. [O que você vai precisar](#o-que-você-vai-precisar)
3. [Passo a passo de instalação](#passo-a-passo-de-instalação)
   - [1. Preparar o sistema](#1-preparar-o-sistema)
   - [2. Baixar o projeto](#2-baixar-o-projeto)
   - [3. Instalar dependências Python](#3-instalar-dependências-python)
   - [4. Gerar a chave de segurança](#4-gerar-a-chave-de-segurança)
   - [5. Configurar o BTCPay Server](#5-configurar-o-btcpay-server)
   - [6. Preencher o config.ini](#6-preencher-o-configini)
   - [7. Configurar o Telegram (opcional)](#7-configurar-o-telegram-opcional)
   - [8. Testar a aplicação](#8-testar-a-aplicação)
   - [9. Executar como serviço (produção)](#9-executar-como-serviço-produção)
4. [Estrutura do projeto](#estrutura-do-projeto)
5. [Solução de problemas](#solução-de-problemas)

---

## Como funciona

```
Cliente insere cédula
       ↓
ATM lê o valor via noteiro serial (BV20)
       ↓
Cliente escolhe: On-Chain ou Lightning
       ↓
Cliente escaneia QR code da sua carteira
       ↓
ATM consulta cotação BTC/BRL no BTCPay Server
       ↓
ATM envia Bitcoin para o endereço do cliente
       ↓
Impressora imprime o recibo da transação
       ↓
Alerta enviado ao operador via Telegram
```

Se a internet cair durante o processo, a transação é salva em fila e processada automaticamente quando a conexão voltar.

---

## O que você vai precisar

### Hardware

| Item | Descrição | Onde encontrar |
|---|---|---|
| Computador Linux | Mini-PC ou Raspberry Pi 4+ (AMD64 ou ARM64) | Mercado Livre, Amazon |
| Noteiro BV20 | Leitor de cédulas com saída serial | Fornecedores de hardware para ATM |
| Impressora USB ESC/POS | Ex.: `0x0416:0x5011`. Qualquer impressora térmica compatível com ESC/POS | Mercado Livre, AliExpress |
| Leitor QR USB | Leitor de código de barras/QR que funciona como teclado USB (modo HID) | AliExpress, Amazon |
| Monitor touchscreen | Resolução mínima 800×480 | AliExpress, Amazon |
| Gabinete | Caixa para alojar os componentes | Fabricação própria ou fornecedor de ATM |

### Software / Serviços

| Item | Descrição |
|---|---|
| Linux Ubuntu 20.04+ | Sistema operacional (AMD64 recomendado) |
| Python 3.8 ou superior | Já vem instalado na maioria das distribuições Linux |
| BTCPay Server | Seu próprio servidor de pagamentos Bitcoin. [Saiba mais](https://btcpayserver.org) |
| Bot no Telegram (opcional) | Para receber alertas de transações no celular |

> **O que é o BTCPay Server?**
> É um software gratuito e de código aberto que você instala no seu próprio servidor (ou VPS) para processar pagamentos em Bitcoin sem depender de intermediários. Ele gerencia sua carteira e executa os pagamentos. Existe um [guia oficial de instalação](https://docs.btcpayserver.org/Deployment/) com opções a partir de R$ 20/mês em serviços de nuvem.

---

## Passo a passo de instalação

### 1. Preparar o sistema

Abra o terminal e execute os comandos abaixo para instalar as dependências do sistema:

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv git
```

Crie os diretórios que o ATM vai usar para logs e fila offline:

```bash
sudo mkdir -p /var/atm /var/log /etc/atm
sudo chown $USER:$USER /var/atm /var/log /etc/atm
sudo chmod 700 /var/atm /etc/atm
```

Dê permissão de acesso à porta serial do noteiro (substitua `ttyUSB0` pela porta correta do seu hardware):

```bash
sudo usermod -aG dialout $USER
sudo chmod a+rw /dev/ttyUSB0
```

> **Como saber qual porta é a certa?**
> Execute `ls /dev/tty*` antes e depois de conectar o noteiro. A porta que aparecer somente depois da conexão é a do seu dispositivo (normalmente `/dev/ttyUSB0` ou `/dev/ttyUSB1`).

---

### 2. Baixar o projeto

```bash
git clone https://github.com/danielrortega/bitcoin-atm.git
cd bitcoin-atm
```

---

### 3. Instalar dependências Python

É recomendado usar um ambiente virtual para não interferir com outros programas Python do sistema:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

> **O que é um ambiente virtual?**
> É uma pasta isolada onde as bibliotecas do projeto são instaladas sem afetar o resto do sistema. Sempre que for rodar o ATM, ative o ambiente com `source venv/bin/activate`.

---

### 4. Gerar a chave de segurança

O ATM criptografa o token da API do BTCPay Server para que ele não fique exposto em texto puro no arquivo de configuração. Gere a chave com:

```bash
python scripts/generate_key.py
```

Isso cria o arquivo `/etc/atm/key`. **Guarde um backup desse arquivo em local seguro.** Se você perdê-lo, não conseguirá descriptografar o token e precisará reconfigurar tudo.

---

### 5. Configurar o BTCPay Server

Você precisará de algumas informações do seu BTCPay Server. Acesse o painel e colete:

| Informação | Onde encontrar no BTCPay |
|---|---|
| URL do servidor | Endereço do seu servidor, ex.: `https://pay.minhaempresa.com` |
| Store ID | `Configurações da Loja → Geral → ID da Loja` |
| Wallet ID | `Carteiras → On-chain → Configurações → ID` (geralmente `BTC`) |
| Lightning Wallet ID | `Carteiras → Lightning → Configurações → ID` (geralmente `BTC`) |
| API Token | `Configurações da Conta → Chaves de API → Criar nova chave` (permissões: leitura de taxa, criação de transação, pagamento Lightning) |

Com o API Token em mãos, criptografe-o para usar no config:

```bash
source venv/bin/activate
python3 - <<'EOF'
from cryptography.fernet import Fernet

with open('/etc/atm/key', 'rb') as f:
    cipher = Fernet(f.read())

token = input("Cole seu API Token aqui: ")
print("\nToken criptografado (copie e cole no config.ini):")
print(cipher.encrypt(token.encode()).decode())
EOF
```

Anote o resultado — você vai usá-lo no próximo passo.

---

### 6. Preencher o config.ini

Copie o arquivo de exemplo e edite com suas informações:

```bash
cp config.ini.example config.ini
nano config.ini
```

Preencha cada campo:

```ini
[btcpay]
# Endereço completo do seu BTCPay Server (com https://)
host = https://pay.seuservidor.com

# ID da sua loja no BTCPay Server
store_id = AbCdEf1234567890

# O token criptografado gerado no passo anterior
api_token = gAAAAAB...

# Moeda fiduciária (deixe BRL para o Brasil)
currency = BRL

# ID da carteira on-chain (normalmente "BTC")
wallet_id = BTC

# ID da carteira Lightning (normalmente "BTC")
lightning_wallet_id = BTC

[hardware]
# Porta serial do noteiro BV20
serial_port = /dev/ttyUSB0

# Taxa de comunicação serial do noteiro (verifique o manual do BV20)
baud_rate = 9600

# ID USB da impressora no formato vendor_id:product_id
# Para descobrir, conecte a impressora e execute: lsusb
printer_usb = 0x0416:0x5011

[telegram]
# ID do chat para receber alertas (obtido com o @userinfobot no Telegram)
chat_id = 123456789
```

> **Como descobrir o ID USB da impressora?**
> Com a impressora conectada, execute `lsusb` no terminal. Você verá linhas como:
> `Bus 001 Device 003: ID 0416:5011 Winbond Electronics Corp.`
> Os números `0416:5011` são o `vendor_id:product_id`. Use-os no campo `printer_usb`.

---

### 7. Configurar o Telegram (opcional)

O Telegram envia um alerta para o seu celular a cada transação realizada.

1. No Telegram, procure o bot `@BotFather` e crie um novo bot com o comando `/newbot`. Anote o token gerado.

2. Configure o `telegram-send` no terminal:

```bash
source venv/bin/activate
telegram-send --configure
```

Siga as instruções na tela: ele vai pedir o token do bot e enviar uma mensagem de teste.

3. Para descobrir o seu `chat_id`, envie qualquer mensagem para o seu bot e acesse:
`https://api.telegram.org/bot<SEU_TOKEN>/getUpdates`
O `chat_id` aparece no campo `"chat": {"id": ...}`.

---

### 8. Testar a aplicação

Com tudo configurado, execute:

```bash
source venv/bin/activate
python src/main.py
```

A interface gráfica deve abrir em tela cheia. Para sair, pressione `Alt+F4`.

**Fluxo de teste:**
1. O ATM exibe a cotação BTC/BRL (atualizada a cada 30 segundos).
2. Insira uma cédula no noteiro. O valor deve aparecer na tela.
3. Escolha "Enviar On-Chain" ou "Enviar via Lightning".
4. Aponte o leitor QR para o endereço da sua carteira de teste.
5. O endereço aparece na tela e o botão "Confirmar" é habilitado.
6. Clique em "Confirmar". O Bitcoin é enviado e o recibo é impresso.

> **Dica:** Use valores pequenos para os primeiros testes e uma carteira de teste separada.

---

### 9. Executar como serviço (produção)

Para que o ATM inicie automaticamente com o sistema, configure um serviço systemd.

Crie o arquivo de serviço:

```bash
sudo nano /etc/systemd/system/bitcoin-atm.service
```

Cole o conteúdo abaixo, substituindo `/caminho/para/bitcoin-atm` pelo caminho real do projeto e `seu_usuario` pelo seu nome de usuário Linux:

```ini
[Unit]
Description=Bitcoin ATM
After=network.target

[Service]
ExecStart=/caminho/para/bitcoin-atm/venv/bin/python /caminho/para/bitcoin-atm/src/main.py
WorkingDirectory=/caminho/para/bitcoin-atm
Restart=always
RestartSec=5
User=seu_usuario
Environment=DISPLAY=:0

[Install]
WantedBy=multi-user.target
```

Ative e inicie o serviço:

```bash
sudo systemctl daemon-reload
sudo systemctl enable bitcoin-atm
sudo systemctl start bitcoin-atm
```

Para verificar se está rodando:

```bash
sudo systemctl status bitcoin-atm
```

---

## Estrutura do projeto

```
bitcoin-atm/
├── config.ini.example    # Modelo do arquivo de configuração
├── requirements.txt      # Dependências Python
├── docs/
│   └── implementation.md # Documentação técnica detalhada
├── scripts/
│   └── generate_key.py   # Gera a chave de criptografia
└── src/
    ├── main.py           # Ponto de entrada da aplicação
    ├── atm_gui.py        # Interface gráfica (PyQt5)
    ├── atm_core.py       # Lógica de negócio (BTCPay, impressora, fila)
    └── utils.py          # Funções auxiliares (validação, QR, rede)
```

---

## Solução de problemas

**A interface não abre / erro de display**
```bash
export DISPLAY=:0
python src/main.py
```

**"Porta serial não encontrada" / erro no noteiro**
- Verifique se o noteiro está conectado: `ls /dev/ttyUSB*`
- Confirme que o usuário tem permissão: `groups $USER` (deve incluir `dialout`)
- Corrija a porta no `config.ini` se necessário

**"Erro ao conectar à impressora"**
- Verifique se a impressora está ligada e conectada via USB
- Confirme o ID correto com `lsusb`
- Teste com `sudo` para descartar problema de permissão

**"Cotação indisponível (offline)"**
- Verifique a conexão com a internet: `ping 8.8.8.8`
- Verifique se o endereço do BTCPay Server em `config.ini` está correto
- Confira se o API Token foi criptografado corretamente (refaça o passo 5)

**Transações na fila não são processadas**
- As transações offline ficam em `/var/atm/offline_queue.json`
- Elas são processadas automaticamente na próxima inicialização com internet
- Para processar manualmente: `python -c "from atm_core import process_offline_queue; process_offline_queue()"`

**Ver os logs do ATM**
```bash
tail -f /var/log/btc_atm.log
```

**Ver os logs do serviço systemd**
```bash
sudo journalctl -u bitcoin-atm -f
```

---

## Licença

Veja o arquivo [LICENSE](LICENSE).
