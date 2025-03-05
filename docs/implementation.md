# Tutorial de Implementação do Bitcoin ATM

Este tutorial guiará você na configuração e execução do Bitcoin ATM em um sistema Linux AMD64.

## Pré-requisitos
- **Hardware**: Noteiro BV20, impressora USB (ex.: 0x0416:0x5011).
- **Sistema**: Linux AMD64 (ex.: Ubuntu 20.04+).
- **Software**: Python 3.8+, Tor (opcional para anonimato).
- **Acesso**: Permissões de root para criar diretórios e acessar dispositivos.

## Passo 1: Clonar o Repositório
Clone o repositório do GitHub:

    git clone https://github.com/seu-usuario/bitcoin-atm.git
    cd bitcoin-atm

## Passo 2: Instalar Dependências

Instale as bibliotecas necessárias:

    pip install -r requirements.txt

Certifique-se de que o Tor está rodando (se usado):

    sudo systemctl start tor

## Passo 3: Gerar Chave de Criptografia

Gere uma chave para criptografar dados sensíveis:

    sudo mkdir -p /etc/atm
    sudo python scripts/generate_key.py
    sudo chmod 600 /etc/atm/key

## Passo 4: Configurar o Arquivo config.ini

1. Copie o exemplo:

        cp config.ini.example config.ini

2. Edite config.ini com suas informações:

- host, store_id, wallet_id, etc., do BTCPay Server.
- Porta serial do noteiro (ex.: /dev/ttyUSB0).
- ID da impressora USB.
- chat_id do Telegram (obtenha com telegram-send --configure).

3. Criptografe o api_token:

        python

        from cryptography.fernet import Fernet
        with open('/etc/atm/key', 'rb') as f:
            cipher = Fernet(f.read())
        token = "seu_token_aqui"
        encrypted_token = cipher.encrypt(token.encode()).decode()
        print(encrypted_token)

4. Insira o resultado em api_token no config.ini.

## Passo 5: Configurar Permissões
Crie diretórios e configure permissões:


    sudo mkdir -p /var/atm /var/log
    sudo chown $USER:$USER /var/atm /var/log
    sudo chmod 700 /var/atm
    sudo chmod +rw /dev/ttyUSB0  # Ajuste conforme sua porta serial

## Passo 6: Testar a Aplicação
1. Execute o ATM:

        python src/main.py

2. Insira uma nota no noteiro para testar.

3. Escaneie um QR code (simulado por padrão após 5s).

## Passo 7: Configurar Telegram
1. Configure o telegram-send:

        telegram-send --configure

2. Siga as instruções para vincular ao seu bot Telegram.

## Passo 8: Implantação em Produção
1. Adicione como serviço systemd:

        sudo nano /etc/systemd/system/bitcoin-atm.service

2. Cole o conteúdo abaixo no arquivo:

        ini

        [Unit]
        Description=Bitcoin ATM Service
        After=network.target

        [Service]
        ExecStart=/usr/bin/python3 /path/to/bitcoin-atm/src/main.py
        WorkingDirectory=/path/to/bitcoin-atm
        Restart=always
        User=seu_usuario

        [Install]
        WantedBy=multi-user.target

3. Inicie o serviço

        sudo systemctl enable bitcoin-atm
        sudo systemctl start bitcoin-atm

## Solução de Problemas

- Logs: Verifique /var/log/btc_atm.log.

- Telegram: Certifique-se de que o chat_id está correto.

- Hardware: Confirme que os dispositivos estão conectados e acessíveis.

## Personalizações
- Substitua a simulação de QR code em main.py por um leitor real.

- Ajuste o estilo da GUI em atm_gui.py.


Com isso, seu Bitcoin ATM estará pronto para uso!

