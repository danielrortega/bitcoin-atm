import os
import sys

from cryptography.fernet import Fernet

_KEY_PATH = '/etc/atm/key'

if __name__ == "__main__":
    # Nunca sobrescrever uma chave existente: isso tornaria o api_token já
    # criptografado impossível de decifrar, e o ATM não conseguiria pagar até
    # o token ser re-encriptado (as transações ficam acumuladas na fila).
    if os.path.exists(_KEY_PATH):
        sys.exit(
            f"ERRO: {_KEY_PATH} já existe. Sobrescrever a chave tornaria o "
            "api_token criptografado ilegível. Se tiver certeza, remova o "
            "arquivo manualmente e re-encripte o token depois.")

    os.makedirs(os.path.dirname(_KEY_PATH), mode=0o700, exist_ok=True)
    key = Fernet.generate_key()
    # O_EXCL + modo 0600 desde a criação: nunca há uma janela em que a chave
    # fique legível por outros usuários locais (que poderiam decifrar o token
    # da API e drenar a carteira).
    fd = os.open(_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(key)
    os.chmod(_KEY_PATH, 0o600)
    print(f"Chave gerada e salva em {_KEY_PATH} (permissões 0600). "
          "Guarde um backup seguro!")
